"""Render tests for the Helm charts: the access controls that live in manifests.

The API has no authentication code. In public mode every request is authenticated by
the ALB's Cognito action, which exists only as Ingress annotations, and in
port-forward mode the gate is Kubernetes RBAC. So the security of both modes is in
what these charts render, and these tests pin it: both Ingresses carry the Cognito
action, /api needs the CSRF header, an internet-facing listener cannot be rendered
without sign-in or a certificate, port-forward mode renders no Ingress at all, and the
NetworkPolicies admit only the intended callers.

They run `helm template` and skip when helm is not installed. CI sets REQUIRE_HELM=1
so a runner that loses helm fails instead of quietly skipping.
"""

import json
import os
import re
import shutil
from pathlib import Path

import pytest
import yaml

from tests._proc import Finished, minimal_env, run

ROOT = Path(__file__).resolve().parents[1]
CHARTS = ROOT / "infrastructure" / "helm"
HELM = shutil.which("helm")

if HELM is None:
    if os.environ.get("REQUIRE_HELM") == "1":
        raise RuntimeError("REQUIRE_HELM=1 but helm is not on PATH")
    pytest.skip("helm not installed", allow_module_level=True)

A = "alb.ingress.kubernetes.io/"
POOL_ARN = "arn:aws:cognito-idp:us-east-1:111122223333:userpool/us-east-1_AbC123"

# Every chart requires an image tag, and refuses latest (T29).
ECR = "111122223333.dkr.ecr.us-east-1.amazonaws.com/"
IMAGE = {
    "image.repository": ECR + "pointcloud-detection-frontend",
    "image.tag": "0123abc",
}
BACKEND_IMAGE = {**IMAGE, "image.repository": ECR + "pointcloud-detection-backend"}
# The frontend's Content-Security-Policy admits uploads to this bucket only (T35).
BUCKET = {"aws.s3.inputBucket": "amzn-s3-demo-bucket"}

PUBLIC = {
    **IMAGE,
    **BUCKET,
    "ingress.certificateArn": "arn:aws:acm:us-east-1:111122223333:certificate/abc",
    "ingress.host": "PCD.Example.com",
    "ingress.auth.userPoolArn": POOL_ARN,
    "ingress.auth.userPoolClientId": "client123",
    "ingress.auth.userPoolDomain": "pointcloud-detection-dev-1a2b3c4d",
    "portForward.group": "pointcloud-detection-app-clients",
    "networkPolicy.albSourceCidrs": "{10.0.101.0/24,10.0.102.0/24}",
}
PORT_FORWARD = {
    **IMAGE,
    **BUCKET,
    "accessMode": "port-forward",
    "portForward.group": "pcd-clients",
}
BACKEND = {
    **BACKEND_IMAGE,
    "publicHost": "PCD.Example.com",
    "jobStore.tableName": "jobs",
    "aws.sqs.queueUrl": "https://sqs.us-east-1.amazonaws.com/111122223333/q",
    "networkPolicy.albSourceCidrs": "{10.0.101.0/24,10.0.102.0/24}",
    "networkPolicy.vpcCidr": "10.0.0.0/16",
    # Public mode's default access scope is per-user, which checks sign-ins against
    # this pool and app client (T31).
    "identity.userPoolId": "us-east-1_AbC123",
    "identity.clientId": "client123",
}

TRAINING = {
    **BACKEND_IMAGE,
    "image.tag": "0123abc-gpu",
    "networkPolicy.vpcCidr": "10.0.0.0/16",
}

# Cluster DNS: the one in-cluster destination every egress policy allows.
DNS_EGRESS = {
    "to": [
        {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
            },
            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
        }
    ],
    "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}],
}
# HTTPS anywhere but the VPC and link-local: AWS APIs, not the cluster API or IMDS.
HTTPS_OUT_OF_VPC = {
    "to": [
        {"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.0.0.0/16", "169.254.0.0/16"]}}
    ],
    "ports": [{"port": 443, "protocol": "TCP"}],
}


