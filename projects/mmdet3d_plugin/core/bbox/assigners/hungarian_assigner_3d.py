# ------------------------------------------------------------------------
# Modified from DETR3D (https://github.com/WangYueFt/detr3d)
# Copyright (c) 2021 Wang, Yue
# ------------------------------------------------------------------------
import torch
from mmdet.core.bbox.assigners import AssignResult, BaseAssigner
from mmdet.core.bbox.builder import BBOX_ASSIGNERS
from mmdet.core.bbox.match_costs import build_match_cost

from projects.mmdet3d_plugin.core.bbox.util import normalize_bbox

try:
    from scipy.optimize import linear_sum_assignment
except ImportError:
    linear_sum_assignment = None


@BBOX_ASSIGNERS.register_module()
class HungarianAssigner3D(BaseAssigner):
    def __init__(
        self,
        cls_cost=dict(type="ClassificationCost", weight=1.0),
        reg_cost=dict(type="BBoxL1Cost", weight=1.0),
        iou_cost=dict(type="IoUCost", weight=0.0),
        pc_range=None,
        debug=False,
        stats_one2one_matching=0,
    ):
        self.cls_cost = build_match_cost(cls_cost)
        self.reg_cost = build_match_cost(reg_cost)
        self.iou_cost = build_match_cost(iou_cost)
        self.pc_range = pc_range
        self.debug = debug
        self.stats_one2one_matching = stats_one2one_matching
        self.layer_indicator = 0

    def assign(
        self,
        bbox_pred,
        cls_pred,
        gt_bboxes,
        gt_labels,
        gt_bboxes_ignore=None,
        code_weights=None,
        with_velo=False,
        eps=1e-7,
    ):
        assert (
            gt_bboxes_ignore is None
        ), "Only case when gt_bboxes_ignore is None is supported."
        num_gts, num_bboxes = gt_bboxes.size(0), bbox_pred.size(0)
        # 1. assign -1 by default
        assigned_gt_inds = bbox_pred.new_full((num_bboxes,), -1, dtype=torch.long)
        assigned_labels = bbox_pred.new_full((num_bboxes,), -1, dtype=torch.long)
        if num_gts == 0 or num_bboxes == 0:
            # No ground truth or boxes, return empty assignment
            if num_gts == 0:
                # No ground truth, assign all to background
                assigned_gt_inds[:] = 0
            return AssignResult(num_gts, assigned_gt_inds, None, labels=assigned_labels)
        # 2. compute the weighted costs
        # classification and bboxcost.
        cls_cost = self.cls_cost(cls_pred, gt_labels)
        # regression L1 cost
        normalized_gt_bboxes = normalize_bbox(gt_bboxes, self.pc_range)
        if code_weights is not None:
            bbox_pred = bbox_pred * code_weights
            normalized_gt_bboxes = normalized_gt_bboxes * code_weights

        if with_velo:
            reg_cost = self.reg_cost(bbox_pred, normalized_gt_bboxes)
        else:
            reg_cost = self.reg_cost(bbox_pred[:, :8], normalized_gt_bboxes[:, :8])

        # weighted sum of above two costs
        cost = cls_cost + reg_cost

        if self.debug:
            import os

            import numpy as np

            pred_center = bbox_pred[:, [0, 1, 2]]
            gt_center = normalized_gt_bboxes[:, [0, 1, 2]]
            center_dist = torch.cdist(pred_center, gt_center, p=2)
            name = f"{center_dist.size(0)}_{center_dist.size(1)}_{center_dist.data.flatten()[[0, -1]].tolist()}"
            cost_np = cost.detach().cpu().numpy()
            center_dist = center_dist.detach().cpu().numpy()
            assign_np = assigned_gt_inds.detach().cpu().numpy()
            cost_save = np.array(
                {
                    "cost": cost_np,
                    "dist": center_dist,
                    "assign": assign_np,
                    "gt_center": gt_center.detach().cpu().numpy(),
                    "pred_center": pred_center.detach().cpu().numpy(),
                },
                dtype=object,
            )
            os.makedirs(self.debug, exist_ok=True)
            np.save(os.path.join(self.debug, name + ".npy"), cost_save)

        # 3. do Hungarian matching on CPU using linear_sum_assignment
        cost = cost.detach().cpu()
        if linear_sum_assignment is None:
            raise ImportError(
                'Please run "pip install scipy" ' "to install scipy first."
            )
        cost = torch.nan_to_num(cost, nan=100.0, posinf=100.0, neginf=-100.0)
        matched_row_inds, matched_col_inds = linear_sum_assignment(cost)
        matched_row_inds = torch.from_numpy(matched_row_inds).to(bbox_pred.device)
        matched_col_inds = torch.from_numpy(matched_col_inds).to(bbox_pred.device)

        if self.stats_one2one_matching > 0 and self.layer_indicator > 0:
            num_prop = self.stats_one2one_matching
            num_q = assigned_gt_inds.size(0) - num_prop
            num_q_matched = (matched_row_inds < num_q).sum()
            num_prop_matched = len(matched_row_inds) - num_q_matched
            name = "level_{}_stats".format(self.layer_indicator)
            if not hasattr(self, name):
                setattr(self, name, {"num_q_matched": 0, "num_prop_matched": 0})
            stats = getattr(self, name)
            stats["num_q_matched"] += num_q_matched
            stats["num_prop_matched"] += num_prop_matched
            total = stats["num_q_matched"] + stats["num_prop_matched"]
            print(
                f"level {self.layer_indicator}: q_matched->{stats['num_q_matched']/total:.2f}, prop_matched->{stats['num_prop_matched']/total:.2f}"
            )

            self.layer_indicator += 1
            # import ipdb; ipdb.set_trace()

        # 4. assign backgrounds and foregrounds
        # assign all indices to backgrounds first
        assigned_gt_inds[:] = 0
        # assign foregrounds based on matching results
        assigned_gt_inds[matched_row_inds] = matched_col_inds + 1
        assigned_labels[matched_row_inds] = gt_labels[matched_col_inds]
        return AssignResult(num_gts, assigned_gt_inds, None, labels=assigned_labels)
