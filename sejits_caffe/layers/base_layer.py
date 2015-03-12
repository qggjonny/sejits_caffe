class BaseLayer(object):
    def __init__(self, param):
        self.layer_param = param
        self.blobs = []
        # TODO:  Initialize with proto blob

    def set_up(self, bottom, top):
        pass

    def forward(self, bottom, top):
        raise NotImplementedError()

    def backward(self, bottom, propagate_down, top):
        raise NotImplementedError()
