# ---------------------------------------------
# Copyright (c) OpenMMLab. All rights reserved.
# ---------------------------------------------
#  Modified by Zhiqi Li
# ---------------------------------------------
#  Modified by Shihao Wang
# ---------------------------------------------
from mmcv.utils.registry import Registry, build_from_cfg

SAMPLER = Registry("sampler")


def build_sampler(cfg, default_args):
    return build_from_cfg(cfg, SAMPLER, default_args)
