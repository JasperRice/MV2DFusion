dataset_type = "KittiMonoDataset"
data_root = "data/kitti/"
class_names = ["Pedestrian", "Cyclist", "Car"]
input_modality = dict(use_lidar=False, use_camera=True)
img_norm_cfg = dict(
    mean=[123.675, 116.28, 103.53], std=[58.395, 57.12, 57.375], to_rgb=True
)
train_pipeline = [
    dict(type="LoadImageFromFileMono3D"),
    dict(
        type="LoadAnnotations3D",
        with_bbox=True,
        with_label=True,
        with_attr_label=False,
        with_bbox_3d=True,
        with_label_3d=True,
        with_bbox_depth=True,
    ),
    dict(type="Resize", img_scale=(1242, 375), keep_ratio=True),
    dict(type="RandomFlip3D", flip_ratio_bev_horizontal=0.5),
    dict(type="Normalize", **img_norm_cfg),
    dict(type="Pad", size_divisor=32),
    dict(type="DefaultFormatBundle3D", class_names=class_names),
    dict(
        type="Collect3D",
        keys=[
            "img",
            "gt_bboxes",
            "gt_labels",
            "gt_bboxes_3d",
            "gt_labels_3d",
            "centers2d",
            "depths",
        ],
    ),
]
test_pipeline = [
    dict(type="LoadImageFromFileMono3D"),
    dict(
        type="MultiScaleFlipAug",
        img_scale=(1242, 375),
        flip=False,
        transforms=[
            dict(type="RandomFlip3D"),
            dict(type="Normalize", **img_norm_cfg),
            dict(type="Pad", size_divisor=32),
            dict(
                type="DefaultFormatBundle3D", class_names=class_names, with_label=False
            ),
            dict(type="Collect3D", keys=["img"]),
        ],
    ),
]
# construct a pipeline for data and gt loading in show function
# please keep its loading function consistent with test_pipeline (e.g. client)
eval_pipeline = [
    dict(type="LoadImageFromFileMono3D"),
    dict(type="DefaultFormatBundle3D", class_names=class_names, with_label=False),
    dict(type="Collect3D", keys=["img"]),
]
data = dict(
    samples_per_gpu=2,
    workers_per_gpu=2,
    train=dict(
        type=dataset_type,
        data_root=data_root,
        ann_file=data_root + "kitti_infos_train_mono3d.coco.json",
        info_file=data_root + "kitti_infos_train.pkl",
        img_prefix=data_root,
        classes=class_names,
        pipeline=train_pipeline,
        modality=input_modality,
        test_mode=False,
        box_type_3d="Camera",
    ),
    val=dict(
        type=dataset_type,
        data_root=data_root,
        ann_file=data_root + "kitti_infos_val_mono3d.coco.json",
        info_file=data_root + "kitti_infos_val.pkl",
        img_prefix=data_root,
        classes=class_names,
        pipeline=test_pipeline,
        modality=input_modality,
        test_mode=True,
        box_type_3d="Camera",
    ),
    test=dict(
        type=dataset_type,
        data_root=data_root,
        ann_file=data_root + "kitti_infos_val_mono3d.coco.json",
        info_file=data_root + "kitti_infos_val.pkl",
        img_prefix=data_root,
        classes=class_names,
        pipeline=test_pipeline,
        modality=input_modality,
        test_mode=True,
        box_type_3d="Camera",
    ),
)
evaluation = dict(interval=2)
