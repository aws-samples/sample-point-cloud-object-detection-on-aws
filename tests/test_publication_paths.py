"""The export's leak check refuses a `.kiro` directory at any depth (prepare.sh).

`.kiro/` is AI-assistant workspace configuration, which must not ship (PUBLICATION.md).
REMOVE_PATHS drops the top-level one, but a copy anywhere else, such as `docs/.kiro/`,
would pass every string rule of the leak check. So the leak check also refuses any path
with a `.kiro` component, in every commit on every ref.
"""

import os
import shutil
from pathlib import Path

import pytest

from tests._proc import Finished, minimal_env, run

ROOT = Path(__file__).resolve().parents[1]
PREPARE = ROOT / "scripts" / "publication" / "prepare.sh"

pytestmark = [
    pytest.mark.skipif(
        not PREPARE.exists(), reason="the export tooling is not in the published tree"
    ),
    pytest.mark.skipif(shutil.which("git") is None, reason="needs git"),
]

# No global or system git configuration (signing, hooks, a default branch) reaches the
# throwaway repositories, and commits get a fixed identity.
ENV = minimal_env(
    GIT_CONFIG_GLOBAL=os.devnull,
    GIT_CONFIG_NOSYSTEM="1",
    GIT_AUTHOR_NAME="test",
    GIT_AUTHOR_EMAIL="test@example.invalid",
    GIT_COMMITTER_NAME="test",
    GIT_COMMITTER_EMAIL="test@example.invalid",
)


def git(repo: Path, *args: str) -> None:
    run("git", ["-C", str(repo), *args], env=ENV, timeout=60, check=True)


def repo_with(tmp_path: Path, *commits: dict[str, str | None]) -> Path:
    """A repository with one commit per mapping of path to text (None deletes)."""
    repo = tmp_path / "export"
    repo.mkdir()
    git(repo, "init", "-q")
    for files in commits:
        for name, text in files.items():
            path = repo / name
            if text is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "commit")
    return repo


def leak_check(repo: Path) -> Finished:
    return run("bash", [str(PREPARE), "verify", str(repo)], env=ENV, timeout=300)


def test_a_nested_kiro_directory_is_refused(tmp_path):
    repo = repo_with(
        tmp_path, {"README.md": "x\n", "docs/.kiro/steering/rules.md": "r\n"}
    )
    result = leak_check(repo)
    assert result.returncode != 0
    assert "LEAK   .kiro path docs/.kiro/steering/rules.md" in result.stdout
    assert "do not publish it" in result.stderr


def test_a_kiro_directory_only_in_history_is_refused(tmp_path):
    repo = repo_with(
        tmp_path,
        {"README.md": "x\n", "src/.kiro/specs/plan.md": "s\n"},
        {"src/.kiro/specs/plan.md": None},
    )
    result = leak_check(repo)
    assert result.returncode != 0
    assert "LEAK   .kiro path src/.kiro/specs/plan.md" in result.stdout


def test_names_that_only_contain_kiro_are_not_refused(tmp_path):
    repo = repo_with(
        tmp_path, {"README.md": "x\n", "docs/kiro-notes.md": "n\n", "a.kiro": "k\n"}
    )
    result = leak_check(repo)
    assert "CLEAN  no .kiro path at any depth, in any commit" in result.stdout
    assert "LEAK" not in result.stdout