def _render(chart: str, values: dict[str, str]) -> Finished:
    args = ["template", "t", str(CHARTS / chart)]
    for k, v in values.items():
        # --set-string would turn the {a,b} list syntax into a literal string, and
        # "false" into a string, which templates read as true.
        typed = v.startswith("{") or v in ("true", "false")
        args += ["--set" if typed else "--set-string", f"{k}={v}"]
    return run("helm", args, env=minimal_env(), timeout=120)


def render(chart: str, values: dict[str, str]) -> list[dict]:
    r = _render(chart, values)
    assert r.returncode == 0, r.stderr
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def render_error(chart: str, values: dict[str, str]) -> str:
    r = _render(chart, values)
    assert r.returncode != 0, "rendered, but should have been refused"
    return r.stderr


def of_kind(docs: list[dict], kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}


def nginx_conf(docs: list[dict]) -> str:
    return of_kind(docs, "ConfigMap")["frontend-nginx"]["data"]["default.conf"]


@pytest.fixture(scope="module")
def public_docs() -> list[dict]:
    return render("frontend", PUBLIC)


@pytest.fixture(scope="module")
def ingresses(public_docs: list[dict]) -> dict[str, dict]:
    return of_kind(public_docs, "Ingress")


@pytest.fixture(scope="module")
def port_forward_docs() -> list[dict]:
    return render("frontend", PORT_FORWARD)


@pytest.fixture(scope="module")
def backend_public() -> list[dict]:
    return render("backend", BACKEND)


