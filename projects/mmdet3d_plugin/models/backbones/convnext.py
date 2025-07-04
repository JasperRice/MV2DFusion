# Copyright (c) OpenMMLab. All rights reserved.
import json
from functools import partial
from itertools import chain
from typing import Sequence

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.checkpoint as cp
from mmcv.cnn import NORM_LAYERS, build_activation_layer, build_norm_layer
from mmcv.cnn.bricks import DropPath
from mmcv.runner import (BaseModule, DefaultOptimizerConstructor, ModuleList,
                         Sequential, get_dist_info)
from mmcv.runner.optimizer import OPTIMIZER_BUILDERS
from mmdet.models import BACKBONES
from mmdet.utils import get_root_logger


@NORM_LAYERS.register_module("ConvNeXtLN2d")
class LayerNorm2d(nn.LayerNorm):
    """LayerNorm on channels for 2d images.

    Args:
        num_channels (int): The number of channels of the input tensor.
        eps (float): a value added to the denominator for numerical stability.
            Defaults to 1e-5.
        elementwise_affine (bool): a boolean value that when set to ``True``,
            this module has learnable per-element affine parameters initialized
            to ones (for weights) and zeros (for biases). Defaults to True.
    """

    def __init__(self, num_channels: int, **kwargs) -> None:
        super().__init__(num_channels, **kwargs)
        self.num_channels = self.normalized_shape[0]

    def forward(self, x, data_format="channel_first"):
        """Forward method.

        Args:
            x (torch.Tensor): The input tensor.
            data_format (str): The format of the input tensor. If
                ``"channel_first"``, the shape of the input tensor should be
                (B, C, H, W). If ``"channel_last"``, the shape of the input
                tensor should be (B, H, W, C). Defaults to "channel_first".
        """
        assert x.dim() == 4, (
            "LayerNorm2d only supports inputs with shape "
            f"(N, C, H, W), but got tensor with shape {x.shape}"
        )
        if data_format == "channel_last":
            x = F.layer_norm(x, self.normalized_shape, self.weight, self.bias, self.eps)
        elif data_format == "channel_first":
            x = x.permute(0, 2, 3, 1)
            x = F.layer_norm(x, self.normalized_shape, self.weight, self.bias, self.eps)
            # If the output is discontiguous, it may cause some unexpected
            # problem in the downstream tasks
            x = x.permute(0, 3, 1, 2).contiguous()
        return x


class GRN(nn.Module):
    """Global Response Normalization Module.

    Come from `ConvNeXt V2: Co-designing and Scaling ConvNets with Masked
    Autoencoders <http://arxiv.org/abs/2301.00808>`_

    Args:
        in_channels (int): The number of channels of the input tensor.
        eps (float): a value added to the denominator for numerical stability.
            Defaults to 1e-6.
    """

    def __init__(self, in_channels, eps=1e-6):
        super().__init__()
        self.in_channels = in_channels
        self.gamma = nn.Parameter(torch.zeros(in_channels))
        self.beta = nn.Parameter(torch.zeros(in_channels))
        self.eps = eps

    def forward(self, x: torch.Tensor, data_format="channel_first"):
        """Forward method.

        Args:
            x (torch.Tensor): The input tensor.
            data_format (str): The format of the input tensor. If
                ``"channel_first"``, the shape of the input tensor should be
                (B, C, H, W). If ``"channel_last"``, the shape of the input
                tensor should be (B, H, W, C). Defaults to "channel_first".
        """
        if data_format == "channel_last":
            gx = torch.norm(x, p=2, dim=(1, 2), keepdim=True)
            nx = gx / (gx.mean(dim=-1, keepdim=True) + self.eps)
            x = self.gamma * (x * nx) + self.beta + x
        elif data_format == "channel_first":
            gx = torch.norm(x, p=2, dim=(2, 3), keepdim=True)
            nx = gx / (gx.mean(dim=1, keepdim=True) + self.eps)
            x = (
                self.gamma.view(1, -1, 1, 1) * (x * nx)
                + self.beta.view(1, -1, 1, 1)
                + x
            )
        return x


