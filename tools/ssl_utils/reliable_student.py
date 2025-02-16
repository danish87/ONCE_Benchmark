import torch

from .semi_utils import (construct_pseudo_label, construct_pseudo_label_scores,
                         load_data_to_gpu, reverse_transform)


@torch.no_grad()
def filter_pseudo_labels(pred_dicts, cfgs):
    filtered_pred_dicts = []
    for ind in range(len(pred_dicts)):
        pseudo_score = pred_dicts[ind]['pred_scores']
        pseudo_box = pred_dicts[ind]['pred_boxes']
        pseudo_label = pred_dicts[ind]['pred_labels']
        pseudo_sem_score = pred_dicts[ind]['pred_sem_scores']
        record_dict = {
            'pred_boxes': torch.empty((0, 7), device=pseudo_label.device),
            'pred_scores': torch.empty((0, 1), device=pseudo_label.device),
            'pred_labels': torch.empty((0, 1), device=pseudo_label.device),
            'pred_sem_scores': torch.empty((0, 1), device=pseudo_label.device)
            }
        if len(pseudo_label) > 0:
            conf_thresh = torch.tensor(cfgs.TEACHER.THRESH, device=pseudo_label.device).unsqueeze(
                0).repeat(len(pseudo_label), 1).gather(dim=1, index=(pseudo_label - 1).unsqueeze(-1))
            sem_conf_thresh = torch.tensor(cfgs.TEACHER.SEM_THRESH, device=pseudo_label.device).unsqueeze(
                0).repeat(len(pseudo_label), 1).gather(dim=1, index=(pseudo_label - 1).unsqueeze(-1))
            valid_inds = pseudo_score > conf_thresh.squeeze()
            valid_inds = valid_inds & (pseudo_sem_score > sem_conf_thresh.squeeze())
            if valid_inds.sum():
                record_dict = {
                    'pred_boxes': pseudo_box[valid_inds],
                    'pred_scores': pseudo_score[valid_inds],
                    'pred_labels': pseudo_label[valid_inds],
                    'pred_sem_scores': pseudo_sem_score[valid_inds]
                }
        filtered_pred_dicts.append(record_dict)

    return filtered_pred_dicts


@torch.no_grad()
def get_teacher_rcnn_cls_scores(teacher_model, ud_student_batch_dict, ud_teacher_batch_dict):
    rois_dict = {key: ud_student_batch_dict[key].clone().detach() for key in
                 ['rois', 'roi_scores', 'roi_labels']}
    rois_dict['has_class_labels'] = ud_student_batch_dict['has_class_labels']
    rois_dict['type'] = 'roi'
    rois_dict['batch_size'] = ud_student_batch_dict['batch_size']
    tea_point_feats_dict = {key: ud_teacher_batch_dict[key].clone().detach() for key in
                            ['point_features', 'point_coords', 'point_cls_scores']}

    rois_dict.update(tea_point_feats_dict)

    reverse_transform(rois_dict['rois'], ud_student_batch_dict, ud_teacher_batch_dict)
    teacher_model.roi_head.forward_ulb(rois_dict)
    reverse_transform(rois_dict['batch_box_preds'], ud_teacher_batch_dict, ud_student_batch_dict)
    pred_dicts_std, recall_dicts_std = teacher_model.post_processing(rois_dict, no_nms=True)

    return torch.cat(
        [pred_dict['pred_scores'] for pred_dict in pred_dicts_std], dim=0
    )


def reliable_student(teacher_model, student_model,
                     ld_teacher_batch_dict, ld_student_batch_dict,
                     ud_teacher_batch_dict, ud_student_batch_dict,
                     cfgs, epoch_id, dist):

    load_data_to_gpu(ld_student_batch_dict)
    load_data_to_gpu(ud_student_batch_dict)
    load_data_to_gpu(ud_teacher_batch_dict)
    if dist:
        teacher_model=teacher_model.module.onepass
        student_model=student_model.module.onepass

    with torch.no_grad():
        for cur_module in teacher_model.module_list:
            ud_teacher_batch_dict = cur_module(ud_teacher_batch_dict)
    pred_dicts, recall_dicts = teacher_model.post_processing(ud_teacher_batch_dict)

    teacher_boxes = filter_pseudo_labels(pred_dicts, cfgs)
    teacher_boxes = reverse_transform(teacher_boxes, ud_teacher_batch_dict, ud_student_batch_dict)
    pl_boxes = construct_pseudo_label(teacher_boxes)
    pl_scores = construct_pseudo_label_scores(teacher_boxes)
    #pl_boxes.to(ud_student_batch_dict['points'].device)
    #pl_scores.to(ud_student_batch_dict['points'].device)
    ud_student_batch_dict['gt_boxes'] = pl_boxes
    ud_student_batch_dict['pl_scores'] = pl_scores
    
    ld_ret_dict, ld_tb_dict, _ = student_model(ld_student_batch_dict)

    for cur_module in student_model.module_list[:-1]:
        ud_student_batch_dict = cur_module(ud_student_batch_dict)

    student_model.roi_head.generate_proposals(ud_student_batch_dict)

    cls_scores = get_teacher_rcnn_cls_scores(teacher_model, ud_student_batch_dict, ud_teacher_batch_dict)
    ud_student_batch_dict['rcnn_cls_score_teacher'] = cls_scores

    student_model.module_list[-1](ud_student_batch_dict)

    ud_ret_dict, ud_tb_dict, ud_disp_dict = student_model.get_training_loss_ulb()
    loss = ld_ret_dict['loss'] + cfgs.UNLABELED_WEIGHT * ud_ret_dict['loss']

    ud_tb_dict.update(ld_tb_dict)

    return loss, ud_tb_dict, ud_disp_dict