class TestPublicMode:
    """The internet-facing ALB, with Cognito on every rule."""

    def test_two_ingresses_share_one_alb(self, ingresses: dict[str, dict]) -> None:
        assert set(ingresses) == {"pointcloud-api", "pointcloud-spa"}
        groups = {
            i["metadata"]["annotations"][A + "group.name"] for i in ingresses.values()
        }
        assert groups == {"pointcloud-detection"}

    def test_every_ingress_authenticates_with_cognito(
        self, ingresses: dict[str, dict]
    ) -> None:
        idp = {
            "userPoolARN": POOL_ARN,
            "userPoolClientID": "client123",
            "userPoolDomain": "pointcloud-detection-dev-1a2b3c4d",
        }
        for name, ing in ingresses.items():
            a = ing["metadata"]["annotations"]
            assert a[A + "auth-type"] == "cognito", name
            assert json.loads(a[A + "auth-idp-cognito"]) == idp, name
            assert a[A + "auth-scope"] == "openid email", name
            assert a[A + "auth-session-cookie"] == "AWSELBAuthSessionCookie", name
            assert a[A + "auth-session-timeout"] == "43200", name

    def test_https_only(self, ingresses: dict[str, dict]) -> None:
        # Auth runs only on HTTPS listeners, and each group member must set its own
        # certificate and listen-ports or its rules are silently dropped.
        for name, ing in ingresses.items():
            a = ing["metadata"]["annotations"]
            assert a[A + "certificate-arn"].startswith("arn:aws:acm:"), name
            assert json.loads(a[A + "listen-ports"]) == [{"HTTP": 80}, {"HTTPS": 443}]
            assert a[A + "ssl-redirect"] == "443", name
            assert a[A + "scheme"] == "internet-facing", name

    def test_exclusive_group_annotations_agree(
        self, ingresses: dict[str, dict]
    ) -> None:
        keys = ["scheme", "ssl-redirect", "ssl-policy", "load-balancer-attributes"]
        values = [
            tuple(i["metadata"]["annotations"][A + k] for k in keys)
            for i in ingresses.values()
        ]
        assert len(set(values)) == 1

    def test_api_denies_and_spa_redirects(self, ingresses: dict[str, dict]) -> None:
        api = ingresses["pointcloud-api"]["metadata"]["annotations"]
        spa = ingresses["pointcloud-spa"]["metadata"]["annotations"]
        assert api[A + "auth-on-unauthenticated-request"] == "deny"
        assert spa[A + "auth-on-unauthenticated-request"] == "authenticate"
        # The spa rule matches /*, so the api rules must be evaluated first.
        assert int(api[A + "group.order"]) < int(spa[A + "group.order"])
        assert api[A + "healthcheck-path"] == "/health"

    def test_api_requires_the_client_header(self, ingresses: dict[str, dict]) -> None:
        a = ingresses["pointcloud-api"]["metadata"]["annotations"]
        assert json.loads(a[A + "conditions.api"]) == [
            {
                "field": "http-header",
                "httpHeaderConfig": {
                    "httpHeaderName": "X-PCD-Client",
                    "values": ["spa"],
                },
            }
        ]
        action = json.loads(a[A + "actions.api-client-header-required"])
        assert action["type"] == "fixed-response"
        assert action["fixedResponseConfig"]["statusCode"] == "403"

        paths = ingresses["pointcloud-api"]["spec"]["rules"][0]["http"]["paths"]
        forward, fallback = paths
        assert (forward["path"], forward["pathType"]) == ("/api", "Prefix")
        assert forward["backend"]["service"]["name"] == "api"
        # ImplementationSpecific sorts after Prefix, so this only catches requests the
        # header condition did not match.
        assert (fallback["path"], fallback["pathType"]) == (
            "/api*",
            "ImplementationSpecific",
        )
        assert fallback["backend"]["service"] == {
            "name": "api-client-header-required",
            "port": {"name": "use-annotation"},
        }

    def test_rules_match_only_the_lowercased_host(
        self, ingresses: dict[str, dict]
    ) -> None:
        for ing in ingresses.values():
            assert [r["host"] for r in ing["spec"]["rules"]] == ["pcd.example.com"]

    def test_sign_out_expires_every_cookie_shard(self, public_docs: list[dict]) -> None:
        conf = nginx_conf(public_docs)
        assert "location = /signout" in conf
        for n in range(4):
            assert f'Set-Cookie "AWSELBAuthSessionCookie-{n}=; ' in conf
        assert (
            "https://pointcloud-detection-dev-1a2b3c4d.auth.us-east-1.amazoncognito.com"
            "/logout?client_id=client123&logout_uri=https%3A%2F%2Fpcd.example.com%2F"
        ) in conf
        # The ALB routes /api straight to the api Service; nginx must not proxy it.
        assert "proxy_pass" not in conf

    def test_nginx_accepts_the_largest_session_cookie(
        self, public_docs: list[dict]
    ) -> None:
        # Up to 16K of cookie shards on one Cookie line; the default 8k would 400.
        m = re.search(
            r"large_client_header_buffers (\d+) (\d+)k;", nginx_conf(public_docs)
        )
        assert m and int(m.group(2)) >= 16

    def test_extra_annotations_are_passed_through(self) -> None:
        docs = render(
            "frontend",
            {
                **PUBLIC,
                "ingress.annotations.alb\\.ingress\\.kubernetes\\.io/tags": "team=pcd",
            },
        )
        for ing in of_kind(docs, "Ingress").values():
            assert ing["metadata"]["annotations"][A + "tags"] == "team=pcd"


