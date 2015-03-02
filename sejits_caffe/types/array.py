import numpy as np

from ctree.frontend import get_ast
from ctree.jit import LazySpecializedFunction, ConcreteSpecializedFunction
from collections import namedtuple
from ctree.transformations import PyBasicConversions
from ctree.transforms import ConstantFold
import ctree.c.nodes as C
from ctree.templates.nodes import StringTemplate
from ctree.nodes import Project
import ctypes as ct
import ast
import inspect
import ctree.np

arr_cfg = namedtuple('arr_cfg', ['shape', 'dtype'])
tuple_cfg = namedtuple('tuple_cfg', ['val'])


class Backend(ast.NodeTransformer):
    def __init__(self, arg_cfg, symbol_table):
        self.symbol_table = symbol_table
        self.arg_cfg = arg_cfg
        self.cfg_dict = {}
        self.loop_shape_map = {}
        self.defns = []

    def visit_CFile(self, node):
        self.defns = []
        node = super(Backend, self).generic_visit(node)
        for defn in self.defns:
            node.body.insert(0, defn)
        return node

    def visit_FunctionDecl(self, node):
        for param, cfg in zip(node.params, self.arg_cfg):
            if type(cfg) == arr_cfg:
                param.type = np.ctypeslib.ndpointer(cfg.dtype, len(cfg.shape),
                                                    cfg.shape)()
            elif type(cfg) == int:
                param.type = ct.c_int()
            else:
                # TODO: Generalize type inference or add support for all types
                raise NotImplementedError()
            self.cfg_dict[param.name] = cfg
        node.defn = list(map(self.visit, node.defn))
        return node

    def gen_loop_nest(self, loopvars, cfg):
        body = []
        node = C.For(C.Assign(C.SymbolRef(loopvars[0], ct.c_int()),
                              C.Constant(0)),
                     C.Lt(C.SymbolRef(loopvars[0]), C.Constant(cfg.shape[0])),
                     C.PostInc(C.SymbolRef(loopvars[0])),
                     body)
        curr_node = node
        for loopvar, dim in zip(loopvars[1:], cfg.shape[1:]):
            curr_node = C.For(C.Assign(C.SymbolRef(loopvar, ct.c_int()),
                                       C.Constant(0)),
                              C.Lt(C.SymbolRef(loopvar), C.Constant(dim)),
                              C.PostInc(C.SymbolRef(loopvar)),
                              [])
            body.append(curr_node)
            body = curr_node.body
        self.loop_shape_map[loopvars] = cfg.shape
        return node, curr_node

    def is_loop_by_index(self, node):
        if isinstance(node.iter, ast.Call):
            if isinstance(node.iter.func, ast.Attribute):
                if node.iter.func.attr == 'indices':
                    return True
        return False

    def visit_For(self, node):
        if self.is_loop_by_index(node):
            cfg = self.cfg_dict[node.iter.func.value.id]
            loopvars = tuple(var.id for var in node.target.elts)
            outer, inner = self.gen_loop_nest(loopvars, cfg)
            inner.body = list(map(self.visit, node.body))
            return outer

        node.body = list(map(self.visit, node.body))
        return node

    def gen_loop_index(self, loopvars, shape):
        curr = C.SymbolRef(loopvars[-1])
        for i in reversed(range(len(loopvars) - 1)):
            curr = C.Add(
                C.Mul(C.SymbolRef(loopvars[i]),
                      C.Constant(np.prod(shape[i + 1:]))),
                curr
            )
        return curr

    def visit_SymbolRef(self, node):
        # if node.name in self.cfg_dict:
        #     return C.Constant(self.cfg_dict[node.name])
        return node

    def visit_BinaryOp(self, node):
        if isinstance(node.op, C.Op.ArrayRef):
            if isinstance(node.left, C.SymbolRef):
                target = node.left.name
                if target in self.cfg_dict:
                    target = self.cfg_dict[target]
                    # if type(target) in {int, float}:
                    #     return C.Constant(target)
                    loopvars = tuple(var.name for var in node.right.elts)
                    node.right = self.gen_loop_index(
                        loopvars, target.shape)
                    return node
            if isinstance(node.left, ast.Attribute):
                if node.left.value.name in self.cfg_dict:
                    attr = getattr(self.cfg_dict[node.left.value.name],
                                   node.left.attr)
                    return C.Constant(attr[node.right.value])
                else:
                    raise NotImplementedError()
        node.left = self.visit(node.left)
        node.right = self.visit(node.right)
        return node

    def visit_FunctionCall(self, node):
        # FIXME: This is specific for handling a map function
        # do we have to generalize?
        node = super(Backend, self).generic_visit(node)
        func_tree = get_ast(self.symbol_table[node.func.name])
        func_tree = PyBasicConversions().visit(func_tree).body[0]
        func_tree.name = C.SymbolRef(node.func.name)
        self.defns.append(func_tree)
        # FIXME: Infer type
        func_tree.params[0].type = ct.c_float()
        func_tree.return_type = ct.c_float()
        return node


