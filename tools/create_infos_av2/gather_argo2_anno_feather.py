# "create complete infos of gts for evaluation"
import os
from pathlib import Path

import pandas as pd
from av2.utils.io import read_feather

if __name__ == "__main__":
    save_path = "data/argo/converted/val_anno.feather"  # replace with absolute path
    split_dir = Path("data/argo/converted/val")  # replace with absolute path
    annotations_path_list = split_dir.glob("*/annotations.feather")

    seg_anno_list = []
    for annotations_path in annotations_path_list:

        seg_anno = read_feather(Path(annotations_path))
        log_dir = os.path.dirname(annotations_path)
        log_id = log_dir.split("/")[-1]
        print(log_id)
        seg_anno["log_id"] = log_id
        seg_anno_list.append(seg_anno)

    gts = pd.concat(seg_anno_list).reset_index()
    gts.to_feather(save_path)