class TestPublicModeRefusesUnsafeValues:
    """An internet-facing listener without sign-in must be impossible to render."""

    @pytest.mark.parametrize(
        "missing",
        [
            "ingress.auth.userPoolArn",
            "ingress.auth.userPoolClientId",
            "ingress.auth.userPoolDomain",
        ],
    )
    def test_without_cognito(self, missing: str) -> None:
        err = render_error("frontend", {**PUBLIC, missing: ""})
        assert "Cognito sign-in on every rule" in err

    def test_without_a_certificate(self) -> None:
        err = render_error("frontend", {**PUBLIC, "ingress.certificateArn": ""})
        assert "HTTPS listeners" in err

    def test_without_a_host(self) -> None:
        assert "ingress.host" in render_error(
            "frontend", {**PUBLIC, "ingress.host": ""}
        )

    def test_defaults_alone(self) -> None:
        # accessMode defaults to public, so a bare render must fail, not open up.
        render_error("frontend", {})
        # And not only for want of an image: the public-mode checks refuse it too.
        assert "accessMode=public requires" in render_error(
            "frontend", {**IMAGE, **BUCKET}
        )

    @pytest.mark.parametrize(
        "domain",
        [
            "https://x.auth.us-east-1.amazoncognito.com",
            "x.auth.us-east-1.amazoncognito.com",
        ],
    )
    def test_domain_must_be_a_prefix(self, domain: str) -> None:
        err = render_error(
            "frontend", {**PUBLIC, "ingress.auth.userPoolDomain": domain}
        )
        assert "domain prefix only" in err

    @pytest.mark.parametrize(
        "key",
        [
            "auth-type",
            "auth-on-unauthenticated-request",
            "scheme",
            "listen-ports",
            "ssl-redirect",
            "certificate-arn",
            "group.name",
            "group.order",
            "conditions.api",
            "actions.api-client-header-required",
            "healthcheck-path",
            "load-balancer-attributes",
            # Custom groups make the controller ignore inbound-cidrs.
            "security-groups",
            "manage-backend-security-group-rules",
        ],
    )
    def test_annotation_overrides_refused(self, key: str) -> None:
        esc = "alb\\.ingress\\.kubernetes\\.io/" + key.replace(".", "\\.")
        err = render_error("frontend", {**PUBLIC, f"ingress.annotations.{esc}": "x"})
        assert "may not set" in err

    def test_unknown_access_mode(self) -> None:
        err = render_error("frontend", {**PUBLIC, "accessMode": "internet"})
        assert "accessMode" in err

    def test_another_ingress_controller(self) -> None:
        # Any controller but the AWS one ignores the auth annotations entirely.
        err = render_error("frontend", {**PUBLIC, "ingress.className": "nginx"})
        assert "no authentication" in err


class TestPortForwardMode:
    """No load balancer; nginx proxies /api and RBAC is the gate."""

    def test_no_ingress(self, port_forward_docs: list[dict]) -> None:
        assert of_kind(port_forward_docs, "Ingress") == {}

    def test_nginx_proxies_api(self, port_forward_docs: list[dict]) -> None:
        conf = nginx_conf(port_forward_docs)
        assert "location /api/ {" in conf
        assert re.search(
            r"set \$api_upstream http://api\.pointcloud-detection\.svc\.cluster\.local:80;",
            conf,
        )
        assert "proxy_pass $api_upstream;" in conf
        # The SPA's Sign out link explains itself rather than faking a sign-out.
        assert "Nothing to sign out of" in conf
        assert "amazoncognito.com" not in conf

    def test_nginx_applies_the_csrf_and_host_rules(
        self, port_forward_docs: list[dict]
    ) -> None:
        api = nginx_conf(port_forward_docs).split("location /api/ {", 1)[1]
        header_rule = api.index('if ($http_x_pcd_client != "spa") {\n')
        host_rule = api.index("if ($host !~ ^(localhost|127\\.0\\.0\\.1)$) {")
        # Both refuse before anything is proxied.
        assert max(header_rule, host_rule) < api.index("proxy_pass")

    def test_deployment_mounts_the_config(self, port_forward_docs: list[dict]) -> None:
        pod = of_kind(port_forward_docs, "Deployment")["frontend"]["spec"]["template"][
            "spec"
        ]
        mount = next(
            m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "nginx-conf"
        )
        assert mount["mountPath"] == "/etc/nginx/conf.d/default.conf"
        assert mount["readOnly"] is True
        # The root filesystem stays read-only; the config comes in as a volume.
        assert pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"] is True

    def test_needs_no_certificate_or_cognito(self) -> None:
        render("frontend", {**IMAGE, **BUCKET, "accessMode": "port-forward"})


