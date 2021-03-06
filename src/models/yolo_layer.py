"""
# -*- coding: utf-8 -*-
-----------------------------------------------------------------------------------
# Author: Nguyen Mau Dung
# DoC: 2020.07.05
# email: nguyenmaudung93.kstn@gmail.com
-----------------------------------------------------------------------------------
# Description: This script for the yolo layer

# Refer: https://github.com/Tianxiaomo/pytorch-YOLOv4
# Refer: https://github.com/VCasecnikovs/Yet-Another-YOLOv4-Pytorch
"""

import sys

import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.append('../')

from utils.torch_utils import to_cpu
from utils.iou_rotated_boxes_utils import iou_pred_vs_target_boxes, get_polygons_fix_xy, iou_rotated_boxes_vs_anchors


class YoloLayer(nn.Module):
    """Yolo layer"""

    def __init__(self, num_classes, anchors, stride, scale_x_y, ignore_thresh):
        super(YoloLayer, self).__init__()
        # Update the attributions when parsing the cfg during create the darknet
        self.num_classes = num_classes
        self.anchors = anchors
        self.num_anchors = len(anchors)
        self.stride = stride
        self.scale_x_y = scale_x_y
        self.ignore_thresh = ignore_thresh

        self.noobj_scale = 100
        self.obj_scale = 1
        # self.lbox_scale = 3.54
        # self.lobj_scale = 64.3
        # self.lcls_scale = 37.4
        self.lbox_scale = 1.
        self.lobj_scale = 1.
        self.lcls_scale = 1.

        self.seen = 0
        # Initialize dummy variables
        self.grid_size = 0
        self.img_size = 0
        self.metrics = {}

    def compute_grid_offsets(self, grid_size):
        self.grid_size = grid_size
        g = self.grid_size
        self.stride = self.img_size / self.grid_size
        # Calculate offsets for each grid
        self.grid_x = torch.arange(g, device=self.device, dtype=torch.float).repeat(g, 1).view([1, 1, g, g])
        self.grid_y = torch.arange(g, device=self.device, dtype=torch.float).repeat(g, 1).t().view([1, 1, g, g])
        self.scaled_anchors = torch.tensor(
            [(a_w / self.stride, a_h / self.stride, im, re) for a_w, a_h, im, re in self.anchors], device=self.device,
            dtype=torch.float)
        self.anchor_w = self.scaled_anchors[:, 0:1].view((1, self.num_anchors, 1, 1))
        self.anchor_h = self.scaled_anchors[:, 1:2].view((1, self.num_anchors, 1, 1))

        # Pre compute polygons and areas of anchors
        self.scaled_anchors_polygons = get_polygons_fix_xy(to_cpu(self.scaled_anchors).numpy(), fix_xy=100)
        self.scaled_anchors_areas = [polygon_.area for polygon_ in self.scaled_anchors_polygons]

    def build_targets(self, out_boxes, pred_cls, target, anchors):
        """ Built yolo targets to compute loss
        :param out_boxes: [num_samples or batch, num_anchors, grid_size, grid_size, 6]
        :param pred_cls: [num_samples or batch, num_anchors, grid_size, grid_size, num_classes]
        :param target: [num_boxes, 8]
        :param anchors: [num_anchors, 4]
        :return:
        """
        nB, nA, nG, _, nC = pred_cls.size()
        n_target_boxes = target.size(0)

        # Create output tensors on "device"
        obj_mask = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.uint8)
        noobj_mask = torch.full(size=(nB, nA, nG, nG), fill_value=1, device=self.device, dtype=torch.uint8)
        class_mask = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        iou_scores = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        tx = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        ty = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        tw = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        th = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        tim = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        tre = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        tcls = torch.full(size=(nB, nA, nG, nG, nC), fill_value=0, device=self.device, dtype=torch.float)
        tconf = obj_mask.float()

        if n_target_boxes > 0:  # Make sure that there is at least 1 box
            # Convert to position relative to box
            target_boxes = target[:, 2:8]

            gxy = target_boxes[:, :2] * nG  # scale up x, y
            gwh = target_boxes[:, 2:4] * nG  # scale up w, l
            gimre = target_boxes[:, 4:]

            targets_polygons = get_polygons_fix_xy(to_cpu(target_boxes[:, 2:6] * nG).numpy(), fix_xy=100)
            targets_areas = [polygon_.area for polygon_ in targets_polygons]

            # Get anchors with best iou
            ious = iou_rotated_boxes_vs_anchors(self.scaled_anchors_polygons, self.scaled_anchors_areas,
                                                targets_polygons, targets_areas)
            best_ious, best_n = ious.max(0)

            b, target_labels = target[:, :2].long().t()

            gx, gy = gxy.t()
            gw, gh = gwh.t()
            gim, gre = gimre.t()
            gi, gj = gxy.long().t()
            # Set masks
            obj_mask[b, best_n, gj, gi] = 1
            noobj_mask[b, best_n, gj, gi] = 0

            # Set noobj mask to zero where iou exceeds ignore threshold
            for i, anchor_ious in enumerate(ious.t()):
                noobj_mask[b[i], anchor_ious > self.ignore_thresh, gj[i], gi[i]] = 0

            # Coordinates
            tx[b, best_n, gj, gi] = gx - gx.floor()
            ty[b, best_n, gj, gi] = gy - gy.floor()
            # Width and height
            tw[b, best_n, gj, gi] = torch.log(gw / anchors[best_n][:, 0] + 1e-16)
            th[b, best_n, gj, gi] = torch.log(gh / anchors[best_n][:, 1] + 1e-16)
            # Im and real part
            tim[b, best_n, gj, gi] = gim
            tre[b, best_n, gj, gi] = gre

            # One-hot encoding of label
            tcls[b, best_n, gj, gi, target_labels] = 1
            class_mask[b, best_n, gj, gi] = (pred_cls[b, best_n, gj, gi].argmax(-1) == target_labels).float()
            iou_scores[b, best_n, gj, gi] = iou_pred_vs_target_boxes(out_boxes[b, best_n, gj, gi], target_boxes, nG)
            tconf = obj_mask.float()

        return iou_scores, class_mask, obj_mask.type(torch.bool), noobj_mask.type(torch.bool), \
               tx, ty, tw, th, tim, tre, tcls, tconf

    def forward(self, x, targets=None, img_size=608):
        """
        :param x: [num_samples or batch, num_anchors * (6 + 1 + num_classes), grid_size, grid_size]
        :param targets: [num boxes, 8] (box_idx, class, x, y, w, l, sin(yaw), cos(yaw))
        :param img_size: default 608
        :return:
        """
        self.img_size = img_size
        self.device = x.device
        num_samples, _, _, grid_size = x.size()

        prediction = x.view(num_samples, self.num_anchors, self.num_classes + 7, grid_size, grid_size)
        prediction = prediction.permute(0, 1, 3, 4, 2).contiguous()
        # prediction size: [num_samples, num_anchors, grid_size, grid_size, num_classes + 7]

        # Get outputs
        pred_x = torch.sigmoid(prediction[..., 0])
        pred_y = torch.sigmoid(prediction[..., 1])
        pred_w = prediction[..., 2]  # Width
        pred_h = prediction[..., 3]  # Height
        pred_im = prediction[..., 4]  # angle imaginary part
        pred_re = prediction[..., 5]  # angle real part
        pred_conf = torch.sigmoid(prediction[..., 6])  # Conf
        pred_cls = torch.sigmoid(prediction[..., 7:])  # Cls pred.

        # If grid size does not match current we compute new offsets
        if grid_size != self.grid_size:
            self.compute_grid_offsets(grid_size)

        # Add offset and scale with anchors
        # pred_boxes size: [num_samples, num_anchors, grid_size, grid_size, 6]
        out_boxes = torch.empty(prediction[..., :6].shape, device=self.device, dtype=torch.float)
        out_boxes[..., 0] = pred_x.clone().detach() + self.grid_x
        out_boxes[..., 1] = pred_y.clone().detach() + self.grid_y
        out_boxes[..., 2] = torch.exp(pred_w.clone().detach()) * self.anchor_w
        out_boxes[..., 3] = torch.exp(pred_h.clone().detach()) * self.anchor_h
        out_boxes[..., 4] = pred_im.clone().detach()
        out_boxes[..., 5] = pred_re.clone().detach()

        output = torch.cat((
            out_boxes[..., :4].view(num_samples, -1, 4) * self.stride,
            out_boxes[..., 4:6].view(num_samples, -1, 2),
            pred_conf.clone().view(num_samples, -1, 1),
            pred_cls.clone().view(num_samples, -1, self.num_classes),
        ), dim=-1)
        # output size: [num_samples, num boxes, 7 + num_classes]

        if targets is None:
            return output, 0
        else:
            reduction = 'mean'
            iou_scores, class_mask, obj_mask, noobj_mask, tx, ty, tw, th, tim, tre, tcls, tconf = self.build_targets(
                out_boxes=out_boxes, pred_cls=pred_cls, target=targets, anchors=self.scaled_anchors)

            iou_masked = iou_scores[obj_mask]  # size: (n_target_boxes,)
            loss_box = (1. - iou_masked).sum() if reduction == 'sum' else (1. - iou_masked).mean()

            loss_conf_obj = F.binary_cross_entropy(pred_conf[obj_mask], tconf[obj_mask], reduction=reduction)
            loss_conf_noobj = F.binary_cross_entropy(pred_conf[noobj_mask], tconf[noobj_mask], reduction=reduction)
            loss_obj = self.obj_scale * loss_conf_obj + self.noobj_scale * loss_conf_noobj
            loss_cls = F.binary_cross_entropy(pred_cls[obj_mask], tcls[obj_mask], reduction=reduction)
            total_loss = loss_box * self.lbox_scale + loss_obj * self.lobj_scale + loss_cls * self.lcls_scale

            # Metrics (store loss values using tensorboard)
            self.metrics = {
                "loss": to_cpu(total_loss).item(),
                'loss_box': to_cpu(loss_box).item(),
                "loss_obj": to_cpu(loss_obj).item(),
                "loss_cls": to_cpu(loss_cls).item()
            }

            return output, total_loss
