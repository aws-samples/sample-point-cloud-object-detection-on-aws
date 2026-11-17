"""Tests for scripts/check_citations.py, the threat model's citation check.

CI trusts this script to say every `path:line` in docs/security/THREAT_MODEL.md
resolves. If it stopped recognising a citation form, a broken citation would pass
silently, so these pin the forms the document actually uses.
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_citations", ROOT / "scripts" / "check_citations.py"
)
assert _spec and _spec.loader
cc = importlib.util.module_from_spec(_spec)
sys.modules["check_citations"] = cc  # dataclasses resolve types via sys.modules
_spec.loader.exec_module(cc)

LINES = len((ROOT / "pyproject.toml").read_text().splitlines())


def _doc(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "doc.md"
    p.write_text(text)
    return p


def test_path_line_and_shorthand(tmp_path: Path) -> None:
    doc = _doc(tmp_path, "See `pyproject.toml:1-2`, `:3` and `README.md`.\n")
    found = [(c.path, c.start, c.end) for c in cc.parse(doc)]
    assert found == [
        ("pyproject.toml", 1, 2),
        ("pyproject.toml", 3, None),
        ("README.md", None, None),
    ]


def test_broken_citations_are_reported(tmp_path: Path) -> None:
    doc = _doc(
        tmp_path,
        f"`pyproject.toml:{LINES + 1}` and `src/pointcloud_detect/api/auth.py:1`\n",
    )
    problems = [cc.problems(c) for c in cc.parse(doc)]
    assert problems[0] is not None and "past end of file" in problems[0]
    assert problems[1] == "file does not exist"


def test_non_repository_spans_are_ignored(tmp_path: Path) -> None:
    # Backticked text that is not a repository path, and citations of another
    # revision, are not checked against the working tree; a shorthand after a
    # revision citation belongs to neither.
    doc = _doc(
        tmp_path,
        "`uploads/*` `/api` `git show 2bba4c9:src/x.py` `:5` "
        "`origin/main:src/pointcloud_detect/api/app.py`\n",
    )
    assert cc.parse(doc) == []


def test_shorthand_does_not_cross_lines(tmp_path: Path) -> None:
    doc = _doc(tmp_path, "`pyproject.toml:1`\n`:2`\n")
    assert [c.label() for c in cc.parse(doc)] == ["pyproject.toml:1"]


def test_threat_model_citations_resolve() -> None:
    """The same check CI runs, so a local test run catches it too."""
    cites = cc.parse(ROOT / "docs" / "security" / "THREAT_MODEL.md")
    assert len(cites) > 900
    assert [c.label() for c in cites if cc.problems(c)] == []


def test_export_removed_paths_are_skipped_only_in_an_export(tmp_path: Path) -> None:
    # The internal tree: the export tooling is there, so its citations are checked.
    (tmp_path / "scripts" / "publication").mkdir(parents=True)
    assert not cc.removed_by_export("scripts/publication/prepare.sh", tmp_path)
    # The published tree: the tooling is gone, and so is every path it removes.
    (tmp_path / "scripts" / "publication").rmdir()
    assert cc.removed_by_export("scripts/publication/prepare.sh", tmp_path)
    assert cc.removed_by_export("docs/PUBLICATION.md", tmp_path)
    assert not cc.removed_by_export("docs/PUBLICATION.mdx", tmp_path)
    assert not cc.removed_by_export("scripts/check_citations.py", tmp_path)


@pytest.mark.skipif(
    not (ROOT / "scripts" / "publication" / "prepare.sh").exists(),
    reason="the export tooling is not in the published tree",
)
def test_export_removed_matches_prepare_sh() -> None:
    text = (ROOT / "scripts" / "publication" / "prepare.sh").read_text()
    block = re.search(r"^REMOVE_PATHS=\((.*?)^\)", text, re.S | re.M)
    assert block
    paths = [
        line.strip()
        for line in block.group(1).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert tuple(paths) == cc.EXPORT_REMOVED


def _trees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """An old checkout and a new working tree of one file, two lines inserted."""
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "notes.txt").write_text("a\nb\nc\nd\n")
    (new / "notes.txt").write_text("x\ny\na\nb\nC\nd\n")
    monkeypatch.setattr(cc, "ROOT", new)
    return old, new


def test_remap_from_an_old_checkout_moves_unchanged_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, _ = _trees(tmp_path, monkeypatch)
    doc = _doc(tmp_path, "`notes.txt:1-2` and `notes.txt:3`\n")
    monkeypatch.setattr(sys, "argv", ["cc", str(doc), "--remap-from", str(old)])
    assert cc.main() == 0
    # Lines 1-2 moved to 3-4; line 3 was edited, so it is left for a person.
    assert doc.read_text() == "`notes.txt:3-4` and `notes.txt:3`\n"


def test_changed_since_names_edited_citations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    old, _ = _trees(tmp_path, monkeypatch)
    doc = _doc(tmp_path, "`notes.txt:3` `notes.txt:4`\n")
    monkeypatch.setattr(sys, "argv", ["cc", str(doc), "--changed-since", str(old)])
    cc.main()
    out = capsys.readouterr().out
    assert "notes.txt:3: cited lines edited since" in out
    assert "notes.txt:4: cited lines edited" not in out
    assert doc.read_text() == "`notes.txt:3` `notes.txt:4`\n"


def test_an_old_tree_that_is_not_a_directory_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _trees(tmp_path, monkeypatch)
    doc = _doc(tmp_path, "`notes.txt:1`\n")
    monkeypatch.setattr(sys, "argv", ["cc", str(doc), "--remap-from", "HEAD~1"])
    assert cc.main() == 2
