"""Tests for infrastructure/scripts: the deploy, teardown, image and state scripts.

The static checks pin what the scripts must and must not do, per the threat model
entries each class names (docs/security/THREAT_MODEL.md). The behavioural tests run
build-push.sh, destroy.sh and plan-adoption.sh from a copy of the scripts against stub
terraform, aws, kubectl, helm, docker and git on PATH. They reach no AWS account and
need only bash. generate_imports.py is tested directly, reading its plan from stdin.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

from tests._proc import Finished, minimal_env, run

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "infrastructure" / "scripts"

_spec = importlib.util.spec_from_file_location(
    "generate_imports", SCRIPTS / "lib" / "generate_imports.py"
)
assert _spec and _spec.loader
gi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gi)

BPA_OPT_IN = "module.s3.aws_s3_account_public_access_block.account_opt_in[0]"
BPA_LEGACY = "module.s3.aws_s3_account_public_access_block.account"
BACKEND = "123456789012.dkr.ecr.us-east-1.amazonaws.com/pointcloud-detection-backend"
FRONTEND = "123456789012.dkr.ecr.us-east-1.amazonaws.com/pointcloud-detection-frontend"


def _commands(name: str) -> list[str]:
    """The script's commands: comment lines dropped, continuation lines joined."""
    out: list[str] = []
    pending = ""
    for line in (SCRIPTS / name).read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        out.append(pending + line)
        pending = ""
    return out


def _command(name: str, marker: str) -> str:
    matches = [c for c in _commands(name) if marker in c]
    assert len(matches) == 1, f"{marker!r}: {len(matches)} commands in {name}"
    return matches[0]


def _index(name: str, marker: str) -> int:
    return next(i for i, c in enumerate(_commands(name)) if marker in c)


def _trust_subjects() -> dict[str, tuple[str, str]]:
    """IRSA trust subjects in the add-ons module: key -> (namespace, account)."""
    text = (ROOT / "infrastructure" / "modules" / "addons" / "main.tf").read_text()
    pattern = r'(\w+)\s*=\s*\{\s*namespace\s*=\s*"([^"]+)",\s*name\s*=\s*"([^"]+)"'
    return {k: (ns, sa) for k, ns, sa in re.findall(pattern, text)}


# --- stubs -------------------------------------------------------------------------

# Every stub appends "<tool> <args>" to $STUB_LOG, then behaves as the test's STUB_*
# variables say.
_STUBS = {
    "git": r"""
case "$*" in
  *"rev-parse --git-dir"*) exit 0 ;;
  *"status --porcelain"*)
    # Honour a pathspec after --, as git does: only changes under one of its paths.
    specs=()
    seen=""
    for arg in "$@"; do
      if [ -n "${seen}" ]; then specs+=("${arg}"); fi
      [ "${arg}" = -- ] && seen=1
    done
    while IFS= read -r line; do
      [ -n "${line}" ] || continue
      path="${line:3}"
      if [ ${#specs[@]} -eq 0 ]; then printf '%s\n' "${line}"; continue; fi
      for spec in "${specs[@]}"; do
        case "${path}" in
          "${spec}" | "${spec}"/*) printf '%s\n' "${line}"; break ;;
        esac
      done
    done <<<"${STUB_GIT_STATUS:-}" ;;
  *"rev-parse --short=12 HEAD"*) echo "${STUB_SHA:-0123456789ab}" ;;
  *) exit 99 ;;
esac
""",
    "docker": r"""
[ "$1" = login ] && cat >/dev/null
echo "docker output"
""",
    "aws": r"""
case "$*" in
  "ecr get-login-password"*) echo password ;;
  "ecr describe-images"*)
    if [ -n "${STUB_ECR_ERROR:-}" ]; then echo "${STUB_ECR_ERROR}" >&2; exit 254; fi
    for repo in ${STUB_ECR_EXISTING:-}; do
      case "$*" in *"--repository-name ${repo} "*) echo sha256:0; exit 0 ;; esac
    done
    echo "An error occurred (ImageNotFoundException)" >&2; exit 254 ;;
  *"eks describe-cluster"*"cluster.[arn,endpoint]"*)
    printf '%s\t%s\n' arn:aws:eks:us-east-1:123456789012:cluster/demo \
      https://ABC123.gr7.us-east-1.eks.amazonaws.com ;;
  "eks describe-cluster"*)
    if [ "${STUB_CLUSTER:-ACTIVE}" = gone ]; then
      echo "An error occurred (ResourceNotFoundException)" >&2; exit 254
    fi
    echo "${STUB_CLUSTER:-ACTIVE}" ;;
  "eks update-kubeconfig"*)
    while [ $# -gt 0 ]; do [ "$1" = --kubeconfig ] && touch "$2"; shift; done ;;
  "sts get-caller-identity --query Account"*) echo 123456789012 ;;
  "sts get-caller-identity"*) echo arn:aws:iam::123456789012:role/operator ;;
  "s3api head-bucket"*)
    if [ -z "${STUB_BUCKET_EXISTS:-}" ]; then
      echo "An error occurred (404) when calling HeadBucket: Not Found" >&2
      exit 254
    fi ;;
  "s3api get-bucket-policy"*)
    if [ -z "${STUB_BUCKET_POLICY:-}" ]; then
      echo "An error occurred (NoSuchBucketPolicy)" >&2; exit 254
    fi
    printf '%s\n' "${STUB_BUCKET_POLICY}" ;;
  "s3api get-bucket-versioning"*) echo None ;;
  "s3api get-bucket-encryption"*)
    if [ -z "${STUB_STATE_SSE:-}" ]; then
      echo "An error occurred (ServerSideEncryptionConfigurationNotFoundError)" >&2
      exit 254
    fi
    printf '%s\n' "${STUB_STATE_SSE}" ;;
  "kms describe-key --key-id alias/aws/s3 "*)
    echo arn:aws:kms:us-east-1:123456789012:key/aws-s3-managed ;;
  "kms describe-key --key-id "*)
    echo "$4" ;;
  # One page holding one version, until a delete-objects call for the bucket is logged;
  # empty after that. $4 is the bucket: s3api list-object-versions --bucket <name>.
  "s3api list-object-versions"*)
    case " ${STUB_GONE_BUCKETS:-} " in
      *" $4 "*)
        echo "An error occurred (NoSuchBucket) when calling ListObjectVersions" >&2
        exit 254 ;;
    esac
    if grep -q "^aws s3api delete-objects --bucket $4 " "${STUB_LOG}"; then
      printf '{\n    "Objects": [],\n    "Quiet": true\n}\n'
    else
      printf '{\n    "Objects": [\n        {\n'
      printf '            "Key": "a key",\n            "VersionId": "v1"\n'
      printf '        }\n    ],\n    "Quiet": true\n}\n'
    fi ;;
  "s3api delete-objects"*)
    if [ -n "${STUB_DELETE_ERROR:-}" ]; then
      printf 'a key\tAccessDenied\tAccess Denied\n'
    else
      echo None
    fi ;;
  "logs describe-log-groups"*)
    prefix=""
    prev=""
    for arg in "$@"; do
      [ "${prev}" = --log-group-name-prefix ] && prefix="${arg}"
      prev="${arg}"
    done
    for group in ${STUB_LOG_GROUPS:-}; do
      case "${group}" in "${prefix}"*) echo "${group}" ;; esac
    done ;;
esac
""",
    "terraform": r"""
case "$1 ${2:-}" in
  "state list")
    if [ -n "${STUB_STATE_ERROR:-}" ]; then echo "${STUB_STATE_ERROR}" >&2; exit 1; fi
    cat "${STUB_STATE_FILE}" ;;
  "state rm") ;;
  # A bucket named after the address: ...buckets["input"] is demo-input, ...logs is
  # demo-logs.
  "state show")
    key="$(sed -E 's/.*\["([^"]+)"\]$/\1/; s/.*\.//' <<<"${!#}")"
    printf '# %s:\nresource "aws_s3_bucket" "this" {\n' "${!#}"
    printf '    bucket             = "demo-%s"\n' "${key}"
    printf '    bucket_domain_name = "demo-%s.s3.amazonaws.com"\n}\n' "${key}" ;;
  "output -raw")
    var="STUB_OUT_$3"
    if [ -z "${!var:-}" ]; then echo "Output not found" >&2; exit 1; fi
    printf '%s' "${!var}" ;;
  "show -json") cat "${STUB_PLAN_JSON}" ;;
  plan*)
    detailed=""
    for arg in "$@"; do
      case "${arg}" in
        -out=*) echo "stub plan" > "${arg#-out=}" ;;
        -detailed-exitcode) detailed=1 ;;
      esac
    done
    echo "Plan: stub (sensitive value)"
    if [ -n "${detailed}" ]; then exit "${STUB_PLAN_RC:-2}"; fi ;;
  apply*)
    [ -f "$2" ] || exit 1
    echo "applied saved plan" >> "${STUB_LOG}"
    exit "${STUB_APPLY_RC:-0}" ;;
esac
""",
    "kubectl": r"""
echo "KUBECONFIG=${KUBECONFIG:-}" >> "${STUB_LOG}"
case "$*" in "auth can-i"*) exit "${STUB_CAN_I_RC:-0}" ;; esac
""",
    "helm": "",
}


