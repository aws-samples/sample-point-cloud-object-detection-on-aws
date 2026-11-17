"""Pretrained checkpoints: fetched once, verified, and loaded without full unpickling.

A checkpoint is a pickle, so loading one runs whatever it says unless ``torch.load``
is limited to tensors and plain containers (``weights_only``). Two controls (threat
model T28):

- Every pretrained URL has a pinned SHA-256. A download that does not match is
  deleted and refused, and so is a cached file that no longer matches, so a changed
  or substituted checkpoint never reaches ``torch.load``.
- Every load is weights-only, with nothing allowlisted. The pinned checkpoints and
  the trainer's exports contain only tensors and builtins, so none needs a class
  allowed; mmengine's full training checkpoints would (HistoryBuffer, numpy objects
  and ``getattr``), and allowing ``getattr`` would undo the protection.

Customer weights (a registered model's ``s3://`` path) are not pinned, since only
the customer knows their hash, but they load weights-only too.

Licensing: every checkpoint below was trained on data whose terms forbid or limit
commercial use (ScanNet, S3DIS, nuScenes, KITTI). None is downloaded unless the
operator sets ALLOW_NONCOMMERCIAL_WEIGHTS=1, which records that they have accepted
those terms. Without it, ``fetch`` refuses before any network request, and the
sample runs only weights the customer trained themselves (README, "Commercially
clean model").
"""

import hashlib
import logging
import os
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# SHA-256 of each pretrained checkpoint the backends and the trainer reference,
# keyed by URL. Where OpenMMLab's filename ends in a hash prefix, the pin starts with
# it (tests/test_checkpoints.py checks this).
PINNED_SHA256: dict[str, str] = {
    "https://download.openmmlab.com/mmdetection3d/v1.0.0_models/fcaf3d/"
    "fcaf3d_8x2_scannet-3d-18class/fcaf3d_8x2_scannet-3d-18class_20220805_084956.pth": (
        "fa75a4658e6c2315623b39e3259b0a2dc6dc778ddf9dd56b5c8eddaae6fb37f7"
    ),
    "https://download.openmmlab.com/mmdetection3d/v1.0.0_models/centerpoint/"
    "centerpoint_01voxel_second_secfpn_circlenms_4x8_cyclic_20e_nus/"
    "centerpoint_01voxel_second_secfpn_circlenms_4x8_cyclic_20e_nus"
    "_20220810_030004-9061688e.pth": (
        "9061688e5f81adae87d28241143e2d33075f68908134264f0de8c901acf911d8"
    ),
    "https://download.openmmlab.com/mmdetection3d/v1.0.0_models/pointpillars/"
    "hv_pointpillars_secfpn_6x8_160e_kitti-3d-3class/"
    "hv_pointpillars_secfpn_6x8_160e_kitti-3d-3class_20220301_150306-37dc2420.pth": (
        "37dc242098f0d3dd7d870e0fe7cd4514f76fd85a04fb1377681279f963b3cbe5"
    ),
    "https://download.openmmlab.com/mmdetection3d/v1.0.0_models/votenet/"
    "votenet_8x8_scannet-3d-18class/"
    "votenet_8x8_scannet-3d-18class_20210823_234503-cf8134fa.pth": (
        "cf8134fa439d760b709932e831d162db446f161db6bad4922a2fb360b6dce75a"
    ),
    "https://download.openmmlab.com/mmdetection3d/v0.1.0_models/pointnet2/"
    "pointnet2_ssg_16x2_cosine_200e_scannet_seg-3d-20class/"
    "pointnet2_ssg_16x2_cosine_200e_scannet_seg-3d-20class"
    "_20210514_143644-ee73704a.pth": (
        "ee73704a74085babfd2ad8d8a22e4a279ae228b3fe8256f988c2ddb8160853ac"
    ),
    "https://download.openmmlab.com/mmdetection3d/v0.1.0_models/pointnet2/"
    "pointnet2_ssg_xyz-only_16x2_cosine_200e_scannet_seg-3d-20class/"
    "pointnet2_ssg_xyz-only_16x2_cosine_200e_scannet_seg-3d-20class"
    "_20210514_143628-4e341a48.pth": (
        "4e341a48c8fd108259d974618945a8d483cd779837e924f23f99c3e9cd71fb1d"
    ),
    "https://download.openmmlab.com/mmdetection3d/v0.1.0_models/pointnet2/"
    "pointnet2_ssg_16x2_cosine_50e_s3dis_seg-3d-13class/"
    "pointnet2_ssg_16x2_cosine_50e_s3dis_seg-3d-13class"
    "_20210514_144205-995d0119.pth": (
        "995d0119e8a17dfba0f6f4f49b19b220ffeae4394e3bca2ac9d64f4112abfdf2"
    ),
}

