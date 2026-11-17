"""Pinned checkpoint fetching (backends/checkpoints.py): what loads, what is refused."""

import hashlib
import re
from pathlib import Path

import pytest

from pointcloud_detect.backends import checkpoints
from pointcloud_detect.backends.mmdet3d import DETECTION_CONFIGS, SEGMENTATION_CONFIGS

URL = "https://download.openmmlab.com/example/model_20220101-0123abcd.pth"
GOOD = b"checkpoint bytes"
GOOD_SHA = hashlib.sha256(GOOD).hexdigest()


@pytest.fixture(autouse=True)
def opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pin/cache tests below run with the non-commercial opt-in; the gate's own
    tests at the end of this file clear it."""
    monkeypatch.setenv(checkpoints.NONCOMMERCIAL_ENV, "1")


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(checkpoints.PINNED_SHA256, URL, GOOD_SHA)


def writer(data: bytes, calls: list[str] | None = None):
    def download(url: str, dest: Path) -> None:
        if calls is not None:
            calls.append(url)
        dest.write_bytes(data)

    return download


def test_every_referenced_checkpoint_is_pinned() -> None:
    urls = [
        m["checkpoint"]
        for m in (*DETECTION_CONFIGS.values(), *SEGMENTATION_CONFIGS.values())
    ]
    assert urls
    for url in urls:
        assert re.fullmatch(r"[0-9a-f]{64}", checkpoints.PINNED_SHA256[url]), url
        assert url.startswith("https://"), url


def test_openmmlab_filename_hash_prefixes_agree_with_the_pins() -> None:
    # OpenMMLab names most files <name>-<first 8 hex of the SHA-256>.pth, which is an
    # independent check on the pins for those files.
    checked = 0
    for url, sha in checkpoints.PINNED_SHA256.items():
        m = re.search(r"-([0-9a-f]{8})\.pth$", url)
        if m:
            assert sha.startswith(m.group(1)), url
            checked += 1
    assert checked >= 5


@pytest.mark.usefixtures("pinned")
def test_downloads_once_then_uses_the_verified_cache(tmp_path: Path) -> None:
    calls: list[str] = []
    first = checkpoints.fetch(URL, directory=tmp_path, download=writer(GOOD, calls))
    second = checkpoints.fetch(URL, directory=tmp_path, download=writer(GOOD, calls))
    assert first == second == tmp_path / "model_20220101-0123abcd.pth"
    assert first.read_bytes() == GOOD
    assert calls == [URL]


@pytest.mark.usefixtures("pinned")
def test_a_download_that_does_not_match_is_refused_and_removed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="expected"):
        checkpoints.fetch(URL, directory=tmp_path, download=writer(b"tampered"))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.usefixtures("pinned")
def test_a_cached_file_that_no_longer_matches_is_refetched(tmp_path: Path) -> None:
    (tmp_path / "model_20220101-0123abcd.pth").write_bytes(b"swapped in the cache")
    calls: list[str] = []
    path = checkpoints.fetch(URL, directory=tmp_path, download=writer(GOOD, calls))
    assert path.read_bytes() == GOOD
    assert calls == [URL]


@pytest.mark.usefixtures("pinned")
def test_a_cached_file_that_no_longer_matches_and_a_bad_download_leave_nothing(
    tmp_path: Path,
) -> None:
    (tmp_path / "model_20220101-0123abcd.pth").write_bytes(b"swapped in the cache")
    with pytest.raises(ValueError):
        checkpoints.fetch(URL, directory=tmp_path, download=writer(b"tampered"))
    assert list(tmp_path.iterdir()) == []


def test_an_unpinned_url_is_refused_without_downloading(tmp_path: Path) -> None:
    calls: list[str] = []
    with pytest.raises(ValueError, match="no pinned SHA-256"):
        checkpoints.fetch(
            "https://example.com/other.pth",
            directory=tmp_path,
            download=writer(GOOD, calls),
        )
    assert calls == []


def test_a_non_https_url_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    http = "http://download.openmmlab.com/example/model.pth"
    monkeypatch.setitem(checkpoints.PINNED_SHA256, http, GOOD_SHA)
    with pytest.raises(ValueError, match="HTTPS"):
        checkpoints.fetch(http, directory=tmp_path, download=writer(GOOD))


def test_cache_dir_follows_torch_home_then_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TORCH_HOME", "/data/torch")
    assert checkpoints.cache_dir() == Path("/data/torch/hub/checkpoints")
    monkeypatch.delenv("TORCH_HOME")
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/.cache")
    assert checkpoints.cache_dir() == Path("/tmp/.cache/torch/hub/checkpoints")


def test_enforce_weights_only_sets_the_override_unless_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TORCH_FORCE_WEIGHTS_ONLY_LOAD", raising=False)
    checkpoints.enforce_weights_only()
    assert checkpoints.os.environ["TORCH_FORCE_WEIGHTS_ONLY_LOAD"] == "1"
    monkeypatch.setenv("TORCH_FORCE_WEIGHTS_ONLY_LOAD", "0")
    checkpoints.enforce_weights_only()
    assert checkpoints.os.environ["TORCH_FORCE_WEIGHTS_ONLY_LOAD"] == "0"


# --- the non-commercial opt-in -------------------------------------------------------


@pytest.mark.usefixtures("pinned")
@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "maybe"])
def test_fetch_refuses_without_the_opt_in_and_never_downloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(checkpoints.NONCOMMERCIAL_ENV, raising=False)
    else:
        monkeypatch.setenv(checkpoints.NONCOMMERCIAL_ENV, value)
    calls: list[str] = []
    with pytest.raises(checkpoints.NonCommercialWeightsDisabled):
        checkpoints.fetch(URL, directory=tmp_path, download=writer(GOOD, calls))
    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_refusal_also_ignores_a_cached_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(checkpoints.PINNED_SHA256, URL, GOOD_SHA)
    (tmp_path / "model_20220101-0123abcd.pth").write_bytes(GOOD)
    monkeypatch.delenv(checkpoints.NONCOMMERCIAL_ENV)
    with pytest.raises(checkpoints.NonCommercialWeightsDisabled):
        checkpoints.fetch(URL, directory=tmp_path, download=writer(GOOD))


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " Yes "])
def test_truthy_values_opt_in(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(checkpoints.NONCOMMERCIAL_ENV, value)
    assert checkpoints.noncommercial_allowed()


def test_the_refusal_is_not_an_import_error() -> None:
    # inference.create_*_backend falls back to the stub on ImportError; a refusal
    # must fail the job instead of silently producing placeholder detections.
    assert not issubclass(checkpoints.NonCommercialWeightsDisabled, ImportError)
    assert issubclass(checkpoints.NonCommercialWeightsDisabled, RuntimeError)
