"""The rules in .semgrep/invariants.yaml: the tree satisfies them, and they still bite.

Three reviewed lines carry an inline suppression for a scanner rule whose finding was
reviewed and kept (docs/security/THREAT_MODEL.md, 4.4). The rules pin each line's
reviewed shape and look for lines like them anywhere else. Each mutation below is one
edit a later change could make that would make a suppression's reason untrue; the rule
named beside it must catch it, so a passing run shows the rules can fail, not only that
they pass. The child's environment allowlist is held by a test instead
(test_parser_isolation.py, test_supervisor_credentials_never_reach_the_child).

They run semgrep and skip when it is not installed. CI's invariants job sets
REQUIRE_SEMGREP=1 so a runner that loses semgrep fails instead of quietly skipping.
Standard library and pytest only, like that job.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from tests._proc import minimal_env, run

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / ".semgrep" / "invariants.yaml"

if shutil.which("semgrep") is None:
    if os.environ.get("REQUIRE_SEMGREP") == "1":
        raise RuntimeError("REQUIRE_SEMGREP=1 but semgrep is not on PATH")
    pytest.skip("semgrep not installed", allow_module_level=True)

PROCESS = "pcd.process-start-outside-reviewed-sites"
LOG = "pcd.credential-word-log-outside-reviewed-site"
TAINT = "pcd.job-data-reaches-child-argv"

SIDECAR = "src/pointcloud_detect/worker/parser_sidecar.py"
PROC = "tests/_proc.py"
CALL = "        _child_command(job_dir),\n        env=_child_env(home),"
HELPER_END = "    return Finished(returncode=proc.returncode, stdout=out, stderr=err)\n"

# What a later edit could do to a reviewed line: (file, exact text, replacement, rule).
MUTATIONS = {
    "child argv extended from the environment": (
        SIDECAR,
        CALL,
        CALL.replace(
            "_child_command(job_dir),",
            '_child_command(job_dir) + os.environ.get("EXTRA", "").split(),',
        ),
        PROCESS,
    ),
    "child argv extended from the request": (
        SIDECAR,
        CALL,
        CALL.replace(
            "_child_command(job_dir),",
            "_child_command(job_dir)"
            " + [str(read_json_file(job_dir / IN_PROGRESS_NAME))],",
        ),
        PROCESS,
    ),
    "child inherits the supervisor's environment": (
        SIDECAR,
        "        env=_child_env(home),",
        "        env={**os.environ, **_child_env(home)},",
        PROCESS,
    ),
    "request chooses the job directory": (
        SIDECAR,
        "        code = _run_child(job_dir, timeout_seconds, home)",
        "        code = _run_child("
        'job_dir / str(request.get("dir", "")), timeout_seconds, home)',
        TAINT,
    ),
    "refusal record logs values": (
        SIDECAR,
        '            ", ".join(problems),',
        '            ", ".join(f"{p}={os.environ.get(p)}" for p in problems),',
        LOG,
    ),
    "helper passes the runner's environment": (
        PROC,
        "argv, env=dict(env), stdin=stdin",
        "argv, env={**os.environ, **env}, stdin=stdin",
        PROCESS,
    ),
    "helper runs any program": (
        PROC,
        "    argv = [_executable(program), *(str(a) for a in args)]",
        "    argv = [program, *(str(a) for a in args)]",
        PROCESS,
    ),
    "a new process start": (
        PROC,
        HELPER_END,
        HELPER_END
        + '\n\ndef extra() -> None:\n    subprocess.run(["true"], check=True)\n',
        PROCESS,
    ),
}


# Every shape a later log line could name a credential in. The log rule must catch each:
# it once matched only the first, so the other eight would have passed CI.
# PLANTED_READ is the environment read each one plants, not a credential.
PLANTED_READ = 'os.environ.get("AWS_SESSION_TOKEN")'
LEAKS = {
    "%s": 'logger.info("session token: %s", ' + PLANTED_READ + ")",
    "%(name)s": 'logger.info("session token: %(t)s", {"t": ' + PLANTED_READ + "})",
    "an f-string": (
        'logger.info(f"session token: {' + PLANTED_READ.replace('"', "'") + '}")'
    ),
    "str.format": 'logger.info("session token: {}".format(' + PLANTED_READ + "))",
    "the % operator": 'logger.info("session token: %s" % ' + PLANTED_READ + ")",
    "concatenation": 'logger.info("session token: " + ' + PLANTED_READ + ")",
    "extra=": 'logger.info("session token", extra={"t": ' + PLANTED_READ + "})",
    "the logging module": 'logging.info("session token: %s", ' + PLANTED_READ + ")",
    "logger.log": 'logger.log(logging.INFO, "session token: %s", ' + PLANTED_READ + ")",
}
for shape, leak in LEAKS.items():
    MUTATIONS[f"another log line names a credential ({shape})"] = (
        SIDECAR,
        "        return 2\n    devices = gpu_devices()",
        "        return 2\n    " + leak + "\n    devices = gpu_devices()",
        LOG,
    )


def _scan(root: Path, files: list[str], home: Path) -> dict[str, object]:
    # --disable-nosem: no inline comment can silence these rules. Files, not
    # directories, because Semgrep's default ignore list skips tests/.
    out = run(
        "semgrep",
        [
            "scan",
            "--metrics=off",
            "--disable-version-check",
            "--disable-nosem",
            "--quiet",
            "--json",
            "--config",
            str(RULES),
            *(str(root / f) for f in files),
        ],
        env=minimal_env(HOME=str(home), SEMGREP_SEND_METRICS="off"),
        timeout=300,
    )
    assert out.returncode in (0, 1), out.stderr
    return json.loads(out.stdout)


def _rule_ids(report: dict[str, object]) -> list[str]:
    results = report["results"]
    assert isinstance(results, list)
    return sorted(str(r["check_id"]) for r in results)


def _caught(report: dict[str, object], rule: str) -> bool:
    # A rule loaded from a file is namespaced by the file's path, so match the end.
    return any(rule_id.endswith(rule) for rule_id in _rule_ids(report))


def test_the_tree_satisfies_the_rules(tmp_path: Path) -> None:
    files = sorted(
        str(p.relative_to(ROOT))
        for top in ("src", "tests", "scripts")
        if (ROOT / top).is_dir()
        for p in (ROOT / top).rglob("*.py")
        if "__pycache__" not in p.parts
    )
    report = _scan(ROOT, files, tmp_path)
    assert report["errors"] == []
    paths = report["paths"]
    assert isinstance(paths, dict)
    assert len(paths["scanned"]) == len(files)
    assert _rule_ids(report) == []


@pytest.mark.parametrize("name", MUTATIONS)
def test_each_reviewed_line_is_guarded(tmp_path: Path, name: str) -> None:
    rel, old, new, rule = MUTATIONS[name]
    text = (ROOT / rel).read_text()
    assert text.count(old) == 1, f"{name}: the text to mutate is gone or ambiguous"
    copy = tmp_path / "tree" / rel
    copy.parent.mkdir(parents=True)
    copy.write_text(text.replace(old, new))
    report = _scan(tmp_path / "tree", [rel], tmp_path)
    assert report["errors"] == []
    assert _caught(report, rule), f"{name}: {rule} did not fire"
