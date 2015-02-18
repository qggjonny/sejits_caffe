from sejits_caffe.layers.conv_layer import ConvLayer
import numpy as np
import sejits_caffe.caffe_pb2 as caffe_pb2
from google.protobuf import text_format
import os
from hindemith.types.hmarray import hmarray
import unittest
from ctree.jit import LazySpecializedFunction, ConcreteSpecializedFunction
from ctree.c.nodes import CFile, Constant
from ctree.nodes import Project
from ctree.templates.nodes import FileTemplate
import ctypes as ct
import ast


class ConvConcrete(ConcreteSpecializedFunction):
    def __init__(self, entry_name, proj, entry_type):
        self._c_function = self._compile(entry_name, proj, entry_type)

    def __call__(self, *args):
        self._c_function(*args)


class NaiveConv(LazySpecializedFunction):
    def __init__(self, conv_param):
        super(NaiveConv, self).__init__(ast.Module())
        self.conv_param = conv_param

    def args_to_subconfig(self, args):
        cfg = {}
        for name, arg in zip(['in_ptr', 'weights', 'bias', 'out'], args):
            cfg[name] = np.ctypeslib.ndpointer(arg.dtype, arg.ndim, arg.shape)
        return cfg

    def transform(self, tree, program_cfg):
        arg_cfg, tune_cfg = program_cfg
        kernel_size = conv_param.kernel_size
        pad = conv_param.pad
        stride = conv_param.stride
        group = conv_param.group
        out_num, out_c, out_h, out_w = arg_cfg['out']._shape_
        in_ptr_num, in_ptr_c, in_ptr_h, in_ptr_w = arg_cfg['in_ptr']._shape_
        weights_g, weights_c, weights_h, weights_w = arg_cfg['weights']._shape_
        return [CFile('conv', [
            FileTemplate(
                os.path.dirname(os.path.realpath(__file__)) + '/conv_test.tmpl.c',
                {
                    'kernel_size': Constant(kernel_size),
                    'pad': Constant(pad),
                    'stride': Constant(stride),
                    'group': Constant(group),
                    'out_num': Constant(out_num),
                    'out_c': Constant(out_c),
                    'out_h': Constant(out_h),
                    'out_w': Constant(out_w),
                    'in_num': Constant(in_ptr_num),
                    'in_c': Constant(in_ptr_c),
                    'in_h': Constant(in_ptr_h),
                    'in_w': Constant(in_ptr_w),
                    'weight_g': Constant(weights_g),
                    'weight_c': Constant(weights_c),
                    'weight_h': Constant(weights_h),
                    'weight_w': Constant(weights_w),
                    'bias_term': Constant(1)
                }
        )], config_target='omp')]

    def finalize(self, files, program_cfg):
        arg_cfg, tune_cfg = program_cfg
        proj = Project(files)
        entry_type = (None, )
        for name in ['in_ptr', 'weights', 'bias', 'out']:
            entry_type += (arg_cfg[name], )
        fn = ConvConcrete('conv', proj, ct.CFUNCTYPE(*entry_type))
        return fn
        


path = os.path.dirname(os.path.realpath(__file__))
param = caffe_pb2.NetParameter()
param_string = open(path + '/test.prototxt').read()
text_format.Merge(param_string, param)
conv_param = param.layers[0].convolution_param
height_out = (256 - conv_param.kernel_size) + 1
width_out = (256 - conv_param.kernel_size) + 1
actual_shape = (5, conv_param.num_output, height_out * width_out)
expected_shape = (5, conv_param.num_output, height_out, width_out)


class ConvLayerTest(unittest.TestCase):
    def _check(self, actual, expected):
        try:
            np.testing.assert_allclose(actual, expected, rtol=1e-04)
        except AssertionError as e:
            self.fail(e)

    def _forward_test(self, backend):
        conv = ConvLayer(param.layers[0])
        expected_conv = NaiveConv(param.layers[0].convolution_param)
        conv.backend = backend
        actual = hmarray(np.zeros(actual_shape, np.float32))
        expected = np.zeros(expected_shape, np.float32)
        in_batch = np.random.rand(5, 3, 256, 256).astype(np.float32) * 255

        conv.set_up(hmarray(in_batch), actual)
        conv.forward(hmarray(in_batch), actual)
        actual = actual.reshape((5, 25, height_out, width_out))
        new_weights = conv.weights.reshape(25, 3, 11, 11)
        expected_conv(in_batch, new_weights, conv.bias, expected)
        actual.copy_to_host_if_dirty()
        self._check(actual, expected)

    def test_cpu_forward(self):
        self._forward_test('cpu')

    def test_gpu_forward(self):
        self._forward_test('gpu')
