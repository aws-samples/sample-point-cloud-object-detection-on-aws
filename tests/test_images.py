"""Static checks that every backend image can import the project it ships.

The images are not built in CI (the GPU one is about 9 GB), so these pin the lines that
make `python -m pointcloud_detect...` work outside the Helm charts, which set
PYTHONPATH=/app/src themselves and so hide an image that cannot find its own code.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _instructions(name: str) -> list[str]:
    """The Dockerfile's instructions, one per entry, continuations joined."""
    text = (ROOT / name).read_text()
    joined = re.sub(r"\\\n", " ", text)
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _index(lines: list[str], prefix: str) -> int:
    return next(i for i, line in enumerate(lines) if line.startswith(prefix))


def test_root_image_installs_the_project_after_copying_its_source() -> None:
    lines = _instructions("Dockerfile")
    copy_src = _index(lines, "COPY src/")
    later = " ".join(lines[copy_src + 1 :])
    assert "uv sync" in later
    assert "--no-install-project" not in later


def test_gpu_image_puts_the_source_on_the_path() -> None:
    # Its project is synced before src/ is copied in, so the editable install has no
    # path entry of its own.
    lines = _instructions("Dockerfile.gpu")
    assert _index(lines, "RUN uv sync") < _index(lines, "COPY src/")
    env = " ".join(line for line in lines if line.startswith("ENV "))
    assert re.search(r"(^|\s)PYTHONPATH=/app/src(\s|$)", env)


def test_cpu_image_inherits_the_gpu_image_and_resets_neither_path() -> None:
    lines = _instructions("Dockerfile.cpu")
    assert any(line.startswith("FROM ${BASE_IMAGE}") for line in lines)
    assert not [line for line in lines if "PYTHONPATH" in line]
    assert any(line.startswith("COPY src/ /app/src/") for line in lines)


def test_images_make_a_scratch_directory_other_uids_can_pass_through() -> None:
    # docker compose's named volume takes this directory's owner; on EKS the parser
    # container, as uid 1001, reaches /scratch/ipc through it.
    for name in ("Dockerfile", "Dockerfile.gpu"):
        run = " ".join(_instructions(name))
        assert "install -d -o 999 -g 999 -m 0755 /scratch" in run, name


def test_compose_worker_scratch_is_on_disk_and_tmp_is_bounded() -> None:
    import yaml

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    worker = compose["services"]["worker"]
    env = dict(item.split("=", 1) for item in worker["environment"])
    assert env["TMPDIR"] == env["PCD_EXTRACT_DIR"] == "/scratch"
    assert "pcd-scratch:/scratch" in worker["volumes"]
    assert "pcd-scratch" in compose["volumes"]
    assert [t for t in worker["tmpfs"] if t.startswith("/tmp:size=")] == worker["tmpfs"]