class ConvNeXtBlock(BaseModule):
    """ConvNeXt Block.

    Args:
        in_channels (int): The number of input channels.
        dw_conv_cfg (dict): Config of depthwise convolution.
            Defaults to ``dict(kernel_size=7, padding=3)``.
        norm_cfg (dict): The config dict for norm layers.
            Defaults to ``dict(type='LN2d', eps=1e-6)``.
        act_cfg (dict): The config dict for activation between pointwise
            convolution. Defaults to ``dict(type='GELU')``.
        mlp_ratio (float): The expansion ratio in both pointwise convolution.
            Defaults to 4.
        linear_pw_conv (bool): Whether to use linear layer to do pointwise
            convolution. More details can be found in the note.
            Defaults to True.
        drop_path_rate (float): Stochastic depth rate. Defaults to 0.
        layer_scale_init_value (float): Init value for Layer Scale.
            Defaults to 1e-6.

    Note:
        There are two equivalent implementations:

        1. DwConv -> LayerNorm -> 1x1 Conv -> GELU -> 1x1 Conv;
           all outputs are in (N, C, H, W).
        2. DwConv -> LayerNorm -> Permute to (N, H, W, C) -> Linear -> GELU
           -> Linear; Permute back

        As default, we use the second to align with the official repository.
        And it may be slightly faster.
    """

    def __init__(
        self,
        in_channels,
        dw_conv_cfg=dict(kernel_size=7, padding=3),
        norm_cfg=dict(type="ConvNeXtLN2d", eps=1e-6),
        act_cfg=dict(type="GELU"),
        mlp_ratio=4.0,
        linear_pw_conv=True,
        drop_path_rate=0.0,
        layer_scale_init_value=1e-6,
        use_grn=False,
        with_cp=False,
    ):
        super().__init__()
        self.with_cp = with_cp

        self.depthwise_conv = nn.Conv2d(
            in_channels, in_channels, groups=in_channels, **dw_conv_cfg
        )

        self.linear_pw_conv = linear_pw_conv
        self.norm = build_norm_layer(norm_cfg, in_channels)[1]

        mid_channels = int(mlp_ratio * in_channels)
        if self.linear_pw_conv:
            # Use linear layer to do pointwise conv.
            pw_conv = nn.Linear
        else:
            pw_conv = partial(nn.Conv2d, kernel_size=1)

        self.pointwise_conv1 = pw_conv(in_channels, mid_channels)
        self.act = build_activation_layer(act_cfg)
        self.pointwise_conv2 = pw_conv(mid_channels, in_channels)

        if use_grn:
            self.grn = GRN(mid_channels)
        else:
            self.grn = None

        self.gamma = (
            nn.Parameter(
                layer_scale_init_value * torch.ones((in_channels)), requires_grad=True
            )
            if layer_scale_init_value > 0
            else None
        )

        self.drop_path = (
            DropPath(drop_path_rate) if drop_path_rate > 0.0 else nn.Identity()
        )

    def forward(self, x):

        def _inner_forward(x):
            shortcut = x
            x = self.depthwise_conv(x)

            if self.linear_pw_conv:
                x = x.permute(0, 2, 3, 1)  # (N, C, H, W) -> (N, H, W, C)
                x = self.norm(x, data_format="channel_last")
                x = self.pointwise_conv1(x)
                x = self.act(x)
                if self.grn is not None:
                    x = self.grn(x, data_format="channel_last")
                x = self.pointwise_conv2(x)
                x = x.permute(0, 3, 1, 2)  # (N, H, W, C) -> (N, C, H, W)
            else:
                x = self.norm(x, data_format="channel_first")
                x = self.pointwise_conv1(x)
                x = self.act(x)

                if self.grn is not None:
                    x = self.grn(x, data_format="channel_first")
                x = self.pointwise_conv2(x)

            if self.gamma is not None:
                x = x.mul(self.gamma.view(1, -1, 1, 1))

            x = shortcut + self.drop_path(x)
            return x

        if self.with_cp and x.requires_grad:
            x = cp.checkpoint(_inner_forward, x)
        else:
            x = _inner_forward(x)
        return x


