"""tests/_proc.py: the constraints the tests' one process start holds them to."""

import subprocess

import pytest

from tests._proc import minimal_env, run


def test_only_the_five_named_programs_can_be_started() -> None:
    for program in ("sh", "/bin/sh", "python3", "curl"):
        only = "start only python, bash, helm, semgrep, git"
        with pytest.raises(ValueError, match=only):
            run(program, ["-c", "true"], env=minimal_env(), timeout=5)


def test_the_runners_environment_is_not_inherited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "planted-for-this-test")
    out = run(
        "python",
        ["-c", "import os; print('AWS_SECRET_ACCESS_KEY' in os.environ)"],
        env=minimal_env(),
        timeout=30,
    )
    assert out.stdout.strip() == "False"
    assert "AWS_SECRET_ACCESS_KEY" not in minimal_env()


def test_what_the_caller_passes_is_all_the_child_gets() -> None:
    out = run(
        "python",
        ["-c", "import os; print(os.environ.get('ONLY_THIS'))"],
        env={"ONLY_THIS": "yes"},
        timeout=30,
    )
    assert out.stdout.strip() == "yes"


def test_a_child_past_its_timeout_is_killed() -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        run(
            "python",
            ["-c", "import time; time.sleep(60)"],
            env=minimal_env(),
            timeout=1,
        )


def test_check_raises_on_a_nonzero_exit() -> None:
    with pytest.raises(subprocess.CalledProcessError):
        run(
            "python",
            ["-c", "raise SystemExit(3)"],
            env=minimal_env(),
            timeout=30,
            check=True,
        )
