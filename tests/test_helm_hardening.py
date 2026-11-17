"""Render tests for the pod and image hardening in the Helm charts.

test_helm_render.py pins the access controls. This file pins what limits a foothold
in a pod, and what decides which build runs:

- images are named by an immutable tag, never latest (T29);
- every pod meets the Pod Security Standard "restricted" profile, which the namespace
  enforces, runs with a read-only root filesystem and mounts no Kubernetes API token
  (T48);
- the worker's native parsers run in a container of their own, with no credential
  (T11, the pod side);
- the SPA is served with a Content-Security-Policy (T35), and public mode with HSTS on
  every response and uvicorn trusting forwarded headers from the ALB only (T57, T45);

They share test_helm_render.py's helpers, and like it they skip when helm is not
installed unless REQUIRE_HELM=1.
"""

import pytest

from tests.test_helm_render import (
    BACKEND,
    CHARTS,
    PORT_FORWARD,
    PUBLIC,
    TRAINING,
    A,
    nginx_conf,
    of_kind,
    render,
    render_error,
)

# Every chart in both access modes, with the values deploy.sh (or, for training, an
# operator) passes.
RENDERS = {
    "backend-public": ("backend", BACKEND),
    "backend-port-forward": (
        "backend",
        {
            **{k: v for k, v in BACKEND.items() if k != "networkPolicy.albSourceCidrs"},
            "accessMode": "port-forward",
        },
    ),
    "frontend-public": ("frontend", PUBLIC),
    "frontend-port-forward": ("frontend", PORT_FORWARD),
    "training": ("training", TRAINING),
}


def pod_templates(docs: list[dict]) -> dict[str, dict]:
    """Every pod spec a chart renders, keyed by workload name."""
    pods = {}
    for d in docs:
        if d["kind"] in ("Deployment", "Job"):
            pods[d["metadata"]["name"]] = d["spec"]["template"]
    return pods


def containers(pod: dict) -> list[dict]:
    spec = pod["spec"]
    return [*spec.get("initContainers", []), *spec["containers"]]


@pytest.fixture(scope="module", params=sorted(RENDERS))
def rendered(request: pytest.FixtureRequest) -> list[dict]:
    chart, values = RENDERS[request.param]
    return render(chart, values)


class TestImages:
    """T29: what runs is the build deploy.sh named, on every node."""

    def test_tag_is_the_one_passed(self, rendered: list[dict]) -> None:
        for pod in pod_templates(rendered).values():
            for c in containers(pod):
                assert c["image"].rsplit(":", 1)[1] in ("0123abc", "0123abc-gpu")
                assert c["imagePullPolicy"] == "IfNotPresent", c["name"]

    @pytest.mark.parametrize("name", sorted(RENDERS))
    def test_tag_is_required(self, name: str) -> None:
        chart, values = RENDERS[name]
        err = render_error(chart, {**values, "image.tag": ""})
        assert "image.tag is required" in err

    @pytest.mark.parametrize("name", sorted(RENDERS))
    def test_repository_is_required(self, name: str) -> None:
        chart, values = RENDERS[name]
        err = render_error(chart, {**values, "image.repository": ""})
        assert "image.repository is required" in err

    @pytest.mark.parametrize("name", sorted(RENDERS))
    def test_latest_is_refused(self, name: str) -> None:
        chart, values = RENDERS[name]
        err = render_error(chart, {**values, "image.tag": "latest"})
        assert "image.tag=latest is refused" in err

    def test_values_carry_no_tag(self) -> None:
        # No chart may ship a default tag: any default is a mutable one.
        for chart in ("backend", "frontend", "training"):
            text = (CHARTS / chart / "values.yaml").read_text()
            assert '  tag: ""' in text, chart
            assert "tag: latest" not in text, chart


# Pod Security Standards, "restricted" profile (which includes "baseline"), as the
# namespace's pod-security.kubernetes.io/enforce=restricted label applies it:
# https://kubernetes.io/docs/concepts/security/pod-security-standards/
RESTRICTED_VOLUME_TYPES = {
    "configMap",
    "csi",
    "downwardAPI",
    "emptyDir",
    "ephemeral",
    "persistentVolumeClaim",
    "projected",
    "secret",
}
SAFE_SYSCTLS = {
    "kernel.shm_rmid_forced",
    "net.ipv4.ip_local_port_range",
    "net.ipv4.ip_unprivileged_port_start",
    "net.ipv4.tcp_syncookies",
    "net.ipv4.ping_group_range",
    "net.ipv4.ip_local_reserved_ports",
    "net.ipv4.tcp_keepalive_time",
    "net.ipv4.tcp_fin_timeout",
    "net.ipv4.tcp_keepalive_intvl",
    "net.ipv4.tcp_keepalive_probes",
}


