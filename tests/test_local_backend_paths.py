"""The local worker turns an input key into a path itself (threat model T33).

The API refuses input keys with traversal segments (validate_input_key) before a job
reaches the local worker's queue. The worker checks again where the key becomes a path,
so whatever is queued, it reads nothing outside the input directory.
"""

import uuid

import pytest

from pointcloud_detect.api import local_backend

UPLOAD_ID = str(uuid.uuid4())


@pytest.fixture
def input_dir(tmp_path, monkeypatch):
    root = tmp_path / "input"
    (root / UPLOAD_ID).mkdir(parents=True)
    monkeypatch.setattr(local_backend, "INPUT_DIR", root)
    return root


def test_a_local_upload_key_names_its_file(input_dir):
    path = local_backend._local_input_path(f"uploads/{UPLOAD_ID}/scan.ply")
    assert path == (input_dir / UPLOAD_ID / "scan.ply").resolve()


@pytest.mark.parametrize(
    "key",
    [
        "uploads/../secret.ply",
        "uploads/../../etc/passwd",
        f"uploads/{UPLOAD_ID}/..",
        f"uploads/{UPLOAD_ID}/.",
        f"uploads/{UPLOAD_ID}/",
        f"uploads/{UPLOAD_ID}/a\\..\\b.ply",
        f"uploads/{UPLOAD_ID}/a/b.ply",
        "uploads/not-a-uuid/scan.ply",
        f"results/{UPLOAD_ID}/scan.ply",
        "uploads/scan.ply",
    ],
)
def test_other_keys_are_refused(input_dir, key):
    with pytest.raises(ValueError, match="not a local upload key"):
        local_backend._local_input_path(key)


def test_a_symlink_out_of_the_input_directory_is_refused(input_dir, tmp_path):
    outside = tmp_path / "outside.ply"
    outside.write_text("not an upload")
    (input_dir / UPLOAD_ID / "scan.ply").symlink_to(outside)
    with pytest.raises(ValueError, match="not a local upload key"):
        local_backend._local_input_path(f"uploads/{UPLOAD_ID}/scan.ply")
