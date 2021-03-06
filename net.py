# Yikang Liao <yikang.liao@tusimple.ai>
# Symbol Definition for R2Plus1D

import numpy as np
import mxnet as mx
import logging

logger = logging.getLogger(__name__)

BLOCK_CONFIG = {
    10: (1, 1, 1, 1),
    16: (2, 2, 2, 1),
    18: (2, 2, 2, 2),
    26: (2, 3, 4, 3),
    34: (3, 4, 6, 3),
}

class ModelBuilder():
    '''
    Helper class for constructing residual blocks.
    '''

    def __init__(self, no_bias, bn_mom=0.9, cudnn_tune='off', workspace=512):
        self.comp_count = 0
        self.comp_idx = 0
        self.bn_mom = bn_mom
        self.no_bias = 1 if no_bias else 0
        self.cudnn_tune = cudnn_tune
        self.workspace = workspace

    def add_spatial_temporal_conv(self, body, in_filters, out_filters, stride):
        self.comp_idx += 1

        i = 3 * in_filters * out_filters * 3 * 3
        i /= in_filters * 3 * 3 + 3 * out_filters
        middle_filters = int(i)
        logger.info("Number of middle filters: {}".format(middle_filters))

        # 1x3x3 Convolution
        body = mx.sym.Convolution(data=body, num_filter=middle_filters, kernel=(1, 3, 3), stride=(1, stride[1], stride[2]),
                                  pad=(0, 1, 1), no_bias=self.no_bias, cudnn_tune=self.cudnn_tune, workspace=self.workspace,
                                  name='comp_%d_conv_%d_middle' % (self.comp_count, self.comp_idx))

        body = mx.sym.BatchNorm(data=body, fix_gamma=False, eps=1e-3, momentum=self.bn_mom,
                                name='comp_%d_spatbn_%d_middle' % (self.comp_count, self.comp_idx))
        body = mx.sym.Activation(data=body, act_type='relu')

        # 3x1x1 Convolution
        body = mx.sym.Convolution(data=body, num_filter=out_filters, kernel=(3, 1, 1), stride=(stride[0], 1, 1),
                                       pad=(1, 0, 0), no_bias=self.no_bias, cudnn_tune=self.cudnn_tune, workspace=self.workspace,
                                       name='comp_%d_conv_%d' % (self.comp_count, self.comp_idx))
        return body

    def add_r3d_block(
            self,
            data,
            input_filters,
            num_filters,
            down_sampling=False,
            spatial_batch_norm=True,
            only_spatial_downsampling=False,
    ):
        self.comp_idx = 0
        shortcut = data

        if down_sampling:
            use_striding = [1, 2, 2] if only_spatial_downsampling else [2, 2, 2]
        else:
            use_striding = [1, 1, 1]

        # add 1*3*3 and 3*1*1 conv
        body = self.add_spatial_temporal_conv(
            data,
            input_filters,
            num_filters,
            stride=use_striding,
        )

        body = mx.sym.BatchNorm(data=body, fix_gamma=False, eps=1e-3, momentum=self.bn_mom,
                                name='comp_%d_spatbn_%d' % (self.comp_count, self.comp_idx))
        body = mx.sym.Activation(data=body, act_type='relu')

        # add 1*3*3 and 3*1*1 conv
        body = self.add_spatial_temporal_conv(
            body,
            num_filters,
            num_filters,
            stride=[1, 1, 1],
        )
        body = mx.sym.BatchNorm(data=body, fix_gamma=False, eps=1e-3, momentum=self.bn_mom,
                                name='comp_%d_spatbn_%d' % (self.comp_count, self.comp_idx))

        # Increase of dimensions, need a projection for the shortcut
        if (num_filters != input_filters) or down_sampling:
            shortcut = mx.sym.Convolution(data=shortcut, num_filter=num_filters, kernel=[1, 1, 1], stride=use_striding,
                                          no_bias=self.no_bias, name='shortcut_projection_%d' % self.comp_count)
            shortcut = mx.sym.BatchNorm(data=shortcut, fix_gamma=False, eps=1e-3,
                                        name='shortcut_projection_%d_spatbn' % self.comp_count)

        out = shortcut + body
        out = mx.sym.Activation(data=out, act_type='relu')
        # Keep track of number of high level components
        self.comp_count += 1
        return out



# 3d or (2+1)d resnets, input 3 x t*8 x 112 x 112
# the final conv output is 512 * t * 7 * 7
def create_r3d(
    num_class,
    no_bias=0,
    model_depth=18,
    final_spatial_kernel=7,
    final_temporal_kernel=1,
    bn_mom=0.9,
    cudnn_tune='off',
    workspace=512,
):
    # Begin Layers
    data = mx.sym.var('data', dtype=np.float32)
    body = mx.sym.Convolution(data=data, num_filter=45, kernel=(1, 7, 7), stride=(1, 2, 2),
                              pad=(0, 3, 3), no_bias=no_bias, cudnn_tune=cudnn_tune, workspace=workspace, name="conv1_middle")
    body = mx.sym.BatchNorm(data=body, fix_gamma=False, eps=1e-3, momentum=bn_mom, name='conv1_middle_spatbn_relu')
    body = mx.sym.Activation(data=body, act_type='relu')


    body = mx.sym.Convolution(data=body, num_filter=64, kernel=(3, 1, 1), stride=(1, 1, 1),
                              pad=(1, 0, 0), no_bias=no_bias, cudnn_tune=cudnn_tune, workspace=workspace, name="conv1")
    body = mx.sym.BatchNorm(data=body, fix_gamma=False, eps=1e-3, momentum=bn_mom, name='conv1_spatbn_relu')
    body = mx.sym.Activation(data=body, act_type='relu')

    (n1, n2, n3, n4) = BLOCK_CONFIG[model_depth]

    # Residual Blocks
    builder = ModelBuilder(no_bias=no_bias, bn_mom=bn_mom, cudnn_tune=cudnn_tune, workspace=workspace)

    # conv_2x
    for _ in range(n1):
        body = builder.add_r3d_block(body, 64, 64)

    # conv_3x
    body = builder.add_r3d_block(body, 64, 128, down_sampling=True)
    for _ in range(n2 - 1):
        body = builder.add_r3d_block(body, 128, 128)

    # conv_4x
    body = builder.add_r3d_block(body, 128, 256, down_sampling=True)
    for _ in range(n3 - 1):
        body = builder.add_r3d_block(body, 256, 256)

    # conv_5x
    body = builder.add_r3d_block(body, 256, 512, down_sampling=True)
    for _ in range(n4 - 1):
        body = builder.add_r3d_block(body, 512, 512)

    # Final Layers
    body = mx.sym.Pooling(data=body, kernel=(final_temporal_kernel, final_spatial_kernel, final_spatial_kernel),
                                stride=(1, 1, 1), pad=(0, 0, 0), pool_type='avg', name='final_pool')
    body = mx.symbol.FullyConnected(data=body, num_hidden=num_class, name='final_fc')
    label = mx.sym.var('softmax_label', dtype=np.float32)
    output = mx.sym.SoftmaxOutput(data=body, label=label, multi_output=True, use_ignore=True, normalization='null',
                                  name='softmax')
    return output


