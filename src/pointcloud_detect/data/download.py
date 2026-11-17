"""Dataset download utilities for point cloud detection.

Supports three dataset categories:
- Indoor room scans: ScanNet v2 (18-class detection, 20-class segmentation)
- Indoor structural: S3DIS (13-class segmentation with columns, beams)
- Outdoor: KITTI 3D Object Detection (cars, pedestrians, cyclists)

Each dataset is downloaded into a standard directory structure and
parsed into a unified annotation format compatible with fine-tuning.
"""

import hashlib
import zipfile
from pathlib import Path

DATA_ROOT = Path("./data")

# KITTI 3D object archives, by the file names KITTI's download page gives them, with the
# SHA-256 of each. KITTI asks every user to register and accept its terms before
# downloading (https://www.cvlibs.net/datasets/kitti/user_login.php), so this module
# does not download them: the user does, after signing in, and the archives are checked
# here before anything is extracted, so a changed or substituted archive is refused.
# KITTI publishes no checksums; these were taken from the archives KITTI served on
# 2026-10-29.
KITTI_ARCHIVES = {
    "velodyne": (
        "data_object_velodyne.zip",
        "2a005138e7a7c01eda7abe0ba48139b27d864984f1c1cab38f3df1c54e87eb28",
    ),
    "labels": (
        "data_object_label_2.zip",
        "4efc76220d867e1c31bb980bbf8cbc02599f02a9cb4350effa98dbb04aaed880",
    ),
    "calib": (
        "data_object_calib.zip",
        "8f7777c2221d79fcb0303838d832311ad4d324c8b9d8243a8ec06952c72802bb",
    ),
}
KITTI_PAGE = "https://www.cvlibs.net/datasets/kitti/eval_object.php?obj_benchmark=3d"
# The directory each archive creates, so an archive already extracted is not redone.
KITTI_EXTRACTED = {"velodyne": "velodyne", "labels": "label_2", "calib": "calib"}