@BACKBONES.register_module()
class ConvNeXt(BaseModule):
    """ConvNeXt v1&v2 backbone.

    A PyTorch implementation of `A ConvNet for the 2020s
    <https://arxiv.org/abs/2201.03545>`_ and
    `ConvNeXt V2: Co-designing and Scaling ConvNets with Masked Autoencoders
    <http://arxiv.org/abs/2301.00808>`_

    Modified from the `official repo
    <https://github.com/facebookresearch/ConvNeXt/blob/main/models/convnext.py>`_
    and `timm
    <https://github.com/rwightman/pytorch-image-models/blob/master/timm/models/convnext.py>`_.

    To use ConvNeXt v2, please set ``use_grn=True`` and ``layer_scale_init_value=0.``.

    Args:
        arch (str | dict): The model's architecture. If string, it should be
            one of architecture in ``ConvNeXt.arch_settings``. And if dict, it
            should include the following two keys:

            - depths (list[int]): Number of blocks at each stage.
            - channels (list[int]): The number of channels at each stage.

            Defaults to 'tiny'.
        in_channels (int): Number of input image channels. Defaults to 3.
        stem_patch_size (int): The size of one patch in the stem layer.
            Defaults to 4.
        norm_cfg (dict): The config dict for norm layers.
            Defaults to ``dict(type='LN2d', eps=1e-6)``.
        act_cfg (dict): The config dict for activation between pointwise
            convolution. Defaults to ``dict(type='GELU')``.
        linear_pw_conv (bool): Whether to use linear layer to do pointwise
            convolution. Defaults to True.
        use_grn (bool): Whether to add Global Response Normalization in the
            blocks. Defaults to False.
        drop_path_rate (float): Stochastic depth rate. Defaults to 0.
        layer_scale_init_value (float): Init value for Layer Scale.
            Defaults to 1e-6.
        out_indices (Sequence | int): Output from which stages.
            Defaults to -1, means the last stage.
        frozen_stages (int): Stages to be frozen (all param fixed).
            Defaults to 0, which means not freezing any parameters.
        gap_before_final_norm (bool): Whether to globally average the feature
            map before the final norm layer. In the official repo, it's only
            used in classification task. Defaults to True.
        with_cp (bool): Use checkpoint or not. Using checkpoint will save some
            memory while slowing down the training speed. Defaults to False.
        init_cfg (dict, optional): Initialization config dict
    """  # noqa: E501

    arch_settings = {
        "atto": {"depths": [2, 2, 6, 2], "channels": [40, 80, 160, 320]},
        "femto": {"depths": [2, 2, 6, 2], "channels": [48, 96, 192, 384]},
        "pico": {"depths": [2, 2, 6, 2], "channels": [64, 128, 256, 512]},
        "nano": {"depths": [2, 2, 8, 2], "channels": [80, 160, 320, 640]},
        "tiny": {"depths": [3, 3, 9, 3], "channels": [96, 192, 384, 768]},
        "small": {"depths": [3, 3, 27, 3], "channels": [96, 192, 384, 768]},
        "base": {"depths": [3, 3, 27, 3], "channels": [128, 256, 512, 1024]},
        "large": {"depths": [3, 3, 27, 3], "channels": [192, 384, 768, 1536]},
        "xlarge": {"depths": [3, 3, 27, 3], "channels": [256, 512, 1024, 2048]},
        "huge": {"depths": [3, 3, 27, 3], "channels": [352, 704, 1408, 2816]},
    }

    def __init__(
        self,
        arch="tiny",
        in_channels=3,
        stem_patch_size=4,
        norm_cfg=dict(type="ConvNeXtLN2d", eps=1e-6),
        act_cfg=dict(type="GELU"),
        linear_pw_conv=True,
        use_grn=False,
        drop_path_rate=0.0,
        layer_scale_init_value=1e-6,
        out_indices=-1,
        frozen_stages=0,
        gap_before_final_norm=True,
        with_cp=False,
        init_cfg=[
            dict(type="TruncNormal", layer=["Conv2d", "Linear"], std=0.02, bias=0.0),
            dict(type="Constant", layer=["LayerNorm"], val=1.0, bias=0.0),
        ],
    ):
        super().__init__(init_cfg=init_cfg)

        if isinstance(arch, str):
            assert arch in self.arch_settings, (
                f"Unavailable arch, please choose from "
                f"({set(self.arch_settings)}) or pass a dict."
            )
            arch = self.arch_settings[arch]
        elif isinstance(arch, dict):
            assert "depths" in arch and "channels" in arch, (
                f'The arch dict must have "depths" and "channels", '
                f"but got {list(arch.keys())}."
            )

        self.depths = arch["depths"]
        self.channels = arch["channels"]
        assert (
            isinstance(self.depths, Sequence)
            and isinstance(self.channels, Sequence)
            and len(self.depths) == len(self.channels)
        ), (
            f'The "depths" ({self.depths}) and "channels" ({self.channels}) '
            "should be both sequence with the same length."
        )

        self.num_stages = len(self.depths)

        if isinstance(out_indices, int):
            out_indices = [out_indices]
        assert isinstance(out_indices, Sequence), (
            f'"out_indices" must by a sequence or int, '
            f"get {type(out_indices)} instead."
        )
        for i, index in enumerate(out_indices):
            if index < 0:
                out_indices[i] = 4 + index
                assert out_indices[i] >= 0, f"Invalid out_indices {index}"
        self.out_indices = out_indices

        self.frozen_stages = frozen_stages
        self.gap_before_final_norm = gap_before_final_norm

        # stochastic depth decay rule
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(self.depths))]
        block_idx = 0

        # 4 downsample layers between stages, including the stem layer.
        self.downsample_layers = ModuleList()
        stem = nn.Sequential(
            nn.Conv2d(
                in_channels,
                self.channels[0],
                kernel_size=stem_patch_size,
                stride=stem_patch_size,
            ),
            build_norm_layer(norm_cfg, self.channels[0])[1],
        )
        self.downsample_layers.append(stem)

        # 4 feature resolution stages, each consisting of multiple residual
        # blocks
        self.stages = nn.ModuleList()

        for i in range(self.num_stages):
            depth = self.depths[i]
            channels = self.channels[i]

            if i >= 1:
                downsample_layer = nn.Sequential(
                    build_norm_layer(norm_cfg, self.channels[i - 1])[1],
                    nn.Conv2d(self.channels[i - 1], channels, kernel_size=2, stride=2),
                )
                self.downsample_layers.append(downsample_layer)

            stage = Sequential(
                *[
                    ConvNeXtBlock(
                        in_channels=channels,
                        drop_path_rate=dpr[block_idx + j],
                        norm_cfg=norm_cfg,
                        act_cfg=act_cfg,
                        linear_pw_conv=linear_pw_conv,
                        layer_scale_init_value=layer_scale_init_value,
                        use_grn=use_grn,
                        with_cp=with_cp,
                    )
                    for j in range(depth)
                ]
            )
            block_idx += depth

            self.stages.append(stage)

            if i in self.out_indices:
                norm_layer = build_norm_layer(norm_cfg, channels)[1]
                self.add_module(f"norm{i}", norm_layer)

        self._freeze_stages()

    def forward(self, x):
        outs = []
        for i, stage in enumerate(self.stages):
            x = self.downsample_layers[i](x)
            x = stage(x)
            if i in self.out_indices:
                norm_layer = getattr(self, f"norm{i}")
                if self.gap_before_final_norm:
                    gap = x.mean([-2, -1], keepdim=True)
                    outs.append(norm_layer(gap).flatten(1))
                else:
                    outs.append(norm_layer(x))

        return tuple(outs)

    def _freeze_stages(self):
        for i in range(self.frozen_stages):
            downsample_layer = self.downsample_layers[i]
            stage = self.stages[i]
            downsample_layer.eval()
            stage.eval()
            for param in chain(downsample_layer.parameters(), stage.parameters()):
                param.requires_grad = False

    def train(self, mode=True):
        super(ConvNeXt, self).train(mode)
        self._freeze_stages()

    def get_layer_depth(self, param_name: str, prefix: str = ""):
        """Get the layer-wise depth of a parameter.

        Args:
            param_name (str): The name of the parameter.
            prefix (str): The prefix for the parameter.
                Defaults to an empty string.

        Returns:
            Tuple[int, int]: The layer-wise depth and the num of layers.
        """

        max_layer_id = 12 if self.depths[-2] > 9 else 6

        if not param_name.startswith(prefix):
            # For subsequent module like head
            return max_layer_id + 1, max_layer_id + 2

        param_name = param_name[len(prefix) :]
        if param_name.startswith("downsample_layers"):
            stage_id = int(param_name.split(".")[1])
            if stage_id == 0:
                layer_id = 0
            elif stage_id == 1 or stage_id == 2:
                layer_id = stage_id + 1
            else:  # stage_id == 3:
                layer_id = max_layer_id

        elif param_name.startswith("stages"):
            stage_id = int(param_name.split(".")[1])
            block_id = int(param_name.split(".")[2])
            if stage_id == 0 or stage_id == 1:
                layer_id = stage_id + 1
            elif stage_id == 2:
                layer_id = 3 + block_id // 3
            else:  # stage_id == 3:
                layer_id = max_layer_id

        # final norm layer
        else:
            layer_id = max_layer_id + 1

        return layer_id, max_layer_id + 2


