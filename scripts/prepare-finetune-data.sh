#!/bin/bash
set -euo pipefail

# ==============================================================================
# Prepare Fine-Tuning Data for Doors (detection) + Walls (segmentation)
#
# This script:
#   1. Downloads KITTI (direct) for outdoor detection baseline testing
#   2. Provides instructions for ScanNet (doors) and S3DIS (walls)
#   3. Parses available data into unified annotation format
#   4. Splits into train/val for fine-tuning
#
# Prerequisites:
#   - setup-ec2.sh completed (or local with uv sync --extra io)
#   - For ScanNet: registration at http://www.scan-net.org/
#   - For S3DIS: registration at http://buildingparser.stanford.edu/dataset.html
#
# Usage:
#   bash scripts/prepare-finetune-data.sh
# ==============================================================================

echo "============================================"
echo "Preparing Fine-Tuning Data"
echo "  Detection target: doors"
echo "  Segmentation target: walls"
echo "============================================"
echo ""

DATA_ROOT="./data"
ANNOTATIONS_ROOT="./data/annotations"

# --- KITTI (direct download, outdoor baseline) ---
echo ">>> Step 1: KITTI data (outdoor detection baseline)..."
if [ ! -d "${DATA_ROOT}/kitti/training/velodyne" ]; then
    echo "  Downloading KITTI velodyne + labels..."
    uv run pointcloud-detect data download --dataset kitti --data-root "${DATA_ROOT}"
else
    echo "  KITTI already present, skipping download."
fi

echo "  Parsing KITTI annotations..."
uv run pointcloud-detect data parse --dataset kitti --data-root "${DATA_ROOT}" --output "${ANNOTATIONS_ROOT}"

# --- ScanNet (doors — detection fine-tuning) ---
echo ""
echo ">>> Step 2: ScanNet data (doors for detection fine-tuning)..."
SCANNET_DIR="${DATA_ROOT}/scannet/scans"
if [ -d "${SCANNET_DIR}" ] && [ "$(ls -d ${SCANNET_DIR}/scene* 2>/dev/null | wc -l)" -gt 0 ]; then
    echo "  ScanNet scenes found. Parsing annotations (filtering for doors)..."
    uv run pointcloud-detect data parse --dataset scannet --data-root "${DATA_ROOT}" --output "${ANNOTATIONS_ROOT}"
    
    # Filter annotations to doors only for detection fine-tuning
    echo "  Filtering to doors-only annotations..."
    python3 -c "
import json
from pathlib import Path

ann_dir = Path('${ANNOTATIONS_ROOT}/scannet')
doors_dir = Path('${ANNOTATIONS_ROOT}/scannet_doors')
doors_dir.mkdir(parents=True, exist_ok=True)

count = 0
for f in sorted(ann_dir.glob('*.json')):
    data = json.loads(f.read_text())
    door_anns = [a for a in data['annotations'] if a['class_name'] == 'door']
    if door_anns:
        data['annotations'] = door_anns
        data['num_annotations'] = len(door_anns)
        (doors_dir / f.name).write_text(json.dumps(data, indent=2))
        count += 1

print(f'  Created {count} scenes with door annotations in {doors_dir}')
"
else
    echo "  ScanNet not downloaded yet."
    echo ""
    echo "  To get ScanNet data for door detection fine-tuning:"
    echo "    1. Register at http://www.scan-net.org/"
    echo "    2. Run their download script for scenes with doors:"
    echo "       python download-scannet.py -o ${SCANNET_DIR} --type _vh_clean_2.ply _bbox.npy"
    echo "    3. Re-run this script after download completes."
    echo ""
fi

# --- S3DIS (walls — segmentation fine-tuning) ---
echo ""
echo ">>> Step 3: S3DIS data (walls for segmentation fine-tuning)..."
S3DIS_DIR="${DATA_ROOT}/s3dis"
if [ -d "${S3DIS_DIR}/Area_1" ] && [ "$(ls -d ${S3DIS_DIR}/Area_*/*/Annotations 2>/dev/null | wc -l)" -gt 0 ]; then
    echo "  S3DIS data found. Parsing annotations (filtering for walls)..."
    uv run pointcloud-detect data parse --dataset s3dis --data-root "${DATA_ROOT}" --output "${ANNOTATIONS_ROOT}"
    
    # Filter to wall annotations for segmentation fine-tuning
    echo "  Filtering to wall-only annotations..."
    python3 -c "
import json
from pathlib import Path

ann_dir = Path('${ANNOTATIONS_ROOT}/s3dis')
walls_dir = Path('${ANNOTATIONS_ROOT}/s3dis_walls')
walls_dir.mkdir(parents=True, exist_ok=True)

count = 0
for f in sorted(ann_dir.glob('*.json')):
    data = json.loads(f.read_text())
    wall_anns = [a for a in data['annotations'] if a['class_name'] == 'wall']
    if wall_anns:
        data['annotations'] = wall_anns
        data['num_annotations'] = len(wall_anns)
        (walls_dir / f.name).write_text(json.dumps(data, indent=2))
        count += 1

print(f'  Created {count} scenes with wall annotations in {walls_dir}')
"
else
    echo "  S3DIS not downloaded yet."
    echo ""
    echo "  To get S3DIS data for wall segmentation fine-tuning:"
    echo "    1. Register at http://buildingparser.stanford.edu/dataset.html"
    echo "    2. Download Stanford3dDataset_v1.2_Aligned_Version.zip"
    echo "    3. Extract to ${S3DIS_DIR}/"
    echo "    4. Re-run this script after extraction completes."
    echo ""
fi

# --- Summary ---
echo ""
echo "============================================"
echo "Data Preparation Summary"
echo "============================================"
echo ""
echo "Annotations directory: ${ANNOTATIONS_ROOT}/"
[ -d "${ANNOTATIONS_ROOT}/kitti" ] && echo "  kitti/        — $(ls "${ANNOTATIONS_ROOT}"/kitti/*.json 2>/dev/null | wc -l) scenes (outdoor baseline)"
[ -d "${ANNOTATIONS_ROOT}/scannet" ] && echo "  scannet/      — $(ls "${ANNOTATIONS_ROOT}"/scannet/*.json 2>/dev/null | wc -l) scenes (all classes)"
[ -d "${ANNOTATIONS_ROOT}/scannet_doors" ] && echo "  scannet_doors/ — $(ls "${ANNOTATIONS_ROOT}"/scannet_doors/*.json 2>/dev/null | wc -l) scenes (doors only → detection fine-tuning)"
[ -d "${ANNOTATIONS_ROOT}/s3dis" ] && echo "  s3dis/        — $(ls "${ANNOTATIONS_ROOT}"/s3dis/*.json 2>/dev/null | wc -l) rooms (all classes)"
[ -d "${ANNOTATIONS_ROOT}/s3dis_walls" ] && echo "  s3dis_walls/  — $(ls "${ANNOTATIONS_ROOT}"/s3dis_walls/*.json 2>/dev/null | wc -l) rooms (walls only → segmentation fine-tuning)"
echo ""
echo "Next steps:"
echo "  1. Fine-tune detection (doors):  python -m pointcloud_detect.training.trainer --config configs/finetune_doors.yaml"
echo "  2. Fine-tune segmentation (walls): python -m pointcloud_detect.training.trainer --config configs/finetune_walls.yaml"
echo "============================================"