NUSCENES_MINI_URL = "https://www.nuscenes.org/data/v1.0-mini.tgz"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_zip(zip_path: Path, extract_to: Path) -> None:
    """Extract a zip file."""
    print(f"  [extract] {zip_path.name}...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_to)


def download_nuscenes_mini(data_root: Path | None = None) -> Path:
    """Download nuScenes mini split for outdoor 360° LiDAR testing.

    nuScenes mini is a free ~4GB subset (10 scenes) of the full nuScenes dataset.
    It requires a free account at https://www.nuscenes.org/nuscenes.

    The LiDAR point clouds are stored as .bin files (N x 5 float32:
    x, y, z, intensity, ring_index) in samples/LIDAR_TOP/.

    Args:
        data_root: Root directory for data storage. Defaults to ./data.

    Returns:
        Path to the nuScenes dataset directory.
    """
    import tarfile

    root = (data_root or DATA_ROOT) / "nuscenes"
    root.mkdir(parents=True, exist_ok=True)

    lidar_dir = root / "samples" / "LIDAR_TOP"
    if lidar_dir.exists() and any(lidar_dir.glob("*.bin")):
        count = len(list(lidar_dir.glob("*.bin")))
        print(f"nuScenes mini: {count} LiDAR samples already present")
        return root

    tgz_path = root / "v1.0-mini.tgz"
    if not tgz_path.exists():
        print(
            "\nnuScenes mini requires a free account. To download:\n"
            "  1. Register at https://www.nuscenes.org/nuscenes\n"
            "  2. Download 'v1.0-mini' (~4 GB) from the downloads page\n"
            f"  3. Save to: {tgz_path}\n"
            "\n"
            "  Or download directly with curl (after logging in to get cookie):\n"
            f"    curl -o {tgz_path} '{NUSCENES_MINI_URL}'\n"
        )
    else:
        print(f"  [extract] {tgz_path.name}...")
        with tarfile.open(tgz_path, "r:gz") as tf:
            # The archive is a manual download from a third party. The "data"
            # filter refuses absolute paths, "..", links that leave root, device
            # files and setuid bits, so a tampered archive cannot write outside
            # root (T42). Python 3.11.4 and later have it; on an older
            # 3.11 the extraction is refused rather than done unfiltered.
            if not hasattr(tarfile, "data_filter"):
                raise RuntimeError(
                    "Extracting this archive safely needs Python 3.11.4 or later "
                    "(tarfile extraction filters)."
                )
            tf.extractall(root, filter="data")
        print(f"nuScenes mini extracted to: {root}")

    print(f"\nnuScenes directory: {root}")
    return root


def download_kitti(data_root: Path | None = None) -> Path:
    """Prepare the KITTI 3D Object Detection dataset from archives the user downloaded.

    Contains LiDAR point clouds in .bin format (N x 4: x, y, z, reflectance)
    with 3D bounding box annotations for car, pedestrian, cyclist.

    KITTI requires registration, so the archives are not downloaded here. Each
    archive found in the KITTI directory is checked against its SHA-256 in
    KITTI_ARCHIVES and then extracted; one that does not match is refused. For any
    that is missing, the steps to get it are printed.

    Args:
        data_root: Root directory for data storage. Defaults to ./data.

    Returns:
        Path to the KITTI dataset directory.

    Raises:
        ValueError: an archive does not match its pinned SHA-256.
    """
    root = (data_root or DATA_ROOT) / "kitti"
    root.mkdir(parents=True, exist_ok=True)

    missing = []
    for name, (filename, expected) in KITTI_ARCHIVES.items():
        zip_path = root / filename
        if not zip_path.exists():
            missing.append(filename)
            continue
        actual = _sha256(zip_path)
        if actual != expected:
            raise ValueError(
                f"{zip_path} has SHA-256 {actual}, not the {expected} KITTI served; "
                "refusing to extract it. Download it again from KITTI's page."
            )
        print(f"  [verified] {name}: {filename}")
        if not (root / "training" / KITTI_EXTRACTED[name]).exists():
            _extract_zip(zip_path, root)

    if missing:
        print(
            "\nKITTI requires a free account. To download:\n"
            "  1. Register and sign in at "
            "https://www.cvlibs.net/datasets/kitti/user_login.php\n"
            f"  2. On {KITTI_PAGE}, download:\n"
            + "".join(f"       {f}\n" for f in missing)
            + f"  3. Save them to: {root}\n"
            "  4. Run this command again to verify and extract them.\n"
            "  (Labels and calibration are 21 MB; the velodyne scans are 29 GB.)\n"
        )
    else:
        print(f"KITTI dataset ready at: {root}")
    return root


def download_scannet_samples(
    data_root: Path | None = None,
    scene_ids: list[str] | None = None,
) -> Path:
    """Prepare ScanNet sample scene data.

    ScanNet requires accepting a Terms of Use agreement at:
    http://www.scan-net.org/

    This function creates the expected directory structure and
    provides instructions for obtaining the data. If scene files
    are already present, it validates the structure.

    The standard directory layout after manual download:
        scannet/
        ├── scans/
        │   ├── scene0000_00/
        │   │   ├── scene0000_00_vh_clean_2.ply    (mesh + colors)
        │   │   ├── scene0000_00_vh_clean_2.labels.ply (semantic labels)
        │   │   ├── scene0000_00_bbox.npy          (axis-aligned bboxes)
        │   │   └── scene0000_00.aggregation.json  (instance segmentation)
        │   ├── scene0000_01/
        │   ...

    Args:
        data_root: Root directory for data storage.
        scene_ids: Specific scene IDs to prepare (e.g., ["scene0000_00"]).

    Returns:
        Path to the ScanNet dataset directory.
    """
    root = (data_root or DATA_ROOT) / "scannet"
    scans_dir = root / "scans"
    scans_dir.mkdir(parents=True, exist_ok=True)

    if scene_ids is None:
        scene_ids = [
            "scene0000_00",
            "scene0000_01",
            "scene0011_00",
            "scene0015_00",
            "scene0030_00",
        ]

    existing = [s for s in scene_ids if (scans_dir / s).exists()]
    missing = [s for s in scene_ids if not (scans_dir / s).exists()]

    if existing:
        print(f"ScanNet: {len(existing)} scenes already present")

    if missing:
        print(
            "\nScanNet requires registration. To download:\n"
            "  1. Go to http://www.scan-net.org/ and accept Terms of Use\n"
            "  2. You will receive a download script (download-scannet.py)\n"
            "  3. Run:\n"
        )
        for scene_id in missing[:3]:
            print(
                f"     python download-scannet.py -o {scans_dir} "
                f"--type _vh_clean_2.ply _vh_clean_2.labels.ply "
                f"_bbox.npy .aggregation.json --id {scene_id}"
            )
        if len(missing) > 3:
            print(f"     ... and {len(missing) - 3} more scenes")

        for scene_id in missing:
            (scans_dir / scene_id).mkdir(exist_ok=True)

    print(f"\nScanNet directory: {root}")
    return root


def download_s3dis_samples(data_root: Path | None = None) -> Path:
    """Prepare S3DIS dataset structure.

    S3DIS (Stanford Large-Scale 3D Indoor Spaces) requires
    filling out a form at:
    http://buildingparser.stanford.edu/dataset.html

    The dataset contains 6 areas of office buildings with annotations
    for: ceiling, floor, wall, beam, column, window, door, table,
    chair, sofa, bookcase, board, clutter.

    Expected directory layout after manual download:
        s3dis/
        ├── Area_1/
        │   ├── conferenceRoom_1/
        │   │   ├── conferenceRoom_1.txt        (XYZ RGB per line)
        │   │   └── Annotations/
        │   │       ├── beam_1.txt
        │   │       ├── column_1.txt
        │   │       ├── wall_1.txt
        │   │       └── ...
        │   ├── hallway_1/
        │   ...
        ├── Area_2/
        ...
        ├── Area_6/

    Args:
        data_root: Root directory for data storage.

    Returns:
        Path to the S3DIS dataset directory.
    """
    root = (data_root or DATA_ROOT) / "s3dis"
    root.mkdir(parents=True, exist_ok=True)

    areas_present = [
        d.name for d in root.iterdir() if d.is_dir() and d.name.startswith("Area_")
    ]

    if areas_present:
        print(f"S3DIS: {len(areas_present)} areas present: {areas_present}")
    else:
        print(
            "\nS3DIS requires registration. To download:\n"
            "  1. Go to http://buildingparser.stanford.edu/dataset.html\n"
            "  2. Fill out the download form\n"
            "  3. Download 'Stanford3dDataset_v1.2_Aligned_Version.zip'\n"
            f"  4. Extract to: {root}/\n"
            "\n"
            "  Expected structure:\n"
            "    s3dis/Area_1/office_1/office_1.txt\n"
            "    s3dis/Area_1/office_1/Annotations/chair_1.txt\n"
            "    ...\n"
        )
        for area in range(1, 7):
            (root / f"Area_{area}").mkdir(exist_ok=True)

    print(f"\nS3DIS directory: {root}")
    return root


def download_all(data_root: Path | None = None) -> dict[str, Path]:
    """Download/prepare all supported datasets.

    Returns:
        Dict mapping dataset name to its root path.
    """
    root = data_root or DATA_ROOT
    print("=" * 60)
    print("Point Cloud Detection - Dataset Preparation")
    print("=" * 60)

    results: dict[str, Path] = {}

    print("\n--- KITTI (outdoor, free account required) ---")
    results["kitti"] = download_kitti(root)

    print("\n--- nuScenes mini (outdoor 360\u00b0, free account required) ---")
    results["nuscenes"] = download_nuscenes_mini(root)

    print("\n--- ScanNet (indoor rooms, requires registration) ---")
    results["scannet"] = download_scannet_samples(root)

    print("\n--- S3DIS (indoor structural, requires registration) ---")
    results["s3dis"] = download_s3dis_samples(root)

    print("\n" + "=" * 60)
    print("Summary:")
    for name, path in results.items():
        print(f"  {name}: {path}")
    print("=" * 60)

    return results