def restricted_violations(pod: dict) -> list[str]:
    """What in a pod template the "restricted" profile would refuse."""
    spec = pod["spec"]
    psc = spec.get("securityContext") or {}
    out = []
    for field in ("hostNetwork", "hostPID", "hostIPC"):
        if spec.get(field):
            out.append(field)
    if (psc.get("windowsOptions") or {}).get("hostProcess"):
        out.append("hostProcess")
    for v in spec.get("volumes") or []:
        kinds = set(v) - {"name"}
        if not kinds <= RESTRICTED_VOLUME_TYPES:
            out.append(f"volume {v['name']}: {sorted(kinds)}")
    for s in psc.get("sysctls") or []:
        if s["name"] not in SAFE_SYSCTLS:
            out.append(f"sysctl {s['name']}")
    if (psc.get("appArmorProfile") or {}).get("type") == "Unconfined":
        out.append("pod appArmorProfile")
    pod_nonroot = psc.get("runAsNonRoot") is True
    pod_seccomp = (psc.get("seccompProfile") or {}).get("type")
    if psc.get("runAsUser") == 0:
        out.append("pod runAsUser 0")
    if pod_seccomp not in (None, "RuntimeDefault", "Localhost"):
        out.append(f"pod seccomp {pod_seccomp}")
    for c in containers(pod):
        n = c["name"]
        csc = c.get("securityContext") or {}
        if csc.get("privileged"):
            out.append(f"{n}: privileged")
        if csc.get("allowPrivilegeEscalation") is not False:
            out.append(f"{n}: allowPrivilegeEscalation not false")
        caps = csc.get("capabilities") or {}
        if "ALL" not in (caps.get("drop") or []):
            out.append(f"{n}: capabilities not dropped")
        added = set(caps.get("add") or [])
        if not added <= {"NET_BIND_SERVICE"}:
            out.append(f"{n}: capabilities added {sorted(added)}")
        nonroot = csc.get("runAsNonRoot")
        if nonroot is False or (nonroot is None and not pod_nonroot):
            out.append(f"{n}: runAsNonRoot")
        if csc.get("runAsUser") == 0:
            out.append(f"{n}: runAsUser 0")
        seccomp = (csc.get("seccompProfile") or {}).get("type")
        if seccomp is None and pod_seccomp is None:
            out.append(f"{n}: no seccomp profile")
        elif seccomp not in (None, "RuntimeDefault", "Localhost"):
            out.append(f"{n}: seccomp {seccomp}")
        if csc.get("procMount") not in (None, "Default"):
            out.append(f"{n}: procMount")
        if (csc.get("appArmorProfile") or {}).get("type") == "Unconfined":
            out.append(f"{n}: appArmorProfile")
        selinux = csc.get("seLinuxOptions") or {}
        if (
            selinux.get("type")
            not in (
                None,
                "",
                "container_t",
                "container_init_t",
                "container_kvm_t",
                "container_engine_t",
            )
            or selinux.get("user")
            or selinux.get("role")
        ):
            out.append(f"{n}: seLinuxOptions")
        for port in c.get("ports") or []:
            if port.get("hostPort"):
                out.append(f"{n}: hostPort")
    return out