_CHUNK = 1 << 20

# Opt-in for the checkpoints above. Off unless set to one of _TRUE_VALUES.
NONCOMMERCIAL_ENV = "ALLOW_NONCOMMERCIAL_WEIGHTS"
_TRUE_VALUES = frozenset({"1", "true", "yes"})


class NonCommercialWeightsDisabled(RuntimeError):
    """A pretrained checkpoint was requested while ALLOW_NONCOMMERCIAL_WEIGHTS is off.

    Deliberately not an ImportError: the backend factories fall back to the stub on
    ImportError, and a refused model must fail the job, not run placeholder boxes.
    """


def noncommercial_allowed() -> bool:
    """Whether the operator has opted in to the non-commercially trained checkpoints."""
    return os.environ.get(NONCOMMERCIAL_ENV, "").strip().lower() in _TRUE_VALUES


DISABLED_MESSAGE = (
    "The pretrained checkpoints were trained on datasets licensed for "
    "non-commercial use only (THIRD-PARTY-LICENSES) and are disabled. "
    "Train a model from scratch on data "
    "you may use commercially and select it (README, 'Commercially clean model'), "
    f"or set {NONCOMMERCIAL_ENV}=1 after accepting those datasets' terms."
)


def _ml_stack_installed() -> bool:
    """Whether this image can run real inference (and so fetch a checkpoint)."""
    import importlib.util

    return importlib.util.find_spec("mmdet3d") is not None


def pretrained_selection_allowed() -> bool:
    """Whether a job or the UI may select a pretrained model.

    True with the opt-in, and in an image without MMDetection3D (the stub variant),
    where the backends fall back to placeholder boxes before any checkpoint is
    fetched, so selecting one downloads and loads nothing.
    """
    return noncommercial_allowed() or not _ml_stack_installed()


def require_noncommercial_opt_in() -> None:
    """Raise NonCommercialWeightsDisabled unless the operator has opted in."""
    if not noncommercial_allowed():
        raise NonCommercialWeightsDisabled(DISABLED_MESSAGE)


def cache_dir() -> Path:
    """torch hub's checkpoint directory, which the images point at a writable path."""
    torch_home = os.environ.get("TORCH_HOME")
    if not torch_home:
        xdg = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        torch_home = str(Path(xdg) / "torch")
    return Path(torch_home) / "hub" / "checkpoints"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: Path) -> None:
    from torch.hub import download_url_to_file  # pyright: ignore[reportMissingImports]

    download_url_to_file(url, str(dest), progress=False)


def fetch(
    url: str,
    *,
    directory: Path | None = None,
    download: Callable[[str, Path], None] = _download,
) -> Path:
    """Return a local copy of a pinned checkpoint, downloading it if needed.

    Raises NonCommercialWeightsDisabled, before touching the network or the cache,
    unless ALLOW_NONCOMMERCIAL_WEIGHTS is set: every pinned checkpoint is
    non-commercially trained. Raises ValueError for a URL that is not pinned or not
    HTTPS, and for a file whose SHA-256 does not match; a mismatching file is deleted
    first.
    """
    require_noncommercial_opt_in()
    expected = PINNED_SHA256.get(url)
    if expected is None:
        raise ValueError(f"no pinned SHA-256 for checkpoint {url}")
    if not url.startswith("https://"):
        raise ValueError(f"checkpoint URL must be HTTPS: {url}")

    target_dir = directory or cache_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / url.rsplit("/", 1)[-1]

    if dest.exists():
        if sha256_of(dest) == expected:
            return dest
        logger.warning("Cached checkpoint %s does not match its pin; refetching", dest)
        dest.unlink()

    partial = dest.with_name(dest.name + ".partial")
    try:
        download(url, partial)
        actual = sha256_of(partial)
        if actual != expected:
            raise ValueError(
                f"checkpoint {url} has SHA-256 {actual}, expected {expected}"
            )
        os.replace(partial, dest)
    finally:
        partial.unlink(missing_ok=True)
    return dest


def enforce_weights_only() -> None:
    """Make every ``torch.load`` in this process weights-only, mmengine's included.

    torch 2.6 and later default to ``weights_only=True``, but a caller may still pass
    False; TORCH_FORCE_WEIGHTS_ONLY_LOAD overrides that, and torch reads it on every
    load.
    The GPU image sets it; this sets it for any other process that loads a model,
    unless it is already set.
    """
    os.environ.setdefault("TORCH_FORCE_WEIGHTS_ONLY_LOAD", "1")