class TestPortForwardRole:
    """The only programmatic path to the API, in either mode."""

    @pytest.mark.parametrize(
        "values", [PUBLIC, PORT_FORWARD], ids=["public", "port-forward"]
    )
    def test_role_is_port_forward_only(self, values: dict[str, str]) -> None:
        docs = render("frontend", values)
        role = of_kind(docs, "Role")["app-port-forward"]
        grants = {
            (res, verb)
            for rule in role["rules"]
            for res in rule["resources"]
            for verb in rule["verbs"]
        }
        assert grants == {
            ("services", "get"),
            ("pods", "get"),
            ("pods", "list"),
            ("pods/portforward", "get"),
            ("pods/portforward", "create"),
        }
        assert all(rule["apiGroups"] == [""] for rule in role["rules"])
        binding = of_kind(docs, "RoleBinding")["app-port-forward"]
        assert binding["subjects"] == [
            {
                "kind": "Group",
                "apiGroup": "rbac.authorization.k8s.io",
                "name": values["portForward.group"],
            }
        ]
        assert binding["roleRef"]["name"] == "app-port-forward"

    def test_no_role_without_a_group(self) -> None:
        docs = render("frontend", {**IMAGE, **BUCKET, "accessMode": "port-forward"})
        assert of_kind(docs, "Role") == {}


class TestFrontendNetworkPolicy:
    """In port-forward mode nginx relays /api, so the frontend pods are an API client.

    Without a policy of their own, any pod in the cluster could send the client header
    and a localhost Host to the frontend Service and reach every API route. The
    tunnel is not affected: it arrives through the kubelet, gated by RBAC.
    """

    @staticmethod
    def _policy(docs: list[dict]) -> dict:
        policy = of_kind(docs, "NetworkPolicy")["allow-ingress-to-frontend"]
        assert policy["spec"]["podSelector"] == {"matchLabels": {"app": "frontend"}}
        assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
        for rule in policy["spec"]["ingress"]:
            assert rule["ports"] == [{"port": 8080}]
        return policy

    @staticmethod
    def _sources(policy: dict) -> list[dict]:
        return [f for rule in policy["spec"]["ingress"] for f in rule["from"]]

    def test_port_forward_admits_no_network_source(
        self, port_forward_docs: list[dict]
    ) -> None:
        # The tunnel arrives through the kubelet, and kubelet probes come from the
        # pod's own node, which the VPC CNI always admits. Nothing else calls nginx.
        assert self._policy(port_forward_docs)["spec"]["ingress"] == []

    def test_public_admits_only_the_alb(self, public_docs: list[dict]) -> None:
        sources = self._sources(self._policy(public_docs))
        assert sources == [
            {"ipBlock": {"cidr": "10.0.101.0/24"}},
            {"ipBlock": {"cidr": "10.0.102.0/24"}},
        ]

    def test_egress_only_where_nginx_proxies(
        self, public_docs: list[dict], port_forward_docs: list[dict]
    ) -> None:
        # Public mode: static files only, the ALB sends /api to the api pods.
        assert self._policy(public_docs)["spec"]["egress"] == []
        # Port-forward mode: cluster DNS and the api pods, nothing else.
        dns, api = self._policy(port_forward_docs)["spec"]["egress"]
        assert dns == DNS_EGRESS
        assert api == {
            "to": [{"podSelector": {"matchLabels": {"app": "api"}}}],
            "ports": [{"port": 8000}],
        }

    def test_public_mode_needs_alb_cidrs(self) -> None:
        values = dict(PUBLIC)
        del values["networkPolicy.albSourceCidrs"]
        assert "albSourceCidrs" in render_error("frontend", values)