class TestPodSecurity:
    """T48: a foothold in any pod gets no root, no token and nowhere to persist."""

    def test_every_pod_meets_the_restricted_profile(self, rendered: list[dict]) -> None:
        pods = pod_templates(rendered)
        assert pods
        for name, pod in pods.items():
            assert restricted_violations(pod) == [], name

    def test_the_check_catches_a_violation(self) -> None:
        # The checker itself, on a pod the profile refuses.
        pod = pod_templates(render(*RENDERS["frontend-public"]))["frontend"]
        c = pod["spec"]["containers"][0]
        c["securityContext"]["allowPrivilegeEscalation"] = True
        pod["spec"]["volumes"].append({"name": "h", "hostPath": {"path": "/"}})
        assert len(restricted_violations(pod)) == 2

    def test_no_kubernetes_api_token(self, rendered: list[dict]) -> None:
        for name, pod in pod_templates(rendered).items():
            assert pod["spec"]["automountServiceAccountToken"] is False, name
        for sa in of_kind(rendered, "ServiceAccount").values():
            assert sa["automountServiceAccountToken"] is False

    def test_read_only_root_and_a_writable_home(self, rendered: list[dict]) -> None:
        for pod in pod_templates(rendered).values():
            volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
            for c in containers(pod):
                assert c["securityContext"]["readOnlyRootFilesystem"] is True
                mounts = {m["mountPath"]: m["name"] for m in c["volumeMounts"]}
                # nginx keeps its pid and temp files in /tmp; Python code gets HOME.
                assert "emptyDir" in volumes[mounts["/tmp"]], c["name"]
                env = {e["name"]: e.get("value") for e in c.get("env", [])}
                if c["name"] != "frontend":
                    assert env["HOME"] == "/tmp", c["name"]

    def test_python_runs_from_the_venv(self, rendered: list[dict]) -> None:
        # `uv run` syncs the venv before it runs, which a read-only root refuses.
        for pod in pod_templates(rendered).values():
            for c in containers(pod):
                if c["name"] == "frontend":
                    continue
                assert c["command"][0].startswith("/app/.venv/bin/"), c["name"]
                assert "uv" not in c["command"], c["name"]
                env = {e["name"]: e.get("value") for e in c["env"]}
                assert env["PYTHONPATH"] == "/app/src", c["name"]

    def test_training_caches_and_work_dir(self) -> None:
        pod = pod_templates(render(*RENDERS["training"]))
        (job,) = pod.values()
        (c,) = job["spec"]["containers"]
        env = {e["name"]: e["value"] for e in c["env"]}
        for var in ("XDG_CACHE_HOME", "TORCH_HOME", "MPLCONFIGDIR"):
            assert env[var].startswith("/tmp/"), var
        mounts = {m["mountPath"]: m["name"] for m in c["volumeMounts"]}
        # The trainer's default output_path, relative to the image's WORKDIR, and
        # mmengine's work_dir.
        assert c["workingDir"] == "/app"
        assert mounts["/app/training_output"] == "work"


def _worker(docs: list[dict]) -> dict:
    return pod_templates(docs)["worker"]


def _by_name(pod: dict) -> dict[str, dict]:
    return {c["name"]: c for c in pod["spec"]["containers"]}


def _env(c: dict) -> dict[str, str]:
    return {e["name"]: e.get("value", "") for e in c.get("env", [])}


@pytest.fixture(scope="module")
def pod() -> dict:
    """The worker's pod template, as deploy.sh renders it."""
    return _worker(render("backend", BACKEND))