def get_layer_id_for_convnext(var_name, max_layer_id):
    """Get the layer id to set the different learning rates in ``layer_wise``
    decay_type.

    Args:
        var_name (str): The key of the model.
        max_layer_id (int): Maximum layer id.

    Returns:
        int: The id number corresponding to different learning rate in
        ``LearningRateDecayOptimizerConstructor``.
    """

    if var_name in (
        "img_backbone.cls_token",
        "img_backbone.mask_token",
        "img_backbone.pos_embed",
    ):
        return 0
    elif var_name.startswith("img_backbone.downsample_layers"):
        stage_id = int(var_name.split(".")[2])
        if stage_id == 0:
            layer_id = 0
        elif stage_id == 1:
            layer_id = 2
        elif stage_id == 2:
            layer_id = 3
        elif stage_id == 3:
            layer_id = max_layer_id
        return layer_id
    elif var_name.startswith("img_backbone.stages"):
        stage_id = int(var_name.split(".")[2])
        block_id = int(var_name.split(".")[3])
        if stage_id == 0:
            layer_id = 1
        elif stage_id == 1:
            layer_id = 2
        elif stage_id == 2:
            layer_id = 3 + block_id // 3
        elif stage_id == 3:
            layer_id = max_layer_id
        return layer_id
    else:
        return max_layer_id + 1