class CacheBlockLoopNests(ast.NodeTransformer):
    def __init__(self):
        super(CacheBlockLoopNests, self).__init__()
        self.block_factor = 32
        self.inside_nest = False
        self.nest = []

    def gen_nest(self):
        ret_node = self.nest[0]
        ret_node.pragma = 'omp for'
        curr_node = ret_node
        for node in self.nest[1:-1]:
            curr_node.body[0] = node
            curr_node = node
        return ret_node

    def visit_CFile(self, node):
        node.body = [self.visit(s) for s in node.body]
        node.body.insert(0, StringTemplate("#include <math.h>"))
        return node

    def block_loop(self, node):
        loopvar = node.init.left.name
        loopvar += loopvar
        self.nest.insert(
            0,
            C.For(
                C.Assign(C.SymbolRef(loopvar, node.init.left.type),
                         node.init.right),
                C.Lt(C.SymbolRef(loopvar), node.test.right),
                C.AddAssign(C.SymbolRef(loopvar),
                            C.Constant(self.block_factor)),
                [None]
            )
        )
        node.init.right = C.SymbolRef(loopvar)
        node.test.right = C.FunctionCall(
            C.SymbolRef("fmin"),
            [C.Add(C.SymbolRef(loopvar),
                   C.Constant(self.block_factor)),
             node.test.right])

    def visit_For(self, node):
        start = node.init.right.value
        end = node.test.right.value
        if end - start < self.block_factor:
            return node
        elif self.inside_nest:
            self.nest.append(node)
            if isinstance(node.body[0], C.For):
                self.visit(node.body[0])
            self.block_loop(node)
        else:
            if isinstance(node.body[0], C.For):
                self.inside_nest = True
                self.nest.append(node)
                self.visit(node.body[0])
                self.block_loop(node)
                return self.gen_nest()
            else:
                return node


class ConcreteFn(ConcreteSpecializedFunction):
    def __init__(self, entry_name, proj, entry_type):
        self._c_function = self._compile(entry_name, proj, entry_type)

    def __call__(self, *args, **kwargs):
        a = []
        for i in range(len(self._c_function.argtypes)):
            a.append(args[i])
        return self._c_function(*a)


class SpecializedFn(LazySpecializedFunction):
    def __init__(self, tree, symbol_table):
        super(SpecializedFn, self).__init__(tree)
        self.symbol_table = symbol_table

    def args_to_subconfig(self, args, kwargs):
        arg_cfg = ()
        for arg in args:
            if isinstance(arg, Array):
                arg_cfg += (arr_cfg(arg.shape, arg.dtype), )
            elif type(arg) in {int, float, np.float32}:
                arg_cfg += (arg, )
            else:
                raise Exception("Unsupport arg type {}".format(type(arg)))
        for key in kwargs:
            if type(arg) in {int, float}:
                arg_cfg += (kwargs[key], )
            else:
                raise Exception("Unsupport kwarg type {}".format(type(arg)))
        return arg_cfg

    def transform(self, tree, program_cfg):
        arg_cfg, tune_cfg = program_cfg
        tree = PyBasicConversions().visit(tree)
        tree = Backend(arg_cfg, self.symbol_table).visit(tree)
        tree = ConstantFold().visit(tree)
        # tree = CacheBlockLoopNests().visit(tree)
        tree.name = self.original_tree.body[0].name
        return tree

    def finalize(self, files, program_cfg):
        arg_cfg, tune_cfg = program_cfg
        entry_type = (None, )
        for cfg in arg_cfg:
            if isinstance(cfg, arr_cfg):
                entry_type += (np.ctypeslib.ndpointer(cfg.dtype,
                                                      len(cfg.shape),
                                                      cfg.shape), )
            elif isinstance(cfg, np.float32):
                entry_type += (ct.c_float, )
            elif isinstance(cfg, int):
                entry_type += (ct.c_int, )
            else:
                raise NotImplementedError()
        entry_type = ct.CFUNCTYPE(*entry_type)
        return ConcreteFn(files[0].name,
                          Project(files), entry_type)


def specialize(fn):

    frame = inspect.stack()[1][0]
    symbol_table = frame.f_locals
    # FIXME: symbol_table prints out a huge dict, why??

    spec_fn = SpecializedFn(get_ast(fn), symbol_table)

    def fn(*args, **kwargs):
        return spec_fn(*args, **kwargs)
    fn._specializer = spec_fn
    return fn


class SpecializedDispatch(object):
    def __init__(self, fn):
        self.fn = fn

    def __call__(self, *args, **kwargs):
        return self.fn(*args, **kwargs)(*args, **kwargs)


@specialize
def array_array_add(a, b, output):
    for y, x in output.indices():
        output[y, x] = a[y, x] + b[y, x]


@specialize
def array_scalar_add(a, b, output):
    for y, x in output.indices():
        output[y, x] = a[y, x] + b


def smap(func):
    @specialize
    def fn(a, output):
        for y, x in output.indices():
            output[y, x] = func(a[y, x])
    return fn


class Array(np.ndarray):
    @staticmethod
    def zeros(*args, **kwargs):
        return np.zeros(*args, **kwargs).view(Array)

    @staticmethod
    def rand(*args, **kwargs):
        return np.random.rand(*args, **kwargs).view(Array)

    @staticmethod
    def standard_normal(*args, **kwargs):
        return np.random.standard_normal(*args, **kwargs).view(Array)

    @staticmethod
    def empty_like(*args, **kwargs):
        return np.empty_like(*args, **kwargs)

    @staticmethod
    @SpecializedDispatch
    def add(a, b, output):
        if isinstance(a, Array) and isinstance(b, Array):
            return array_array_add
        elif isinstance(a, Array) and type(b) in {np.float32}:
            return array_scalar_add
        raise NotImplementedError()