class TestParserContainer:
    """T11, pod side: untrusted files are parsed away from the worker's credentials."""

    def test_irsa_skips_the_parser(self, pod: dict) -> None:
        # The pod identity webhook injects no role, token path or token mount into it.
        annotations = pod["metadata"]["annotations"]
        assert annotations["eks.amazonaws.com/skip-containers"] == "parser"

    def test_parser_holds_no_credential(self, pod: dict) -> None:
        parser = _by_name(pod)["parser"]
        assert parser["command"] == [
            "/app/.venv/bin/python",
            "-P",
            "-m",
            "pointcloud_detect.worker.parser_sidecar",
        ]
        assert "envFrom" not in parser
        env = _env(parser)
        assert not [k for k in env if k.startswith("AWS_")]
        assert env["PCD_PARSER_SPOOL"] == "/scratch/ipc"
        assert env["XERCES_DISABLE_DTD"] == "1"
        assert env["HOME"] == "/tmp"
        mounts = {m["mountPath"] for m in parser["volumeMounts"]}
        assert mounts == {"/scratch/ipc", "/tmp"}
        assert not [m for m in mounts if "serviceaccount" in m]
        assert pod["spec"]["automountServiceAccountToken"] is False
        assert "shareProcessNamespace" not in pod["spec"]

    def test_parser_runs_as_another_uid_in_the_workers_group(self, pod: dict) -> None:
        containers = _by_name(pod)
        parser = containers["parser"]["securityContext"]
        worker_uid = pod["spec"]["securityContext"]["runAsUser"]
        assert worker_uid == 999
        assert "runAsUser" not in containers["worker"]["securityContext"]
        assert parser["runAsUser"] == 1001 != worker_uid
        assert parser["runAsGroup"] == 999
        assert pod["spec"]["securityContext"]["fsGroup"] == 999
        assert parser["runAsNonRoot"] is True
        assert parser["readOnlyRootFilesystem"] is True
        assert parser["allowPrivilegeEscalation"] is False
        assert parser["capabilities"] == {"drop": ["ALL"]}
        assert parser["seccompProfile"] == {"type": "RuntimeDefault"}

    def test_both_mount_scratch_and_only_the_worker_has_a_gpu(self, pod: dict) -> None:
        containers = _by_name(pod)
        worker_mounts = {
            m["mountPath"]: m for m in containers["worker"]["volumeMounts"]
        }
        assert worker_mounts["/scratch"]["name"] == "scratch"
        assert "subPath" not in worker_mounts["/scratch"]
        # The parser sees the spool and nothing else of scratch: not the worker's
        # TMPDIR or extracts, and not the volume's root.
        parser_mounts = {m["name"]: m for m in containers["parser"]["volumeMounts"]}
        assert parser_mounts["scratch"]["mountPath"] == "/scratch/ipc"
        assert parser_mounts["scratch"]["subPath"] == "ipc"
        assert _env(containers["parser"])["PCD_PARSER_SPOOL"] == "/scratch/ipc"
        # Each has a /tmp of its own.
        assert _by_name(pod)["worker"]["volumeMounts"][1]["name"] == "tmp"
        assert {m["name"] for m in containers["parser"]["volumeMounts"]} == {
            "scratch",
            "parser-tmp",
        }
        parser_resources = containers["parser"]["resources"]
        for kind in ("requests", "limits"):
            assert "nvidia.com/gpu" not in parser_resources[kind]
        assert containers["worker"]["resources"]["limits"]["nvidia.com/gpu"] == "1"
        # Without a request the device plugin gives it none, but a CUDA base image's
        # NVIDIA_VISIBLE_DEVICES=all would still get every GPU from NVIDIA's runtime.
        parser_env = _env(containers["parser"])
        assert parser_env["NVIDIA_VISIBLE_DEVICES"] == "void"
        assert "NVIDIA_VISIBLE_DEVICES" not in _env(containers["worker"])

    def test_worker_hands_files_to_the_parser(self, pod: dict) -> None:
        env = _env(_by_name(pod)["worker"])
        assert env["PCD_PARSER_MODE"] == "sidecar"
        assert env["PCD_PARSER_SPOOL"] == "/scratch/ipc"

    def test_requests_still_fit_the_smallest_node(self, pod: dict) -> None:
        # g4dn.xlarge and m5.xlarge: 4 vCPU, 16 GiB. What one worker requested before.
        def cpu(v: str) -> int:
            return int(v[:-1]) if v.endswith("m") else int(float(v) * 1000)

        def gib(v: str) -> float:
            return float(v[:-2]) if v.endswith("Gi") else float(v[:-2]) / 1024

        requests = [c["resources"]["requests"] for c in pod["spec"]["containers"]]
        assert sum(cpu(r["cpu"]) for r in requests) <= 2000
        assert sum(gib(r["memory"]) for r in requests) <= 8

    def test_parser_may_use_what_the_worker_used_to(self, pod: dict) -> None:
        # Whole-load parses moved from the worker to the parser; a lower memory limit
        # would fail files that used to parse.
        containers = _by_name(pod)
        assert (
            containers["parser"]["resources"]["limits"]["memory"]
            == containers["worker"]["resources"]["limits"]["memory"]
            == "16Gi"
        )

    def test_parser_can_be_switched_off(self) -> None:
        pod = _worker(render("backend", {**BACKEND, "worker.parser.enabled": "false"}))
        assert set(_by_name(pod)) == {"worker"}
        assert "PCD_PARSER_MODE" not in _env(_by_name(pod)["worker"])
        assert "eks.amazonaws.com/skip-containers" not in pod["metadata"]["annotations"]


class TestXercesDtd:
    """T54, defence in depth: pye57's Xerces-C refuses a DTD wherever it runs."""

    def test_in_the_shared_config(self) -> None:
        config = of_kind(render("backend", BACKEND), "ConfigMap")["backend-config"]
        assert config["data"]["XERCES_DISABLE_DTD"] == "1"


