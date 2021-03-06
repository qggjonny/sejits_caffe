#!/usr/bin/env python
"""
Draw a graph of the net architecture.
"""
# import os
from google.protobuf import text_format
import caffe_pb2

from layers.conv_layer import ConvLayer
from layers.relu_layer import ReluLayer
from layers.data_layer import DataLayer
from layers.lrn_layer import LRNLayer
from layers.pooling_layer import PoolingLayer
from layers.inner_product_layer import InnerProductLayer
from layers.dropout_layer import DropoutLayer
from layers.accuracy_layer import AccuracyLayer
from layers.softmax_loss_layer import SoftMaxWithLossLayer
import numpy as np

from cstructures.array import Array
from ctree.util import Timer


TRAIN = caffe_pb2.TRAIN


class Net(object):
    layer_type_map = {
        "Data": DataLayer,
        "Convolution": ConvLayer,
        "ReLU": ReluLayer,
        "LRN": LRNLayer,
        "Pooling": PoolingLayer,
        "InnerProduct": InnerProductLayer,
        "Dropout": DropoutLayer,
        "Accuracy": AccuracyLayer,
        "SoftmaxWithLoss": SoftMaxWithLossLayer,
    }

    def __init__(self, param_file):
        self.phase = TRAIN
        # importing net param from .prototxt
        self.param = caffe_pb2.NetParameter()
        param_string = open(param_file).read()
        text_format.Merge(param_string, self.param)
        self.layers = []
        self.blobs = {}
        data_layers = self.get_data_layers_for_phase(self.param.layer)
        for layer_param in data_layers:
            layer = DataLayer(layer_param)
            top_shape = layer.get_top_shape()
            top = []
            for blob in layer_param.top:
                self.add_blob(blob, top_shape)
                top.append(self.blobs[blob])
            layer.setup(*top)
            self.layers.append(layer)
        for layer_param in self.param.layer:
            if layer_param.type == "Data":
                continue
            bottom = []
            top = []
            for blob in layer_param.bottom:
                if blob not in self.blobs:
                    raise Exception("Found uninitialized blob {}".format(blob))
                bottom.append(self.blobs[blob])
            layer = self.layer_type_map[layer_param.type](layer_param)
            top_shape = layer.get_top_shape(*bottom)
            for blob in layer_param.top:
                if blob not in self.blobs:
                    self.add_blob(blob, top_shape)
                top.append(self.blobs[blob])
            # print(layer_param.type)
            layer.setup(*(bottom + top))
            self.layers.append(layer)
        # print(self.layers)

    def forward(self):
        loss = 0
        for layer in self.layers:
            layer_param = layer.layer_param
            bottom = []
            top = []
            for blob in layer_param.bottom:
                bottom.append(self.blobs[blob])
            for blob in layer_param.top:
                top.append(self.blobs[blob])
            with Timer() as t:
                layer.forward(*(bottom + top))
            print("{} layer time: {}s".format(layer_param.type, t.interval))
        # print("Loss: {}".format(loss))
        return loss

    def add_blob(self, blob, shape):
        self.blobs[blob] = Array.zeros(shape, np.float32)

    def get_data_layers_for_phase(self, layers):
        # TODO: Do we need to handle more than 1 includes?
        return filter(lambda x: x.type == "Data" and
                      x.include[0].phase == self.phase, layers)


def main(argv):
    if len(argv) != 2:
        raise Exception('Usage: model .prototxt file')
    else:
        n = Net(sys.argv[1])
    n.forward()

#     L1 = ConvLayer(n.param.layer[2])
#     L2 = ConvLayer(n.param.layer[6])


if __name__ == '__main__':
    import sys
    main(sys.argv)
