"""scripts/patch_mmdet3d_lyft.py: mmdet3d's Lyft metric optional, the SDK removable."""

import importlib
import importlib.util
import runpy
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "patch_mmdet3d_lyft.py"
PACKAGE = "mmdet3d_shape"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


patcher = _load(SCRIPT, "patch_mmdet3d_lyft")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _upstream_shape(base: Path, first_import: str = "lyft_dataset_sdk") -> Path:
    """An mmdet3d-shaped package whose two __init__ files carry the exact fragments.

    The Lyft modules import `first_import`, then lyft_dataset_sdk, as mmdet3d's Lyft
    metric imports pandas before the SDK; the other modules are stand-ins.
    """
    root = base / PACKAGE
    _write(root, "__init__.py", "")
    _write(
        root,
        "evaluation/__init__.py",
        "from .functional import kitti_eval, lyft_eval\n"
        "from .metrics import KittiMetric, LyftMetric\n",
    )
    _write(
        root,
        patcher.FUNCTIONAL,
        "from .kitti_utils import kitti_eval\n" + patcher.FUNCTIONAL_OLD,
    )
    _write(
        root, "evaluation/functional/kitti_utils.py", "def kitti_eval():\n    pass\n"
    )
    _write(
        root,
        "evaluation/functional/lyft_eval.py",
        f"import {first_import}\nimport lyft_dataset_sdk\n\n\n"
        "def get_classwise_aps():\n    pass\n\n\n"
        "def get_single_class_aps():\n    pass\n\n\n"
        "def load_lyft_gts():\n    pass\n\n\n"
        "def load_lyft_predictions():\n    pass\n\n\n"
        "def lyft_eval():\n    pass\n",
    )
    _write(
        root,
        patcher.METRICS,
        "from .kitti_metric import KittiMetric  # noqa: F401,F403\n"
        + patcher.METRICS_OLD,
    )
    _write(root, "evaluation/metrics/kitti_metric.py", "class KittiMetric:\n    pass\n")
    _write(
        root,
        "evaluation/metrics/lyft_metric.py",
        f"import {first_import}\nimport lyft_dataset_sdk\n\n\n"
        "class LyftMetric:\n    pass\n",
    )
    return root


@pytest.fixture
def importable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    ours = (PACKAGE, "lyft_dataset_sdk")
    for name in [m for m in sys.modules if m.split(".")[0] in ours]:
        del sys.modules[name]
    importlib.invalidate_caches()


def test_unpatched_the_metrics_need_the_sdk(importable: Path) -> None:
    _upstream_shape(importable)
    with pytest.raises(ModuleNotFoundError) as exc:
        importlib.import_module(f"{PACKAGE}.evaluation")
    assert exc.value.name == "lyft_dataset_sdk"


def test_patched_the_metrics_register_without_it(importable: Path) -> None:
    root = _upstream_shape(importable)
    assert patcher.main([str(root)]) == 0
    evaluation = importlib.import_module(f"{PACKAGE}.evaluation")
    assert evaluation.LyftMetric is None
    assert evaluation.lyft_eval is None
    assert evaluation.KittiMetric.__name__ == "KittiMetric"
    assert callable(evaluation.kitti_eval)


def test_with_the_sdk_installed_the_metric_is_kept(importable: Path) -> None:
    root = _upstream_shape(importable)
    _write(importable, "lyft_dataset_sdk/__init__.py", "")
    assert patcher.main([str(root)]) == 0
    evaluation = importlib.import_module(f"{PACKAGE}.evaluation")
    assert evaluation.LyftMetric.__name__ == "LyftMetric"
    assert callable(evaluation.lyft_eval)


def test_without_the_sdk_the_lyft_modules_are_not_imported(importable: Path) -> None:
    # In the GPU image the Lyft metric's first import, pandas, cannot load at all.
    root = _upstream_shape(importable, first_import="not_installed_for_this_test")
    assert patcher.main([str(root)]) == 0
    evaluation = importlib.import_module(f"{PACKAGE}.evaluation")
    assert evaluation.LyftMetric is None
    assert f"{PACKAGE}.evaluation.metrics.lyft_metric" not in sys.modules


def test_with_the_sdk_installed_their_errors_still_raise(importable: Path) -> None:
    root = _upstream_shape(importable, first_import="not_installed_for_this_test")
    _write(importable, "lyft_dataset_sdk/__init__.py", "")
    assert patcher.main([str(root)]) == 0
    with pytest.raises(ModuleNotFoundError) as exc:
        importlib.import_module(f"{PACKAGE}.evaluation")
    assert exc.value.name == "not_installed_for_this_test"


def test_a_second_run_changes_nothing(tmp_path: Path, capsys) -> None:
    root = _upstream_shape(tmp_path)
    assert patcher.main([str(root)]) == 0
    before = {rel: (root / rel).read_text() for rel, _, _ in patcher.PATCHES}
    assert patcher.main([str(root)]) == 0
    assert {rel: (root / rel).read_text() for rel, _, _ in patcher.PATCHES} == before
    assert "already patched" in capsys.readouterr().out


def test_other_code_is_refused_and_nothing_changes(tmp_path: Path) -> None:
    root = _upstream_shape(tmp_path)
    other = (root / patcher.FUNCTIONAL).read_text().replace("load_lyft_gts,", "x,")
    (root / patcher.FUNCTIONAL).write_text(other)
    metrics_before = (root / patcher.METRICS).read_text()
    with pytest.raises(SystemExit) as exc:
        patcher.main([str(root)])
    assert "not mmdet3d 1.4.0's code" in str(exc.value)
    assert (root / patcher.FUNCTIONAL).read_text() == other
    assert (root / patcher.METRICS).read_text() == metrics_before


def test_it_runs_as_a_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = _upstream_shape(tmp_path)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(root)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 0
    assert "Lyft metric made optional" in capsys.readouterr().out