class TestProxyHeaders:
    """T57/T45: behind the ALB, redirects say https and logs name the client."""

    def test_public_mode_trusts_only_the_alb(self) -> None:
        api = _by_name(pod_templates(render("backend", BACKEND))["api"])["api"]
        assert "--proxy-headers" in api["command"]
        assert "--forwarded-allow-ips=10.0.101.0/24,10.0.102.0/24" in api["command"]

    def test_port_forward_mode_trusts_nothing(self) -> None:
        chart, values = RENDERS["backend-port-forward"]
        api = _by_name(pod_templates(render(chart, values))["api"])["api"]
        assert not [a for a in api["command"] if "forwarded" in a or "proxy" in a]


CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob:; worker-src 'self' blob:; "
    "connect-src 'self' "
    "https://amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com "
    "https://amzn-s3-demo-bucket.s3.amazonaws.com; "
    "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)
HSTS = "max-age=31536000; includeSubDomains"


def _header_blocks(conf: str) -> list[str]:
    """The server block and each location with an add_header of its own."""
    blocks = [b for b in conf.split("location ")[1:] if "add_header" in b.split("}")[0]]
    return [b.split("}")[0] for b in blocks] + [conf.rsplit("}", 2)[-2]]


class TestContentSecurityPolicy:
    """T35: every response the SPA's nginx sends carries the policy."""

    @pytest.mark.parametrize("values", [PUBLIC, PORT_FORWARD], ids=["public", "pf"])
    def test_every_add_header_block_repeats_it(self, values: dict[str, str]) -> None:
        conf = nginx_conf(render("frontend", values))
        blocks = _header_blocks(conf)
        # The static-asset location, the server level and, in public mode, /signout.
        assert len(blocks) == (3 if values is PUBLIC else 2)
        for block in blocks:
            assert f'add_header Content-Security-Policy "{CSP}" always;' in block
            assert 'add_header X-Frame-Options "DENY" always;' in block
            assert 'add_header X-Content-Type-Options "nosniff" always;' in block

    def test_report_only_is_a_switch(self) -> None:
        conf = nginx_conf(
            render("frontend", {**PUBLIC, "contentSecurityPolicy.reportOnly": "true"})
        )
        assert "Content-Security-Policy-Report-Only" in conf
        assert 'add_header Content-Security-Policy "' not in conf

    def test_upload_origins_follow_the_region(self) -> None:
        conf = nginx_conf(render("frontend", {**PUBLIC, "aws.region": "eu-west-1"}))
        assert "https://amzn-s3-demo-bucket.s3.eu-west-1.amazonaws.com " in conf

    def test_the_bucket_is_required(self) -> None:
        for values in (PUBLIC, PORT_FORWARD):
            err = render_error("frontend", {**values, "aws.s3.inputBucket": ""})
            assert "aws.s3.inputBucket is required" in err

    def test_local_nginx_conf_has_the_same_policy(self) -> None:
        # frontend/nginx.conf, the docker-compose copy: the same policy, with the
        # local S3 endpoint in place of the bucket.
        conf = (CHARTS.parents[1] / "frontend" / "nginx.conf").read_text()
        local = (
            CSP.split("connect-src")[0] + "connect-src 'self' http://localhost:4566; "
        )
        local += CSP.split("amazonaws.com; ")[-1]
        assert conf.count(f'add_header Content-Security-Policy "{local}" always;') == 2
        assert "Strict-Transport-Security" not in conf


class TestHsts:
    """T57: HTTPS only, on the pages and on /api, which the ALB sends to the api."""

    def test_nginx_sends_it_in_public_mode_only(self) -> None:
        public = nginx_conf(render("frontend", PUBLIC))
        for block in _header_blocks(public):
            assert f'add_header Strict-Transport-Security "{HSTS}" always;' in block
        assert "Strict-Transport-Security" not in nginx_conf(
            render("frontend", PORT_FORWARD)
        )

    def test_the_alb_sends_it_on_every_response(self) -> None:
        ingresses = of_kind(render("frontend", PUBLIC), "Ingress")
        assert set(ingresses) == {"pointcloud-api", "pointcloud-spa"}
        for ing in ingresses.values():
            a = ing["metadata"]["annotations"]
            assert a[A + "listener-attributes.HTTPS-443"] == (
                f"routing.http.response.strict_transport_security.header_value={HSTS}"
            )

    def test_adopters_cannot_override_it(self) -> None:
        key = "alb\\.ingress\\.kubernetes\\.io/listener-attributes\\.HTTPS-443"
        err = render_error("frontend", {**PUBLIC, f"ingress.annotations.{key}": "x=y"})
        assert "may not set" in err