def _stub_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool, body in _STUBS.items():
        stub = bin_dir / tool
        stub.write_text(f'#!/bin/bash\necho "{tool} $*" >> "${{STUB_LOG}}"\n{body}')
        stub.chmod(0o755)
    return bin_dir


def _tree(tmp_path: Path) -> Path:
    """A copy of infrastructure/scripts, so no run writes into the repository."""
    infra = tmp_path / "repo" / "infrastructure"
    shutil.copytree(SCRIPTS, infra / "scripts")
    (infra / "environments" / "dev").mkdir(parents=True)
    (infra / "environments" / "dev" / "terraform.tfvars").write_text("")
    return infra


def _run(
    tmp_path: Path, script: str, *args: str, stdin: str = "", **env: str
) -> tuple[Finished, list[str]]:
    infra = tmp_path / "repo" / "infrastructure"
    if not infra.exists():
        _tree(tmp_path)
    bin_dir = tmp_path / "bin"
    if not bin_dir.exists():
        _stub_bin(tmp_path)
    log = tmp_path / "calls.log"
    log.write_text("")
    full_env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "STUB_LOG": str(log),
        **env,
    }
    proc = run(
        "bash",
        [str(infra / "scripts" / script), *args],
        env=full_env,
        timeout=60,
        input=stdin,
    )
    return proc, log.read_text().splitlines()


def _plan_dir(calls: list[str]) -> Path:
    """The directory the script saved its Terraform plan in, from the stub's log."""
    plan = next(c for c in calls if c.startswith("terraform plan"))
    out = next(a for a in plan.split() if a.startswith("-out="))
    return Path(out.removeprefix("-out=")).parent


# --- every script ------------------------------------------------------------------


@pytest.mark.parametrize("script", sorted(p.name for p in SCRIPTS.glob("*.sh")))
def test_script_parses(script: str) -> None:
    proc = run("bash", ["-n", str(SCRIPTS / script)], env=minimal_env(), timeout=60)
    assert proc.returncode == 0, proc.stderr


# --- T29: images by commit ---------------------------------------------------------


