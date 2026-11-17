"""scripts/patch_mmengine_metadata.py: mmengine package lookup, no pkg_resources."""

import importlib.metadata
import importlib.util
import runpy
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "patch_mmengine_metadata.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


patcher = _load(SCRIPT, "patch_mmengine_metadata")


def _upstream_shape() -> str:
    """A package_utils.py made of the exact fragments the script looks for."""
    return (
        "import os.path as osp\n\n\n"
        "def is_installed(package: str) -> bool:\n"
        "    # When executing `import mmengine.runner`,\n"
        "    # pkg_resources will be imported and it takes too much time.\n"
        + patcher.IS_INSTALLED_OLD
        + "        spec = importlib.util.find_spec(package)\n"
        "        if spec is None:\n"
        "            return False\n"
        "        elif spec.origin is not None:\n"
        "            return True\n"
        "        else:\n"
        "            return False\n\n\n"
        "def get_installed_path(package: str) -> str:\n"
        + patcher.GET_PATH_OLD
        + "        spec = importlib.util.find_spec(package)\n"
        "        if spec is not None and spec.origin is not None:\n"
        "            return osp.dirname(spec.origin)\n"
        "        raise e\n\n"
        + patcher.LOCATION_OLD
        + "\n\ndef package2module(package: str):\n"
        + patcher.TO_MODULE_OLD
        + "    else:\n"
        "        raise ValueError(f'can not infer the module name of {package}')\n"
    )


@pytest.fixture
def patched(tmp_path: Path):
    target = tmp_path / "package_utils.py"
    target.write_text(_upstream_shape())
    assert patcher.main([str(target)]) == 0
    return target, _load(target, "patched_package_utils")


def test_no_pkg_resources_import_is_left(patched) -> None:
    target, _ = patched
    text = target.read_text()
    assert "import pkg_resources" not in text
    assert "from pkg_resources" not in text
    assert "importlib.metadata" in text


def test_the_lookups_answer_as_before(patched) -> None:
    _, module = patched
    assert module.is_installed("pytest") is True
    assert module.is_installed("no-such-package-for-this-test") is False
    path = Path(module.get_installed_path("pytest"))
    assert path.name == "pytest" and path.is_dir()
    with pytest.raises(importlib.metadata.PackageNotFoundError):
        module.get_installed_path("no-such-package-for-this-test")
    # The first name in top_level.txt, as mmengine reads it; a distribution with no
    # top_level.txt (packaging's) has no module name to infer.
    assert module.package2module("pluggy") == "pluggy"
    assert module.package2module("PyYAML") == "_yaml"
    with pytest.raises(ValueError, match="can not infer"):
        module.package2module("packaging")


def test_a_second_run_changes_nothing(patched, capsys) -> None:
    target, _ = patched
    before = target.read_text()
    assert patcher.main([str(target)]) == 0
    assert target.read_text() == before
    assert "already patched" in capsys.readouterr().out


def test_other_code_is_refused_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "package_utils.py"
    other = _upstream_shape().replace("importlib.reload(pkg_resources)", "pass")
    target.write_text(other)
    with pytest.raises(SystemExit) as exc:
        patcher.main([str(target)])
    assert "not mmengine 0.10.7's" in str(exc.value)
    assert target.read_text() == other


def test_it_runs_as_a_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    target = tmp_path / "package_utils.py"
    target.write_text(_upstream_shape())
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(target)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 0
    assert "patched to use importlib.metadata" in capsys.readouterr().out