def get_stage_id_for_convnext(var_name, max_stage_id):
    """Get the stage id to set the different learning rates in ``stage_wise``
    decay_type.

    Args:
        var_name (str): The key of the model.
        max_stage_id (int): Maximum stage id.

    Returns:
        int: The id number corresponding to different learning rate in
        ``LearningRateDecayOptimizerConstructor``.
    """

    if var_name in (
        "img_backbone.cls_token",
        "img_backbone.mask_token",
        "img_backbone.pos_embed",
    ):
        return 0
    elif var_name.startswith("img_backbone.downsample_layers"):
        return 0
    elif var_name.startswith("img_backbone.stages"):
        stage_id = int(var_name.split(".")[2])
        return stage_id + 1
    else:
        return max_stage_id - 1


@OPTIMIZER_BUILDERS.register_module()
class ConvNeXtLearningRateDecayOptimizerConstructor(DefaultOptimizerConstructor):
    # Different learning rates are set for different layers of backbone.
    # Note: Currently, this optimizer constructor is built for ConvNeXt.

    def add_params(self, params, module, **kwargs):
        """Add all parameters of module to the params list.

        The parameters of the given module will be added to the list of param
        groups, with specific rules defined by paramwise_cfg.

        Args:
            params (list[dict]): A list of param groups, it will be modified
                in place.
            module (nn.Module): The module to be added.
        """
        logger = get_root_logger()

        # get param-wise options
        custom_keys = self.paramwise_cfg.get("custom_keys", {})
        # first sort with alphabet order and then sort with reversed len of str
        sorted_keys = sorted(sorted(custom_keys.keys()), key=len, reverse=True)

        parameter_groups = {}
        logger.info(f"self.paramwise_cfg is {self.paramwise_cfg}")
        num_layers = self.paramwise_cfg.get("num_layers") + 2
        decay_rate = self.paramwise_cfg.get("decay_rate")
        decay_type = self.paramwise_cfg.get("decay_type", "layer_wise")
        logger.info(
            "Build LearningRateDecayOptimizerConstructor  "
            f"{decay_type} {decay_rate} - {num_layers}"
        )
        weight_decay = self.base_wd
        for name, param in module.named_parameters():
            if not param.requires_grad:
                continue  # frozen weights
            if (
                len(param.shape) == 1
                or name.endswith(".bias")
                or name in ("pos_embed", "cls_token")
            ):
                group_name = "no_decay"
                this_weight_decay = 0.0
            else:
                group_name = "decay"
                this_weight_decay = weight_decay
            if "layer_wise" in decay_type:
                if "ConvNeXt" in module.img_backbone.__class__.__name__:
                    layer_id = get_layer_id_for_convnext(
                        name, self.paramwise_cfg.get("num_layers")
                    )
                    logger.info(f"set param {name} as id {layer_id}")
                else:
                    raise NotImplementedError()
            elif decay_type == "stage_wise":
                if "ConvNeXt" in module.img_backbone.__class__.__name__:
                    layer_id = get_stage_id_for_convnext(name, num_layers)
                    logger.info(f"set param {name} as id {layer_id}")
                else:
                    raise NotImplementedError()
            group_name = f"layer_{layer_id}_{group_name}"

            if group_name not in parameter_groups:
                scale = decay_rate ** (num_layers - layer_id - 1)

                parameter_groups[group_name] = {
                    "weight_decay": this_weight_decay,
                    "params": [],
                    "param_names": [],
                    "lr_scale": scale,
                    "group_name": group_name,
                    "lr": scale * self.base_lr,
                }

            parameter_groups[group_name]["params"].append(param)
            parameter_groups[group_name]["param_names"].append(name)

        new_parameter_groups = dict()
        for param_g in parameter_groups.values():
            weight_decay = param_g["weight_decay"]
            lr_scale = param_g["lr_scale"]
            lr = param_g["lr"]
            for p, p_name in zip(param_g["params"], param_g["param_names"]):
                name_with_custom_keys = False
                for key in sorted_keys:
                    if key in p_name:
                        name_with_custom_keys = True
                        lr_mult = custom_keys[key].get("lr_mult", 1.0)
                        custom_lr = lr * lr_mult
                        custom_lr_scale = lr_scale * lr_mult
                        if self.base_wd is not None:
                            decay_mult = custom_keys[key].get("decay_mult", 1.0)
                            custom_weight_decay = weight_decay * decay_mult
                        else:
                            custom_weight_decay = weight_decay
                        break
                if not name_with_custom_keys:
                    custom_lr = lr
                    custom_lr_scale = lr_scale
                    custom_weight_decay = weight_decay

                new_parameter_groups[p_name] = {
                    "weight_decay": custom_weight_decay,
                    "params": [p],
                    "param_names": [p_name],
                    "lr_scale": custom_lr_scale,
                    "group_name": p_name,
                    "lr": custom_lr,
                }
        parameter_groups = new_parameter_groups

        rank, _ = get_dist_info()
        if rank == 0:
            to_display = {}
            for key in parameter_groups:
                to_display[key] = {
                    "param_names": parameter_groups[key]["param_names"],
                    "lr_scale": parameter_groups[key]["lr_scale"],
                    "lr": parameter_groups[key]["lr"],
                    "weight_decay": parameter_groups[key]["weight_decay"],
                }
            logger.info(f"Param groups = {json.dumps(to_display, indent=2)}")
        params.extend(parameter_groups.values())