class TestAccessScope:
    """T31: per-user is public mode's default, and needs what it verifies against."""

    @staticmethod
    def _config(values: dict[str, str]) -> dict[str, str]:
        return of_kind(render("backend", values), "ConfigMap")["backend-config"]["data"]

    def test_public_mode_defaults_to_per_user(self) -> None:
        config = self._config(BACKEND)
        assert config["ACCESS_SCOPE"] == "per-user"
        assert config["COGNITO_USER_POOL_ID"] == "us-east-1_AbC123"
        assert config["COGNITO_CLIENT_ID"] == "client123"
        # Unknown until the ALB exists; the API answers 503 until deploy.sh sets it.
        assert config["ALB_ARN"] == ""

    def test_the_alb_arn_is_passed(self) -> None:
        arn = (
            "arn:aws:elasticloadbalancing:us-east-1:111122223333:"
            "loadbalancer/app/k8s-pointclouddetect-0123456789/0123456789abcdef"
        )
        assert self._config({**BACKEND, "identity.albArn": arn})["ALB_ARN"] == arn

    def test_port_forward_mode_defaults_to_shared(self) -> None:
        values = {**BACKEND, "accessMode": "port-forward"}
        del values["networkPolicy.albSourceCidrs"]
        assert self._config(values)["ACCESS_SCOPE"] == "shared"

    def test_public_mode_can_opt_out(self) -> None:
        values = {**BACKEND, "accessScope": "shared"}
        del values["identity.userPoolId"], values["identity.clientId"]
        assert self._config(values)["ACCESS_SCOPE"] == "shared"

    def test_per_user_is_refused_in_port_forward_mode(self) -> None:
        values = {**BACKEND, "accessMode": "port-forward", "accessScope": "per-user"}
        del values["networkPolicy.albSourceCidrs"]
        assert "needs accessMode=public" in render_error("backend", values)

    @pytest.mark.parametrize("field", ["identity.userPoolId", "identity.clientId"])
    def test_per_user_needs_the_user_pool(self, field: str) -> None:
        values = dict(BACKEND)
        del values[field]
        assert "identity.userPoolId and identity.clientId" in render_error(
            "backend", values
        )

    def test_per_user_needs_the_shared_store(self) -> None:
        values = {**BACKEND, "jobStore.backend": "memory"}
        assert "jobStore.backend=dynamodb" in render_error("backend", values)

    def test_an_unknown_scope(self) -> None:
        error = render_error("backend", {**BACKEND, "accessScope": "everyone"})
        assert "accessScope" in error


class TestBackend:
    def test_api_admits_only_the_alb_in_public_mode(
        self, backend_public: list[dict]
    ) -> None:
        policy = of_kind(backend_public, "NetworkPolicy")["allow-ingress-to-api"]
        sources = [f for rule in policy["spec"]["ingress"] for f in rule["from"]]
        cidrs = [s["ipBlock"]["cidr"] for s in sources if "ipBlock" in s]
        assert cidrs == ["10.0.101.0/24", "10.0.102.0/24"]
        assert not any(
            s.get("podSelector", {}).get("matchLabels") == {"app": "frontend"}
            for s in sources
        )
        # Nothing but the ALB: no longer every kube-system pod (M49).
        assert all("ipBlock" in s for s in sources)

    def test_api_admits_the_frontend_in_port_forward_mode(self) -> None:
        values = {**BACKEND, "accessMode": "port-forward"}
        del values["networkPolicy.albSourceCidrs"]
        docs = render("backend", values)
        policy = of_kind(docs, "NetworkPolicy")["allow-ingress-to-api"]
        sources = [f for rule in policy["spec"]["ingress"] for f in rule["from"]]
        assert not any("ipBlock" in s for s in sources)
        assert sources == [{"podSelector": {"matchLabels": {"app": "frontend"}}}]

    def test_public_mode_still_needs_alb_cidrs(self) -> None:
        values = dict(BACKEND)
        del values["networkPolicy.albSourceCidrs"]
        assert "albSourceCidrs" in render_error("backend", values)

    def test_unknown_access_mode(self) -> None:
        assert "accessMode" in render_error("backend", {**BACKEND, "accessMode": "x"})

    @staticmethod
    def _allowed_hosts(docs: list[dict]) -> list[str]:
        config = of_kind(docs, "ConfigMap")["backend-config"]["data"]
        return config["ALLOWED_HOSTS"].split(",")

    def test_api_serves_only_its_own_host_names(
        self, backend_public: list[dict]
    ) -> None:
        # A DNS-rebinding page on the api tunnel sends its own name, which is refused.
        tunnel_and_cluster = [
            "localhost",
            "127.0.0.1",
            "api",
            "api.pointcloud-detection",
            "api.pointcloud-detection.svc",
            "api.pointcloud-detection.svc.cluster.local",
        ]
        assert self._allowed_hosts(backend_public) == [
            *tunnel_and_cluster,
            "pcd.example.com",
        ]
        values = {**BACKEND, "accessMode": "port-forward"}
        del values["networkPolicy.albSourceCidrs"]
        assert self._allowed_hosts(render("backend", values)) == tunnel_and_cluster

    def test_public_mode_needs_the_public_host(self) -> None:
        assert "publicHost" in render_error("backend", {**BACKEND, "publicHost": ""})


