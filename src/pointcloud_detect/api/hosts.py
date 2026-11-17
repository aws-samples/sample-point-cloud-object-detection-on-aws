"""Serve only the host names a deployment is reached by.

This is not authentication: it names no user. It closes DNS rebinding on the api
tunnel. While a user has `kubectl port-forward svc/api` open, a page they visit can
re-point its own name at 127.0.0.1 and so become same-origin with the tunnel, able to
read the responses. Its requests still carry its own name in Host, and that is refused
here. nginx applies the same rule to the frontend tunnel
(infrastructure/helm/frontend/templates/configmap-nginx.yaml).

The list comes from ALLOWED_HOSTS, which the backend chart always sets: the public
hostname in public mode, the api Service's in-cluster names, and localhost and
127.0.0.1 for the tunnels. When it is unset, as in the local modes and the tests,
nothing is checked. /health is exempt, because the kubelet and the ALB probe it by pod
IP.
"""

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

EXEMPT_PATHS = frozenset({"/health"})


def host_name(host_header: str) -> str:
    """The name in a Host header, lowercased and without its port."""
    name, sep, port = host_header.rpartition(":")
    return (name if sep and port.isdigit() else host_header).lower()


class HostAllowlistMiddleware:
    """Answer 421 to any request whose Host is not in ``allowed_hosts``."""

    def __init__(self, app: ASGIApp, allowed_hosts: list[str]) -> None:
        self.app = app
        self.allowed = frozenset(h.lower() for h in allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and self.allowed
            and scope["path"] not in EXEMPT_PATHS
            and host_name(Headers(scope=scope).get("host", "")) not in self.allowed
        ):
            # 421, as nginx answers on the frontend tunnel. The Host is not echoed.
            response = JSONResponse(
                {"detail": "Misdirected request: this API does not serve that host"},
                status_code=421,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