class TestImageTags:
    """T29: images are tagged and deployed by commit, never by `latest`."""

    def test_no_script_pushes_or_deploys_latest(self) -> None:
        for script in SCRIPTS.glob("*.sh"):
            for command in _commands(script.name):
                assert ":latest" not in command, (script.name, command)
                assert "image.tag=latest" not in command, (script.name, command)

    def test_each_chart_gets_the_tag_its_image_was_pushed_under(
        self, tmp_path: Path
    ) -> None:
        """build-push.sh pushes the backend as <base>-<variant> and the frontend as
        <base>, and reports the backend's tag. deploy.sh gave both charts that one,
        so the frontend pulled a tag that does not exist, in every variant: found as
        ImagePullBackOff on a cluster. Each chart is now checked against the tag the
        build actually pushed its image under.
        """
        backend = _command("deploy.sh", "helm upgrade --install pointcloud-backend")
        assert '--set-string image.tag="${IMAGE_TAG}"' in backend
        frontend = _command("deploy.sh", "helm upgrade --install pointcloud-frontend")
        assert '--set-string image.tag="${FRONTEND_IMAGE_TAG}"' in frontend
        build = _command("deploy.sh", 'BUILD_OUTPUT="$("${SCRIPT_DIR}/build-push.sh"')
        assert "${ECR_BACKEND}" in build and "${ECR_FRONTEND}" in build
        assert 'IMAGE_TAG="${BUILD_OUTPUT#IMAGE_TAG=}"' in _commands("deploy.sh")
        derive = _command("deploy.sh", "FRONTEND_IMAGE_TAG=")
        for variant in ("gpu", "stub"):
            proc, calls = _run(
                tmp_path / variant,
                "build-push.sh",
                "--variant",
                variant,
                "us-east-1",
                BACKEND,
                FRONTEND,
            )
            assert proc.returncode == 0, proc.stderr
            image_tag = proc.stdout.strip().removeprefix("IMAGE_TAG=")
            pushed = {
                c.split()[-1].rsplit(":", 1)[0]: c.split()[-1].rsplit(":", 1)[1]
                for c in calls
                if c.startswith("docker push")
            }
            assert pushed[BACKEND] == image_tag
            out = run(
                "bash",
                ["-c", f'{derive}\nprintf %s "$FRONTEND_IMAGE_TAG"'],
                env=minimal_env(IMAGE_TAG=image_tag, BUILD_VARIANT=variant),
                timeout=30,
            )
            assert out.returncode == 0, out.stderr
            assert out.stdout == pushed[FRONTEND], variant

    def test_dirty_tree_fails_before_terraform(self) -> None:
        assert _index("deploy.sh", 'build-push.sh" --variant') < _index(
            "deploy.sh", "terraform init"
        )

    def test_stdout_is_only_the_tag(self, tmp_path: Path) -> None:
        # No --variant: gpu (the default). Backend tag is variant-suffixed; frontend,
        # which does not change between variants, keeps the bare SHA.
        proc, calls = _run(tmp_path, "build-push.sh", "us-east-1", BACKEND, FRONTEND)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "IMAGE_TAG=0123456789ab-gpu\n"
        pushes = [c for c in calls if c.startswith("docker push")]
        assert pushes == [
            f"docker push {BACKEND}:0123456789ab-gpu",
            f"docker push {FRONTEND}:0123456789ab",
        ]

    def test_existing_tag_is_not_rebuilt(self, tmp_path: Path) -> None:
        proc, calls = _run(
            tmp_path,
            "build-push.sh",
            "us-east-1",
            BACKEND,
            FRONTEND,
            STUB_ECR_EXISTING=" ".join(
                url.split("/")[1] for url in (BACKEND, FRONTEND)
            ),
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "IMAGE_TAG=0123456789ab-gpu\n"
        assert not [c for c in calls if c.startswith("docker")]

    def test_dirty_tree_is_refused(self, tmp_path: Path) -> None:
        proc, calls = _run(
            tmp_path,
            "build-push.sh",
            "us-east-1",
            BACKEND,
            FRONTEND,
            STUB_GIT_STATUS=" M src/app.py\n?? notes.txt\n",
        )
        assert proc.returncode != 0
        assert proc.stdout == ""
        assert "src/app.py" in proc.stderr
        assert not [c for c in calls if c.startswith(("docker", "aws"))]

    def test_edited_tfvars_do_not_make_the_tree_dirty(self, tmp_path: Path) -> None:
        # The deploy instructions ask for an edited terraform.tfvars (the placeholder
        # CIDR, alarm_email, principal ARNs); none of it goes into an image.
        proc, calls = _run(
            tmp_path,
            "build-push.sh",
            "--check",
            STUB_GIT_STATUS=(
                " M infrastructure/environments/dev/terraform.tfvars\n"
                "?? docs/notes.md\n"
            ),
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "IMAGE_TAG=0123456789ab-gpu\n"
        status = next(c for c in calls if "status --porcelain" in c)
        assert status.split(" -- ", 1)[1].split() == [
            "Dockerfile",
            "Dockerfile.cpu",
            "Dockerfile.gpu",
            ".dockerignore",
            "pyproject.toml",
            "uv.lock",
            "README.md",
            "src",
            "scripts/patch_mmcv_cpu.py",
            "scripts/patch_mmengine_metadata.py",
            "scripts/patch_mmdet3d_lyft.py",
            "frontend",
        ]

    @pytest.mark.parametrize(
        "change", [" M frontend/src/App.tsx", "?? src/new.py", " M uv.lock"]
    )
    def test_any_image_input_makes_the_tree_dirty(
        self, tmp_path: Path, change: str
    ) -> None:
        proc, _ = _run(tmp_path, "build-push.sh", "--check", STUB_GIT_STATUS=change)
        assert proc.returncode != 0
        assert change.split()[-1] in proc.stderr

    def test_explicit_tag_builds_a_dirty_tree(self, tmp_path: Path) -> None:
        proc, _ = _run(
            tmp_path,
            "build-push.sh",
            "--check",
            STUB_GIT_STATUS=" M src/app.py\n",
            IMAGE_TAG="wip-1.2",
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "IMAGE_TAG=wip-1.2-gpu\n"

    @pytest.mark.parametrize("tag", ["latest", "has space", "-dash", "a:b"])
    def test_unusable_tags_are_refused(self, tmp_path: Path, tag: str) -> None:
        proc, _ = _run(tmp_path, "build-push.sh", "--check", IMAGE_TAG=tag)
        assert proc.returncode != 0
        assert proc.stdout == ""
        assert "is not a usable image tag" in proc.stderr


class TestBuildVariants:
    """The three deployment images: gpu (default, real GPU), cpu (real CPU), stub."""

    def test_gpu_is_the_default(self, tmp_path: Path) -> None:
        # No --variant resolves to gpu, the dev/prod image.
        proc, _ = _run(tmp_path, "build-push.sh", "--check")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "IMAGE_TAG=0123456789ab-gpu\n"

    @pytest.mark.parametrize("variant", ["gpu", "cpu", "stub"])
    def test_variant_tags_carry_the_variant(self, tmp_path: Path, variant: str) -> None:
        proc, _ = _run(tmp_path, "build-push.sh", "--variant", variant, "--check")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == f"IMAGE_TAG=0123456789ab-{variant}\n"

    def test_unknown_variant_is_refused(self, tmp_path: Path) -> None:
        proc, _ = _run(tmp_path, "build-push.sh", "--variant", "tpu", "--check")
        assert proc.returncode != 0
        assert proc.stdout == ""
        assert "--variant must be gpu, cpu or stub" in proc.stderr

    def test_gpu_builds_the_gpu_dockerfile(self, tmp_path: Path) -> None:
        proc, calls = _run(
            tmp_path,
            "build-push.sh",
            "--variant",
            "gpu",
            "us-east-1",
            BACKEND,
            FRONTEND,
        )
        assert proc.returncode == 0, proc.stderr
        build = next(
            c for c in calls if c.startswith("docker build") and "Dockerfile" in c
        )
        assert "Dockerfile.gpu" in build
        assert f"docker push {BACKEND}:0123456789ab-gpu" in calls

    def test_stub_builds_the_plain_dockerfile_and_suffixes_the_tag(
        self, tmp_path: Path
    ) -> None:
        proc, calls = _run(
            tmp_path,
            "build-push.sh",
            "--variant",
            "stub",
            "us-east-1",
            BACKEND,
            FRONTEND,
        )
        assert proc.returncode == 0, proc.stderr
        backend_build = next(
            c for c in calls if c.startswith("docker build") and f"{BACKEND}:" in c
        )
        # The plain Dockerfile, not .gpu or .cpu.
        assert "-f " in backend_build
        assert "Dockerfile.gpu" not in backend_build
        assert "Dockerfile.cpu" not in backend_build
        assert f"docker push {BACKEND}:0123456789ab-stub" in calls
        # Frontend keeps the bare SHA regardless of backend variant.
        assert f"docker push {FRONTEND}:0123456789ab" in calls

    def test_deploy_passes_the_variant_to_build_push(self) -> None:
        # Both the preflight --check and the build call thread BUILD_VARIANT through.
        check = _command("deploy.sh", 'PREFLIGHT_TAG="$("${SCRIPT_DIR}/build-push.sh"')
        assert '--variant "${BUILD_VARIANT}" --check' in check
        build = _command("deploy.sh", 'BUILD_OUTPUT="$("${SCRIPT_DIR}/build-push.sh"')
        assert '--variant "${BUILD_VARIANT}"' in build

    def test_deploy_defaults_variant_to_gpu(self) -> None:
        assert any(
            'BUILD_VARIANT="${BUILD_VARIANT:-gpu}"' in c for c in _commands("deploy.sh")
        )

    def test_deploy_keeps_noncommercial_weights_off_by_default(self) -> None:
        # The pretrained checkpoints are non-commercially licensed; deploy.sh passes
        # the opt-in through to the backend chart and defaults it to off.
        backend = _command("deploy.sh", "helm upgrade --install pointcloud-backend")
        flag = '--set models.allowNoncommercialWeights="${ALLOW_NONCOMMERCIAL}"'
        assert flag in backend
        text = (SCRIPTS / "deploy.sh").read_text()
        assert 'case "${ALLOW_NONCOMMERCIAL_WEIGHTS:-0}" in' in text
        assert '0 | false | no | "") ALLOW_NONCOMMERCIAL=false ;;' in text

    def test_failed_lookup_is_not_read_as_absent(self, tmp_path: Path) -> None:
        proc, calls = _run(
            tmp_path,
            "build-push.sh",
            "us-east-1",
            BACKEND,
            FRONTEND,
            STUB_ECR_ERROR="An error occurred (AccessDeniedException)",
        )
        assert proc.returncode != 0
        assert "AccessDeniedException" in proc.stderr
        assert not [c for c in calls if c.startswith("docker")]


def test_cognito_output_is_read_only_in_public_mode() -> None:
    """Port-forward mode has no Cognito, so its output is null, and Terraform records
    no null output: `terraform output cognito` then exits 1 instead of printing null.
    Read unconditionally under set -e, it ended every port-forward deploy before its
    first add-on. Found by deploying to a cluster; the other tests read commands only.
    """
    lines = (SCRIPTS / "deploy.sh").read_text().splitlines()
    read = "terraform output -json cognito"
    reads = [n for n, line in enumerate(lines) if read in line]
    assert len(reads) == 1
    assert lines[reads[0] - 1].strip() == 'if [ "${ACCESS_MODE}" = "public" ]; then'


# --- T31: per-user access ---------------------------------------------------------


def _deploy_text() -> str:
    return (SCRIPTS / "deploy.sh").read_text()


def _bash(script: str, **env: str) -> Finished:
    return run("bash", ["-c", script], env=minimal_env(**env), timeout=30)


class TestAccessScope:
    """deploy.sh passes the access scope and what per-user mode verifies against."""

    def test_the_backend_gets_the_scope_and_identity(self) -> None:
        backend = _command("deploy.sh", "helm upgrade --install pointcloud-backend")
        assert '--set-string accessScope="${ACCESS_SCOPE}"' in backend
        assert '--set-string identity.albArn="${ALB_ARN}"' in backend
        assert '${BACKEND_IDENTITY_ARGS[@]+"${BACKEND_IDENTITY_ARGS[@]}"}' in backend

    def test_the_user_pool_is_read_in_public_mode_only(self) -> None:
        """It comes from the Cognito output, which port-forward mode does not have."""
        block = re.search(
            r'if \[ "\$\{ACCESS_MODE\}" = "public" \]; then\n'
            r"  COGNITO=\$\(terraform output -json cognito\)\n(.*?)\nfi\n",
            _deploy_text(),
            re.S,
        )
        assert block
        assert "POOL_ID=\"$(jq -r '.user_pool_id'" in block.group(1)
        assert "APP_CLIENT_ID=\"$(jq -r '.client_id'" in block.group(1)
        assert 'identity.userPoolId="${POOL_ID}"' in block.group(1)
        assert 'identity.clientId="${APP_CLIENT_ID}"' in block.group(1)

    @pytest.mark.parametrize(
        ("pool", "client", "ok"),
        [
            ("us-east-1_AbC123", "1example23456789", True),
            ("null", "1example23456789", False),
            ("us-east-1_AbC123", "null client", False),
            ("", "", False),
        ],
    )
    def test_the_user_pool_ids_are_checked(
        self, pool: str, client: str, ok: bool
    ) -> None:
        """jq prints "null" for a missing key: the chart would take it, the API not."""
        text = _deploy_text()
        check = re.search(r'  if ! \[\[ "\$\{POOL_ID\}" =~ .*?\n  fi\n', text, re.S)
        assert check
        script = f"POOL_ID={pool!r}\nAPP_CLIENT_ID={client!r}\n{check.group(0)}"
        assert (_bash(script).returncode == 0) is ok

    @pytest.mark.parametrize(
        ("scope", "mode", "ok"),
        [
            ("", "public", True),
            ("per-user", "public", True),
            ("shared", "public", True),
            ("", "port-forward", True),
            ("shared", "port-forward", True),
            ("per-user", "port-forward", False),
            ("everyone", "public", False),
        ],
    )
    def test_the_scope_is_checked_before_anything_is_built(
        self, scope: str, mode: str, ok: bool
    ) -> None:
        text = _deploy_text()
        check = re.search(r'ACCESS_SCOPE="\$\{ACCESS_SCOPE:-\}"\n.*?\nfi\n', text, re.S)
        assert check
        out = _bash(check.group(0), ACCESS_SCOPE=scope, ACCESS_MODE=mode)
        assert (out.returncode == 0) is ok, out.stderr
        assert _index("deploy.sh", 'ACCESS_SCOPE="${ACCESS_SCOPE:-}"') < _index(
            "deploy.sh", 'terraform init "${BACKEND_ARGS[@]}"'
        )

    @staticmethod
    def _shared_check() -> str:
        start = (
            'if [ "${ACCESS_SCOPE}" = shared ] && [ "${ACCESS_MODE}" = public ]; then\n'
        )
        check = re.search(re.escape(start) + r".*?\n  fi\nfi\n", _deploy_text(), re.S)
        assert check
        return check.group(0)

    @pytest.mark.parametrize(
        ("answer", "env", "ok"),
        [
            ("", {}, False),
            ("y", {}, False),
            ("yes", {}, True),
            ("", {"AUTO_APPROVE": "1"}, True),
        ],
    )
    def test_shared_access_in_public_mode_is_confirmed(
        self, answer: str, env: dict, ok: bool
    ) -> None:
        """A variable left in a shell must not make every user's jobs visible."""
        script = f"printf '%s\\n' {answer!r} | {{\n{self._shared_check()}}}"
        out = _bash(script, ACCESS_SCOPE="shared", ACCESS_MODE="public", **env)
        assert (out.returncode == 0) is ok, out.stderr
        assert "every other user's jobs" in out.stderr

    def test_per_user_and_port_forward_are_not_asked(self) -> None:
        for scope, mode in (
            ("", "public"),
            ("per-user", "public"),
            ("shared", "port-forward"),
        ):
            out = _bash(self._shared_check(), ACCESS_SCOPE=scope, ACCESS_MODE=mode)
            assert out.returncode == 0, (scope, mode, out.stderr)
            assert out.stderr == ""

    def test_the_backend_is_installed_again_once_the_alb_exists(self) -> None:
        commands = _commands("deploy.sh")
        installs = [i for i, c in enumerate(commands) if c.strip() == "install_backend"]
        assert len(installs) == 2
        frontend = _index("deploy.sh", "helm upgrade --install pointcloud-frontend")
        assert installs[0] < frontend < installs[1]
        text = _deploy_text()
        wait = text[text.index("Waiting for the ALB") : text.index("# Step 7: Verify")]
        assert 'FOUND_ARN="$(alb_arn)"' in wait
        assert '[ "${FOUND_ARN}" != "${ALB_ARN}" ]' in wait
        assert "rollout status deployment/api" in wait

    @staticmethod
    def _alb_arn(host: str, arn: str, aws_status: int = 0) -> Finished:
        text = _deploy_text()
        function = re.search(r"alb_arn\(\) \{\n.*?\n\}\n", text, re.S)
        assert function
        stubs = (
            f"kubectl() {{ printf %s {host!r}; }}\n"
            f'aws() {{ printf "%s\\n" "$*" >&2; printf %s {arn!r}; '
            f"return {aws_status}; }}\n"
            "REGION=us-east-1\n"
        )
        return _bash(stubs + function.group(0) + "alb_arn")

    def test_a_failed_lookup_is_an_error(self) -> None:
        """Not an empty answer, which would roll a working backend to no ARN."""
        out = self._alb_arn("k8s-x-1.us-east-1.elb.example.invalid", "", aws_status=254)
        assert out.returncode != 0
        assert "DescribeLoadBalancers" in out.stderr

    @pytest.mark.parametrize(
        "host", ["x' || DNSName!='", "a b.example", "h.example;id", "h\nexample"]
    )
    def test_only_a_dns_name_reaches_the_query(self, host: str) -> None:
        out = self._alb_arn(host, "arn:aws:elasticloadbalancing:us-east-1:1:x")
        assert out.returncode != 0
        assert "describe-load-balancers" not in out.stderr

    def test_the_alb_is_found_by_its_dns_name(self) -> None:
        arn = (
            "arn:aws:elasticloadbalancing:us-east-1:123456789012:"
            "loadbalancer/app/k8s-pointcloud-0123456789/0123456789abcdef"
        )
        out = self._alb_arn("k8s-x-1.us-east-1.elb.example.invalid", arn)
        assert out.returncode == 0, out.stderr
        assert out.stdout == arn
        assert "DNSName=='k8s-x-1.us-east-1.elb.example.invalid'" in out.stderr

    @pytest.mark.parametrize(("host", "arn"), [("", "unused"), ("h.example", "None")])
    def test_no_alb_yet_is_nothing(self, host: str, arn: str) -> None:
        out = self._alb_arn(host, arn)
        assert out.returncode == 0, out.stderr
        assert out.stdout == ""


# --- T38, T41, T67: add-ons --------------------------------------------------------


@pytest.mark.parametrize(
    ("variant", "gpu", "device"),
    [("gpu", "true", "auto"), ("cpu", "false", "cpu"), ("stub", "false", "cpu")],
)
def test_worker_asks_for_a_gpu_only_in_the_gpu_variant(
    variant: str, gpu: str, device: str
) -> None:
    """The cpu and stub images need no GPU. Without these values the chart's GPU
    defaults applied in every variant: the worker waited for a GPU node, which the
    autoscaler would launch and bill, or never ran with no GPU pool (on a cluster).
    """
    backend = _command("deploy.sh", "helm upgrade --install pointcloud-backend")
    assert '--set worker.gpu.enabled="${WORKER_GPU}"' in backend
    assert '--set-string worker.device="${WORKER_DEVICE}"' in backend
    text = (SCRIPTS / "deploy.sh").read_text()
    case = re.search(
        r'case "\$\{BUILD_VARIANT\}" in\n  gpu\) WORKER_GPU.*?esac', text, re.S
    )
    assert case
    out = run(
        "bash",
        ["-c", f'{case.group(0)}\nprintf "%s %s" "$WORKER_GPU" "$WORKER_DEVICE"'],
        env=minimal_env(BUILD_VARIANT=variant),
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == f"{gpu} {device}"


def test_autoscaled_node_groups_leave_the_count_to_the_autoscaler() -> None:
    """Every node group tagged for the cluster autoscaler ignores desired_size, or an
    apply resets the node count the autoscaler chose. On a cluster, deploy.sh stopped
    at its plan for exactly that; for a GPU pool the reset would remove nodes running
    jobs.
    """
    eks = ROOT / "infrastructure" / "modules" / "eks"
    blocks = [
        block
        for name in ("main.tf", "gpu-nodegroup.tf")
        for block in re.findall(
            r'resource "aws_eks_node_group" "\w+" \{.*?\n\}',
            (eks / name).read_text(),
            re.S,
        )
    ]
    assert len(blocks) == 2
    for block in blocks:
        assert "k8s.io/cluster-autoscaler/enabled" in block
        assert "ignore_changes = [scaling_config[0].desired_size]" in block


class TestAddons:
    """T38, T41, T67: each add-on runs as the identity its IRSA trust names."""

    def test_cluster_autoscaler_service_account_matches_its_trust(self) -> None:
        namespace, account = _trust_subjects()["ca"]
        ca = _command("deploy.sh", "helm upgrade --install cluster-autoscaler")
        assert f"-n {namespace}" in ca
        assert "--set rbac.serviceAccount.create=true" in ca
        assert f"--set rbac.serviceAccount.name={account}" in ca
        assert (
            '--set rbac.serviceAccount.annotations."eks\\.amazonaws\\.com/role-arn"'
            '="${CA_ROLE}"'
        ) in ca
        assert (
            "CA_ROLE=$(terraform output -json addon_role_arns"
            " | jq -r '.cluster_autoscaler')"
        ) in _commands("deploy.sh")

    def test_fluent_bit_service_account_matches_its_trust(self) -> None:
        namespace, account = _trust_subjects()["fb"]
        fb = _command("deploy.sh", "helm upgrade --install aws-for-fluent-bit")
        assert f"-n {namespace}" in fb
        assert f"--set serviceAccount.name={account}" in fb
        assert '"eks\\.amazonaws\\.com/role-arn"="${FLUENT_BIT_ROLE}"' in fb

    def test_load_balancer_controller_service_account_matches_its_trust(
        self,
    ) -> None:
        namespace, account = _trust_subjects()["lbc"]
        lbc = _command("deploy.sh", "helm upgrade --install aws-load-balancer-")
        assert f"-n {namespace}" in lbc
        assert f"--set serviceAccount.name={account}" in lbc
        assert '"eks\\.amazonaws\\.com/role-arn"="${LBC_ROLE}"' in lbc

    def test_load_balancer_controller_is_ready_before_a_later_service(self) -> None:
        """The controller registers a Service webhook that fails closed, so until its
        pods are ready no Service in the cluster can be created. deploy.sh installed
        the next chart at once, and the Cluster Autoscaler's Service was refused: found
        on a public-mode deploy to a sandbox cluster.
        """
        lbc = _command("deploy.sh", "helm upgrade --install aws-load-balancer-")
        assert "--wait --timeout" in lbc
        lbc_at = _index("deploy.sh", "helm upgrade --install aws-load-balancer-")
        assert lbc_at < _index("deploy.sh", "helm upgrade --install cluster-autoscaler")

    def test_fluent_bit_runs_on_gpu_nodes(self) -> None:
        fb = _command("deploy.sh", "helm upgrade --install aws-for-fluent-bit")
        assert "--set 'tolerations[0].operator=Exists'" in fb

    def test_keda_rbac_is_namespaced_and_keeps_irsa(self) -> None:
        keda = _command("deploy.sh", "helm upgrade --install keda")
        assert "--set permissions.operator.restrict.secret=true" in keda
        assert "--set watchNamespace=pointcloud-detection" in keda
        assert "--set podIdentity.aws.irsa.enabled=true" in keda
        assert '--set podIdentity.aws.irsa.roleArn="${KEDA_ROLE}"' in keda

    def test_credentials_are_checked_for_every_irsa_addon(self) -> None:
        calls = [c for c in _commands("deploy.sh") if "check_addon_credentials " in c]
        joined = "\n".join(calls)
        for workload in (
            "deployment/cluster-autoscaler-aws-cluster-autoscaler",
            "deployment/aws-load-balancer-controller",
            "daemonset/aws-for-fluent-bit",
            "deployment/keda-operator",
        ):
            assert workload in joined
        errors = _command("deploy.sh", "CREDENTIAL_ERRORS=")
        for error in ("AccessDenied", "NoCredentialProviders", "WebIdentityErr"):
            assert error in errors
        assert "IMDS hop limit" in (SCRIPTS / "deploy.sh").read_text()

    def test_deploy_checks_cluster_access_before_installing(self) -> None:
        check = _index("deploy.sh", "kubectl auth can-i '*' '*' --all-namespaces")
        assert _index("deploy.sh", "write_kubeconfig ") < check
        assert check < _index("deploy.sh", "kubectl create namespace")

    def test_deploy_never_rewrites_the_operators_kubeconfig(self) -> None:
        # Without --kubeconfig, update-kubeconfig rewrites the first KUBECONFIG file,
        # which for a relay user is the relay kubeconfig.
        commands = _commands("deploy.sh")
        assert 'export KUBECONFIG="${KUBE_DIR}/kubeconfig"' in commands
        assert _index("deploy.sh", "export KUBECONFIG=") < _index(
            "deploy.sh", "write_kubeconfig "
        )
        # Only the closing message mentions update-kubeconfig, for the operator.
        done = _index("deploy.sh", 'echo "Deployment complete!"')
        assert not [c for c in commands[:done] if "update-kubeconfig" in c]
        assert "trap 'rm -rf \"${KUBE_DIR}\"' EXIT" in [c.strip() for c in commands]
        lib = (SCRIPTS / "lib" / "kubeconfig.sh").read_text()
        assert '--kubeconfig "${file}"' in lib


# --- T48, T35: namespace and frontend ----------------------------------------------


class TestNamespaceAndFrontend:
    """T48: Pod Security Admission labels. T35: the frontend's CSP inputs."""

    def test_namespace_enforces_restricted(self) -> None:
        label = _command("deploy.sh", "kubectl label namespace pointcloud-detection")
        for mode in ("enforce", "warn", "audit"):
            assert f"pod-security.kubernetes.io/{mode}=restricted" in label
        assert "--overwrite" in label
        at = _index("deploy.sh", "kubectl label namespace pointcloud-detection")
        assert _index("deploy.sh", "kubectl create namespace") < at
        assert at < _index("deploy.sh", "helm upgrade --install pointcloud-backend")

    def test_frontend_gets_region_and_input_bucket(self) -> None:
        frontend = _command("deploy.sh", "helm upgrade --install pointcloud-frontend")
        assert '--set aws.region="${REGION}"' in frontend
        assert '--set aws.s3.inputBucket="${S3_INPUT}"' in frontend


# --- T62: reviewed applies ---------------------------------------------------------


class TestReviewedApply:
    """T62: Terraform applies only a saved plan an operator has confirmed."""

    @pytest.mark.parametrize("script", sorted(p.name for p in SCRIPTS.glob("*.sh")))
    def test_nothing_is_auto_approved(self, script: str) -> None:
        for command in _commands(script):
            assert "-auto-approve" not in command, command
        assert not [c for c in _commands(script) if "terraform destroy" in c]

    @pytest.mark.parametrize(
        ("script", "plan_dir"), [("deploy.sh", "PLAN_DIR"), ("destroy.sh", "WORK_DIR")]
    )
    def test_apply_is_of_a_private_saved_plan(self, script: str, plan_dir: str) -> None:
        commands = [c.strip() for c in _commands(script)]
        assert f'{plan_dir}="$(mktemp -d)"' in commands
        assert f'chmod 0700 "${{{plan_dir}}}"' in commands
        assert f"trap 'rm -rf \"${{{plan_dir}}}\"' EXIT" in commands
        # Exactly the saved plan; destroy.sh keeps the exit code to explain a failure.
        applies = [
            c.removesuffix(" || APPLY_RC=$?")
            for c in commands
            if c.startswith("terraform apply")
        ]
        assert applies == [f'terraform apply "${{{plan_dir}}}/tfplan"']
        plan = _command(script, "terraform plan")
        assert f'-out="${{{plan_dir}}}/tfplan"' in plan

    @pytest.mark.parametrize("script", ["deploy.sh", "destroy.sh"])
    def test_only_auto_approve_1_skips_the_plan_question(self, script: str) -> None:
        tests = [
            c.strip()
            for c in _commands(script)
            if "AUTO_APPROVE" in c and c.strip().startswith(("if ", "elif ", "["))
        ]
        # Every question it skips (deploy.sh also asks before shared access in public
        # mode) is skipped by exactly AUTO_APPROVE=1, and nothing else.
        assert tests
        assert set(tests) == {'if [ "${AUTO_APPROVE:-}" = "1" ]; then'}


# --- T37, T62: destroy.sh ----------------------------------------------------------


def _destroy(
    tmp_path: Path, stdin: str = "yes\nyes\n", state: str | None = None, **env: str
) -> tuple[Finished, list[str]]:
    state_file = tmp_path / "state.txt"
    state_file.write_text(
        state
        if state is not None
        else "\n".join(
            [
                BPA_OPT_IN,
                BPA_LEGACY,
                "module.s3.data.aws_s3_account_public_access_block.current",
                'module.s3.aws_s3_bucket_public_access_block.data["input"]',
                "module.eks.aws_eks_cluster.main",
            ]
        )
        + "\n"
    )
    defaults = {
        "TF_STATE_BUCKET": "state-bucket",
        "STUB_STATE_FILE": str(state_file),
        "STUB_OUT_access_mode": "public",
        "STUB_OUT_app_host": "app.example.com",
        "STUB_OUT_cluster_name": "demo",
        "STUB_OUT_cluster_region": "us-east-1",
    }
    return _run(tmp_path, "destroy.sh", stdin=stdin, **{**defaults, **env})


class TestStateEncryption:
    """T62, A7: Terraform writes its state with the state bucket's own KMS key."""

    @staticmethod
    def _init(calls: list[str]) -> str:
        (init,) = [c for c in calls if c.startswith("terraform init")]
        return init

    @pytest.mark.parametrize("script", ["deploy.sh", "destroy.sh"])
    def test_both_scripts_init_through_the_helper(self, script: str) -> None:
        commands = [c.strip() for c in _commands(script)]
        assert '. "${SCRIPT_DIR}/lib/state-backend.sh"' in commands
        assert 'terraform init "${BACKEND_ARGS[@]}"' in commands
        assert not [c for c in commands if "-backend-config=" in c]

    def test_aws_managed_key_is_passed_as_its_arn(self, tmp_path: Path) -> None:
        proc, calls = _destroy(tmp_path, STUB_STATE_SSE="aws:kms\tNone")
        init = self._init(calls)
        assert (
            "-backend-config=kms_key_id=arn:aws:kms:us-east-1:123456789012:key/aws-s3-managed"
            in init
        )
        assert "-backend-config=bucket=state-bucket" in init
        assert "SSE-S3" not in proc.stderr
        (lookup,) = [
            c for c in calls if c.startswith("aws s3api get-bucket-encryption")
        ]
        assert "--expected-bucket-owner 123456789012" in lookup

    def test_customer_managed_key_is_passed_through(self, tmp_path: Path) -> None:
        key = "arn:aws:kms:us-east-1:123456789012:key/cmk"
        _, calls = _destroy(tmp_path, STUB_STATE_SSE=f"aws:kms\t{key}")
        assert f"-backend-config=kms_key_id={key}" in self._init(calls)

    def test_a_bucket_without_sse_kms_is_warned_about(self, tmp_path: Path) -> None:
        proc, calls = _destroy(tmp_path, STUB_STATE_SSE="AES256\tNone")
        assert "kms_key_id" not in self._init(calls)
        assert "does not default to SSE-KMS" in proc.stderr

    @pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")
    @pytest.mark.parametrize(
        ("bucket", "reconfigure"), [("state-bucket", True), ("other-bucket", False)]
    )
    def test_reconfigure_only_when_the_state_does_not_move(
        self, tmp_path: Path, bucket: str, reconfigure: bool
    ) -> None:
        saved = (
            tmp_path / "repo" / "infrastructure" / ".terraform" / "terraform.tfstate"
        )
        _tree(tmp_path)
        saved.parent.mkdir()
        config = {
            "bucket": bucket,
            "key": "pointcloud-detection/dev/terraform.tfstate",
            "region": "us-east-1",
        }
        saved.write_text(json.dumps({"backend": {"type": "s3", "config": config}}))
        _, calls = _destroy(tmp_path, STUB_STATE_SSE="aws:kms\tNone")
        assert ("-reconfigure" in self._init(calls).split()) is reconfigure


class TestDestroy:
    """T37: the account's Block Public Access survives. T62: destroy is reviewed."""

    def test_account_bpa_leaves_state_before_the_destroy_is_planned(
        self, tmp_path: Path
    ) -> None:
        proc, calls = _destroy(tmp_path)
        assert proc.returncode == 0, proc.stderr
        removed = [c for c in calls if c.startswith("terraform state rm")]
        assert removed == [
            f"terraform state rm {BPA_OPT_IN}",
            f"terraform state rm {BPA_LEGACY}",
        ]
        plan = next(i for i, c in enumerate(calls) if c.startswith("terraform plan"))
        assert "-destroy" in calls[plan]
        assert max(calls.index(c) for c in removed) < plan
        assert "Block Public Access" in proc.stdout

    def test_applies_the_saved_plan_and_removes_it(self, tmp_path: Path) -> None:
        proc, calls = _destroy(tmp_path)
        assert proc.returncode == 0, proc.stderr
        applies = [c for c in calls if c.startswith("terraform apply")]
        assert len(applies) == 1
        assert "applied saved plan" in calls
        plan_file = Path(applies[0].split()[-1])
        assert plan_file.name == "tfplan"
        assert not plan_file.parent.exists()
        assert not _plan_dir(calls).exists()

    def test_kubernetes_cleanup_uses_its_own_kubeconfig(self, tmp_path: Path) -> None:
        proc, calls = _destroy(tmp_path)
        assert proc.returncode == 0, proc.stderr
        configs = {c for c in calls if c.startswith("KUBECONFIG=")}
        assert len(configs) == 1
        (config,) = configs
        assert config == f"KUBECONFIG={_plan_dir(calls) / 'kubeconfig'}"
        helm = next(i for i, c in enumerate(calls) if c.startswith("helm uninstall"))
        assert helm > next(
            i for i, c in enumerate(calls) if c.startswith("terraform plan")
        )

    def test_declining_the_plan_removes_nothing(self, tmp_path: Path) -> None:
        proc, calls = _destroy(tmp_path, stdin="yes\nno\n")
        assert proc.returncode == 1
        assert not [c for c in calls if c.startswith(("terraform apply", "helm"))]
        assert not [c for c in calls if c.startswith("kubectl delete")]
        assert not _plan_dir(calls).exists()

    def test_auto_approve_skips_only_the_plan_question(self, tmp_path: Path) -> None:
        proc, calls = _destroy(tmp_path, stdin="yes\n", AUTO_APPROVE="1")
        assert proc.returncode == 0, proc.stderr
        assert [c for c in calls if c.startswith("terraform apply")]

        proc, calls = _destroy(tmp_path, stdin="", AUTO_APPROVE="1")
        assert "Aborted." in proc.stdout
        assert not [c for c in calls if c.startswith("terraform")]

    def test_state_listing_failure_is_fatal(self, tmp_path: Path) -> None:
        proc, calls = _destroy(
            tmp_path, STUB_STATE_ERROR="Error: error acquiring the state lock"
        )
        assert proc.returncode != 0
        assert not [c for c in calls if c.startswith("terraform plan")]

    def test_no_state_is_not_a_failure(self, tmp_path: Path) -> None:
        proc, calls = _destroy(
            tmp_path,
            STUB_STATE_ERROR="No state file was found!",
            STUB_PLAN_RC="0",
            STUB_OUT_cluster_name="",
        )
        assert proc.returncode == 0, proc.stderr
        assert not [c for c in calls if c.startswith(("terraform apply", "helm"))]

    def test_deleted_cluster_skips_kubernetes_cleanup(self, tmp_path: Path) -> None:
        proc, calls = _destroy(tmp_path, STUB_CLUSTER="gone")
        assert proc.returncode == 0, proc.stderr
        assert not [c for c in calls if c.startswith(("helm", "kubectl"))]
        assert [c for c in calls if c.startswith("terraform apply")]

    def test_the_relay_is_used_when_its_port_is_given(self, tmp_path: Path) -> None:
        # The root defaults leave only the private endpoint, reached from an
        # operator's machine through the SSM relay.
        proc, calls = _destroy(tmp_path, PCD_KUBE_RELAY_PORT="8443")
        assert proc.returncode == 0, proc.stderr
        kubeconfig = _plan_dir(calls) / "kubeconfig"
        set_cluster = [c for c in calls if "config set-cluster" in c]
        assert set_cluster == [
            f"kubectl --kubeconfig {kubeconfig} config set-cluster "
            "arn:aws:eks:us-east-1:123456789012:cluster/demo "
            "--server https://127.0.0.1:8443 "
            "--tls-server-name ABC123.gr7.us-east-1.eks.amazonaws.com"
        ]
        setup = calls.index(set_cluster[0])
        assert setup < next(i for i, c in enumerate(calls) if "auth can-i" in c)
        assert [c for c in calls if c.startswith("terraform apply")]

    @pytest.mark.parametrize("port", ["0", "65536", "84x3", "-1"])
    def test_a_relay_port_that_is_not_one_is_refused(
        self, tmp_path: Path, port: str
    ) -> None:
        proc, calls = _destroy(tmp_path, PCD_KUBE_RELAY_PORT=port)
        assert proc.returncode != 0
        assert "is not a port number" in proc.stderr
        assert not [c for c in calls if c.startswith(("helm", "terraform plan"))]

    def test_without_the_relay_no_server_is_rewritten(self, tmp_path: Path) -> None:
        proc, calls = _destroy(tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert not [c for c in calls if "config set-cluster" in c]

    def test_an_unreachable_cluster_points_at_the_relay(self, tmp_path: Path) -> None:
        proc, _ = _destroy(tmp_path, STUB_CAN_I_RC="1")
        assert "PCD_KUBE_RELAY_PORT" in proc.stderr
        assert "SKIP_KUBE_CLEANUP=1" in proc.stderr

    def test_skip_kube_cleanup_destroys_without_kubernetes(
        self, tmp_path: Path
    ) -> None:
        proc, calls = _destroy(tmp_path, SKIP_KUBE_CLEANUP="1", STUB_CAN_I_RC="1")
        assert proc.returncode == 0, proc.stderr
        assert "ALB" in proc.stderr
        assert not [c for c in calls if c.startswith(("helm", "kubectl"))]
        assert not [c for c in calls if "update-kubeconfig" in c]
        assert [c for c in calls if c.startswith("terraform apply")]

    def test_no_cluster_admin_stops_before_anything_is_removed(
        self, tmp_path: Path
    ) -> None:
        proc, calls = _destroy(tmp_path, STUB_CAN_I_RC="1")
        assert proc.returncode != 0
        assert "cannot administer cluster demo" in proc.stderr
        assert not [
            c for c in calls if c.startswith(("helm", "terraform plan", "terraform ap"))
        ]


BUCKET_STATE = (
    "\n".join(
        [
            'module.s3.aws_s3_bucket.buckets["input"]',
            'module.s3.aws_s3_bucket_versioning.buckets["input"]',
            "module.s3.aws_s3_bucket.logs",
            "module.s3.data.aws_s3_bucket.existing",
            "module.eks.aws_eks_cluster.main",
        ]
    )
    + "\n"
)


class TestDestroyLeavesNothingBehind:
    """The data and access-log buckets are versioned and not force-destroyed, and S3
    deletes no bucket that holds a version, so the destroy of a used stack stopped on
    BucketNotEmpty: found on a sandbox cluster. destroy.sh now empties them once the
    plan is confirmed, and lists the log groups the destroy cannot remove.
    """

    @staticmethod
    def _calls(calls: list[str], prefix: str) -> list[str]:
        return [c for c in calls if c.startswith(prefix)]

    def test_buckets_are_emptied_after_the_cleanup_and_before_the_destroy(
        self, tmp_path: Path
    ) -> None:
        proc, calls = _destroy(tmp_path, state=BUCKET_STATE)
        assert proc.returncode == 0, proc.stderr
        assert self._calls(calls, "terraform state show") == [
            'terraform state show -no-color module.s3.aws_s3_bucket.buckets["input"]',
            "terraform state show -no-color module.s3.aws_s3_bucket.logs",
        ]
        lists = self._calls(calls, "aws s3api list-object-versions")
        deletes = self._calls(calls, "aws s3api delete-objects")
        # One page each, deleted, then listed again and found empty.
        assert [c.split()[4] for c in deletes] == ["demo-input", "demo-logs"]
        assert [c.split()[4] for c in lists] == ["demo-input"] * 2 + ["demo-logs"] * 2
        for call in lists + deletes:
            assert "--expected-bucket-owner 123456789012" in call
        for call in deletes:
            assert f"--delete file://{_plan_dir(calls)}/delete.json" in call
        kube = max(i for i, c in enumerate(calls) if c.startswith(("helm", "kubectl")))
        assert kube < calls.index(lists[0])
        assert calls.index(deletes[-1]) < calls.index("applied saved plan")
        listed = proc.stdout.index("demo-input")
        assert listed < proc.stdout.index("Emptying the buckets")
        assert "earlier versions included" in proc.stdout

    def test_declining_the_plan_empties_no_bucket(self, tmp_path: Path) -> None:
        proc, calls = _destroy(tmp_path, stdin="yes\nno\n", state=BUCKET_STATE)
        assert proc.returncode == 1
        assert not self._calls(calls, "aws s3api list-object-versions")
        assert not self._calls(calls, "aws s3api delete-objects")
        assert "applied saved plan" not in calls

    def test_a_refused_delete_stops_before_the_destroy(self, tmp_path: Path) -> None:
        proc, calls = _destroy(tmp_path, state=BUCKET_STATE, STUB_DELETE_ERROR="1")
        assert proc.returncode == 1
        assert "S3 refused to delete an object in demo-input" in proc.stderr
        assert "the Terraform destroy has not started" in proc.stderr
        assert "applied saved plan" not in calls

    def test_a_bucket_already_gone_is_skipped(self, tmp_path: Path) -> None:
        proc, calls = _destroy(
            tmp_path, state=BUCKET_STATE, STUB_GONE_BUCKETS="demo-input"
        )
        assert proc.returncode == 0, proc.stderr
        assert "demo-input: already gone" in proc.stdout
        deletes = self._calls(calls, "aws s3api delete-objects")
        assert [c.split()[4] for c in deletes] == ["demo-logs"]
        assert "applied saved plan" in calls

    def test_a_failed_destroy_says_to_run_it_again(self, tmp_path: Path) -> None:
        proc, _ = _destroy(tmp_path, state=BUCKET_STATE, STUB_APPLY_RC="1")
        assert proc.returncode == 1
        assert "Run destroy.sh again" in proc.stderr
        assert "Teardown complete" not in proc.stdout

    def test_log_groups_left_behind_are_listed_and_not_deleted(
        self, tmp_path: Path
    ) -> None:
        groups = [
            "/aws/eks/demo/cluster",
            "/aws/eks/demo/workloads",
            "/vpc/demo/flow-logs",
        ]
        proc, calls = _destroy(
            tmp_path, STUB_LOG_GROUPS=" ".join([*groups, "/aws/eks/other/cluster"])
        )
        assert proc.returncode == 0, proc.stderr
        for group in groups:
            assert (
                f"aws logs delete-log-group --region us-east-1 --log-group-name {group}"
                in proc.stdout
            )
        assert "/aws/eks/other/" not in proc.stdout
        assert not self._calls(calls, "aws logs delete-log-group")
        describes = self._calls(calls, "aws logs describe-log-groups")
        assert len(describes) == 2
        assert calls.index("applied saved plan") < calls.index(describes[0])

    def test_nothing_left_prints_nothing(self, tmp_path: Path) -> None:
        proc, _ = _destroy(tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert "Left in CloudWatch Logs" not in proc.stdout

    def test_the_flow_log_group_is_named_after_the_cluster(self) -> None:
        """destroy.sh looks for the flow-log group under /vpc/<cluster name>/."""
        infra = ROOT / "infrastructure"
        flow = (infra / "modules" / "vpc" / "flow-logs.tf").read_text()
        root = (infra / "main.tf").read_text()
        both = r"\$\{var\.project_name\}-\$\{var\.environment\}"
        assert re.search(rf'name\s*=\s*"/vpc/{both}/flow-logs"', flow)
        assert re.search(rf'cluster_name\s*=\s*"{both}"', root)
        vpc = re.search(r'module "vpc" \{(.*?)\n\}', root, re.S)
        assert vpc
        assert re.search(r"project_name\s*=\s*var\.project_name", vpc.group(1))
        assert re.search(r"environment\s*=\s*var\.environment", vpc.group(1))


# --- plan-adoption.sh and generate_imports.py --------------------------------------

# A marker planted in the fake plan where the Cognito client secret would be, so the
# tests can show it never reaches a file plan-adoption.sh writes. Not a credential.
PLANTED_VALUE = "cognito-client-secret-value"


def _plan_json(tmp_path: Path) -> Path:
    """A small `terraform show -json` document, with a sensitive value in it."""
    plan = tmp_path / "show.json"
    plan.write_text(
        json.dumps(
            {
                "format_version": "1.2",
                "resource_changes": [
                    {
                        "address": BPA_OPT_IN,
                        "type": "aws_s3_account_public_access_block",
                        "change": {"actions": ["create"]},
                    },
                    {
                        "address": "module.eks.aws_eks_access_entry.deployer",
                        "type": "aws_eks_access_entry",
                        "change": {"actions": ["create"]},
                    },
                    {
                        "address": "module.eks.aws_eks_access_policy_association.admin",
                        "type": "aws_eks_access_policy_association",
                        "change": {"actions": ["create"]},
                    },
                    {
                        "address": "module.cognito.aws_cognito_user_pool_client.app",
                        "type": "aws_cognito_user_pool_client",
                        "change": {
                            "actions": ["no-op"],
                            "after": {"client_secret": PLANTED_VALUE},
                        },
                    },
                ],
            }
        )
    )
    return plan


def _generate(plan: dict, out: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(plan)))
    monkeypatch.setattr(sys, "argv", ["generate_imports.py", "-", str(out)])
    return gi.main()


class TestGenerateImports:
    """generate_imports.py reads the plan from stdin and writes addresses only."""

    def test_reads_the_plan_from_stdin(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        plan = json.loads(_plan_json(tmp_path).read_text())
        out = tmp_path / "imports.tf"
        assert _generate(plan, out, monkeypatch) == 0
        text = out.read_text()
        for addr in (
            BPA_OPT_IN,
            "module.eks.aws_eks_access_entry.deployer",
            "module.eks.aws_eks_access_policy_association.admin",
        ):
            assert f"  to = {addr}\n" in text
        assert "module.cognito" not in text
        assert PLANTED_VALUE not in text
        assert PLANTED_VALUE not in capsys.readouterr().out
        assert "UNKNOWN" not in text

    def test_maps_the_account_bpa_and_eks_access(self) -> None:
        assert "aws_s3_account_public_access_block" in gi.ID_FORMATS
        assert gi.ID_FORMATS["aws_eks_access_entry"][0].startswith(
            "cluster-name:principal-arn"
        )
        assert gi.ID_FORMATS["aws_eks_access_policy_association"][0] == (
            "cluster-name#principal-arn#policy-arn"
        )
        assert "destroy.sh" in " ".join(gi.NOTES["aws_s3_account_public_access_block"])

    def test_warns_when_the_plan_deletes_the_account_bpa(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        plan = {
            "resource_changes": [
                {
                    "address": BPA_OPT_IN,
                    "type": "aws_s3_account_public_access_block",
                    "change": {"actions": ["delete"]},
                }
            ]
        }
        assert _generate(plan, tmp_path / "imports.tf", monkeypatch) == 0
        out = capsys.readouterr().out
        assert f"WARNING: the plan deletes {BPA_OPT_IN}" in out
        assert f"terraform state rm '{BPA_OPT_IN}'" in out

    def test_still_reads_a_plan_file(self, tmp_path: Path) -> None:
        plan = _plan_json(tmp_path)
        assert gi.load_plan(str(plan)) == json.loads(plan.read_text())


class TestPlanAdoption:
    """Owner request: no plaintext JSON copy of the plan, and so of the secret."""

    def test_never_writes_plan_json(self) -> None:
        commands = "\n".join(_commands("plan-adoption.sh"))
        assert "plan.json" not in commands.replace(
            'for stale in "${OUT_DIR}/plan.json"', ""
        )
        show = _command("plan-adoption.sh", "terraform show -json")
        assert show.split("|")[1].strip().startswith("python3 ")
        assert show.rstrip().endswith('- "${OUT_DIR}/imports.generated.tf"')
        assert ">" not in show

    def test_streams_the_plan_and_removes_it(self, tmp_path: Path) -> None:
        infra = _tree(tmp_path)
        adoption = infra / "adoption"
        adoption.mkdir()
        (adoption / "plan.json").write_text(PLANTED_VALUE)
        proc, calls = _run(
            tmp_path, "plan-adoption.sh", STUB_PLAN_JSON=str(_plan_json(tmp_path))
        )
        assert proc.returncode == 0, proc.stderr
        assert sorted(p.name for p in adoption.iterdir()) == [
            "imports.generated.tf",
            "plan.txt",
        ]
        assert "(sensitive value)" in (adoption / "plan.txt").read_text()
        for path in adoption.iterdir():
            assert PLANTED_VALUE not in path.read_text(), path
        assert f"to = {BPA_OPT_IN}" in (adoption / "imports.generated.tf").read_text()
        plan_dir = _plan_dir(calls)
        assert plan_dir != adoption
        assert not plan_dir.exists()
        assert not [c for c in calls if c.startswith(("terraform ap", "terraform im"))]


# --- T62: bootstrap-state.sh -------------------------------------------------------


needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")


@needs_jq
class TestBootstrapState:
    """T62: the state bucket is encrypted and private; its policy is never replaced."""

    def test_new_bucket_is_kms_encrypted_and_private(self, tmp_path: Path) -> None:
        proc, calls = _run(tmp_path, "bootstrap-state.sh", "state-bucket", "eu-west-1")
        assert proc.returncode == 0, proc.stderr
        (encryption,) = [c for c in calls if "put-bucket-encryption" in c]
        assert '"SSEAlgorithm":"aws:kms"' in encryption
        assert "KMSMasterKeyID" not in encryption
        (block,) = [c for c in calls if "put-public-access-block" in c]
        for setting in (
            "BlockPublicAcls",
            "IgnorePublicAcls",
            "BlockPublicPolicy",
            "RestrictPublicBuckets",
        ):
            assert f"{setting}=true" in block
        assert [c for c in calls if "put-bucket-policy" in c]
        assert not [c for c in calls if "put-bucket-logging" in c]
        for call in calls:
            if call.startswith("aws s3api") and "create-bucket" not in call:
                assert "--expected-bucket-owner 123456789012" in call, call

    def test_account_regional_name_is_created_in_that_namespace(
        self, tmp_path: Path
    ) -> None:
        name = "state-bucket-123456789012-eu-west-1-an"
        proc, calls = _run(tmp_path, "bootstrap-state.sh", name, "eu-west-1")
        assert proc.returncode == 0, proc.stderr
        (create,) = [c for c in calls if "create-bucket" in c]
        assert "--bucket-namespace account-regional" in create
        assert "regional namespace" not in proc.stderr

    def test_global_name_is_created_there_with_a_note(self, tmp_path: Path) -> None:
        proc, calls = _run(tmp_path, "bootstrap-state.sh", "state-bucket", "us-east-1")
        assert proc.returncode == 0, proc.stderr
        (create,) = [c for c in calls if "create-bucket" in c]
        assert "--bucket-namespace" not in create
        assert "-123456789012-us-east-1-an would be" in proc.stderr

    def test_customer_managed_key_and_logging(self, tmp_path: Path) -> None:
        key = "arn:aws:kms:us-east-1:123456789012:key/example"
        proc, calls = _run(
            tmp_path,
            "bootstrap-state.sh",
            "state-bucket",
            "us-east-1",
            key,
            STATE_ACCESS_LOG_BUCKET="log-bucket",
        )
        assert proc.returncode == 0, proc.stderr
        (encryption,) = [c for c in calls if "put-bucket-encryption" in c]
        assert f'"KMSMasterKeyID":"{key}"' in encryption
        (logging,) = [c for c in calls if "put-bucket-logging" in c]
        assert '"TargetBucket":"log-bucket"' in logging

    def test_existing_bucket_policy_is_refused(self, tmp_path: Path) -> None:
        policy = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow"}]}'
        proc, calls = _run(
            tmp_path,
            "bootstrap-state.sh",
            "state-bucket",
            STUB_BUCKET_EXISTS="1",
            STUB_BUCKET_POLICY=policy,
        )
        assert proc.returncode != 0
        assert "Refusing to replace it" in proc.stderr
        assert not [c for c in calls if " put-" in c or "create-bucket" in c]

    def test_rerun_with_its_own_policy_changes_no_policy(self, tmp_path: Path) -> None:
        own = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "DenyInsecureTransport",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "s3:*",
                    "Resource": [
                        "arn:aws:s3:::state-bucket",
                        "arn:aws:s3:::state-bucket/*",
                    ],
                    "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                }
            ],
        }
        proc, calls = _run(
            tmp_path,
            "bootstrap-state.sh",
            "state-bucket",
            STUB_BUCKET_EXISTS="1",
            STUB_BUCKET_POLICY=json.dumps(own),
        )
        assert proc.returncode == 0, proc.stderr
        assert not [c for c in calls if "put-bucket-policy" in c or "create-bu" in c]
        assert [c for c in calls if "put-public-access-block" in c]