class TestServicesCarryNoAlbOverrides:
    """T71: a Service's auth-* annotations override the Ingress's for its rule.

    The controller merges a forwarded-to Service's annotations over the Ingress's, the
    Service winning, so an auth-type or auth-on-unauthenticated-request annotation on
    the api or frontend Service would take sign-in off that rule. And a LoadBalancer
    Service would expose the pods with no sign-in at all.
    """

    @pytest.mark.parametrize(
        ("chart", "values", "name"),
        [
            ("backend", BACKEND, "api"),
            ("frontend", PUBLIC, "frontend"),
            ("frontend", PORT_FORWARD, "frontend"),
        ],
    )
    def test_cluster_ip_with_no_alb_annotations(
        self, chart: str, values: dict[str, str], name: str
    ) -> None:
        svc = of_kind(render(chart, values), "Service")[name]
        assert svc["spec"]["type"] == "ClusterIP"
        annotations = svc["metadata"].get("annotations") or {}
        assert not [k for k in annotations if k.startswith(A)]


class TestNoApplicationCredential:
    """The token is gone: nothing may render it, read it or ask for it."""

    @pytest.mark.parametrize(
        ("chart", "values"),
        [
            ("frontend", PUBLIC),
            ("frontend", PORT_FORWARD),
            ("backend", BACKEND),
            (
                "backend",
                {**BACKEND, "accessMode": "port-forward", "jobStore.backend": "memory"},
            ),
            ("training", TRAINING),
        ],
    )
    def test_no_token_in_any_render(self, chart: str, values: dict[str, str]) -> None:
        r = _render(chart, values)
        assert r.returncode == 0, r.stderr
        for needle in (
            "API_AUTH",
            "pointcloud-api-auth",
            "secretKeyRef",
            "allowInsecureHttp",
        ):
            assert needle not in r.stdout, needle

    def test_no_token_settings_in_values(self) -> None:
        for chart in ("frontend", "backend", "training"):
            text = (CHARTS / chart / "values.yaml").read_text()
            assert "allowInsecureHttp" not in text
            assert not re.search(r"^auth:", text, re.MULTILINE), chart


class TestNetworkSegmentation:
    """T49 and T61: a namespace default-deny, every flow allowed by name, no opt-out."""

    @staticmethod
    def _policies(chart: str, values: dict[str, str]) -> dict[str, dict]:
        return of_kind(render(chart, values), "NetworkPolicy")

    def test_namespace_default_deny(self, backend_public: list[dict]) -> None:
        deny = of_kind(backend_public, "NetworkPolicy")["default-deny"]
        assert deny["spec"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}

    def test_api_egress_is_dns_and_aws(self, backend_public: list[dict]) -> None:
        api = of_kind(backend_public, "NetworkPolicy")["allow-ingress-to-api"]
        assert api["spec"]["policyTypes"] == ["Ingress", "Egress"]
        assert api["spec"]["egress"] == [DNS_EGRESS, HTTPS_OUT_OF_VPC]

    def test_worker_has_no_ingress_and_the_same_egress(
        self, backend_public: list[dict]
    ) -> None:
        worker = of_kind(backend_public, "NetworkPolicy")["worker-isolation"]
        assert "ingress" not in worker["spec"]
        assert worker["spec"]["policyTypes"] == ["Ingress", "Egress"]
        assert worker["spec"]["egress"] == [DNS_EGRESS, HTTPS_OUT_OF_VPC]

    def test_training_is_isolated(self) -> None:
        policy = self._policies("training", TRAINING)["training-isolation"]
        assert policy["spec"]["podSelector"] == {"matchLabels": {"app": "training"}}
        assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
        assert "ingress" not in policy["spec"]
        assert policy["spec"]["egress"] == [DNS_EGRESS, HTTPS_OUT_OF_VPC]

    def test_no_policy_admits_kube_system_wholesale(self) -> None:
        # Only kube-dns, and only as a destination (M49).
        renders = [
            ("backend", BACKEND),
            ("frontend", PUBLIC),
            ("frontend", PORT_FORWARD),
            ("training", TRAINING),
        ]
        for chart, values in renders:
            for name, policy in self._policies(chart, values).items():
                for rule in policy["spec"].get("ingress", []):
                    for source in rule["from"]:
                        assert "namespaceSelector" not in source, name
                for rule in policy["spec"].get("egress", []):
                    for dest in rule["to"]:
                        if "namespaceSelector" in dest:
                            assert dest == DNS_EGRESS["to"][0], name

    @pytest.mark.parametrize(
        ("chart", "values"), [("backend", BACKEND), ("training", TRAINING)]
    )
    def test_vpc_cidr_is_required(self, chart: str, values: dict[str, str]) -> None:
        # Without it the 443 rules would admit every in-VPC listener.
        err = render_error(chart, {**values, "networkPolicy.vpcCidr": ""})
        assert "networkPolicy.vpcCidr is required" in err

    @pytest.mark.parametrize(
        ("chart", "values"),
        [
            ("backend", BACKEND),
            ("frontend", PUBLIC),
            ("frontend", PORT_FORWARD),
            ("training", TRAINING),
        ],
    )
    def test_disabling_policies_needs_an_acknowledgement(
        self, chart: str, values: dict[str, str]
    ) -> None:
        off = {**values, "networkPolicy.enabled": "false"}
        assert "acknowledgeUnprotectedApi" in render_error(chart, off)
        docs = render(chart, {**off, "networkPolicy.acknowledgeUnprotectedApi": "true"})
        assert of_kind(docs, "NetworkPolicy") == {}


def test_spa_sends_the_header_the_alb_requires() -> None:
    """The header name and value live in two places; they must agree."""
    helpers = (CHARTS / "frontend" / "templates" / "_helpers.tpl").read_text()
    name = re.search(r'define "pcd.clientHeaderName" -}}([^{]+){{', helpers)
    value = re.search(r'define "pcd.clientHeaderValue" -}}([^{]+){{', helpers)
    assert name and value
    http_ts = (ROOT / "frontend" / "src" / "services" / "http.ts").read_text()
    assert f"CLIENT_HEADER = '{name.group(1)}'" in http_ts
    assert f"CLIENT_HEADER_VALUE = '{value.group(1)}'" in http_ts


class TestNoncommercialWeightsOptIn:
    """The pretrained checkpoints are non-commercially licensed: off unless opted in."""

    def test_backend_is_off_by_default(self, backend_public: list[dict]) -> None:
        data = of_kind(backend_public, "ConfigMap")["backend-config"]["data"]
        assert data["ALLOW_NONCOMMERCIAL_WEIGHTS"] == "0"

    def test_backend_opt_in(self) -> None:
        docs = render(
            "backend", {**BACKEND, "models.allowNoncommercialWeights": "true"}
        )
        data = of_kind(docs, "ConfigMap")["backend-config"]["data"]
        assert data["ALLOW_NONCOMMERCIAL_WEIGHTS"] == "1"

    @staticmethod
    def _training_env(values: dict[str, str]) -> dict[str, str]:
        (job,) = of_kind(render("training", values), "Job").values()
        (container,) = job["spec"]["template"]["spec"]["containers"]
        return {e["name"]: e.get("value") for e in container["env"]}

    def test_training_is_off_by_default(self) -> None:
        assert self._training_env(TRAINING)["ALLOW_NONCOMMERCIAL_WEIGHTS"] == "0"

    def test_training_opt_in(self) -> None:
        values = {**TRAINING, "training.allowNoncommercialWeights": "true"}
        assert self._training_env(values)["ALLOW_NONCOMMERCIAL_WEIGHTS"] == "1"
