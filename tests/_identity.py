"""Signed ALB claims for tests, and the settings per-user mode needs.

The tokens are built the way the Elastic Load Balancing guide describes the
x-amzn-oidc-data header: ES256 over base64url segments that keep their padding, with
kid, signer, client and exp in the header and the user's claims, exp and iss in the
payload.
"""

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from pointcloud_detect.api import identity
from pointcloud_detect.api.deps import get_settings
from pointcloud_detect.api.identity import ALB_CLAIMS_HEADER, AlbKeyCache

ALB_ARN = (
    "arn:aws:elasticloadbalancing:us-east-1:123456789012:"
    "loadbalancer/app/k8s-pointcloud-0a1b2c3d4e/50dc6c495c0c9188"
)
POOL_ID = "us-east-1_AbCdEf123"
CLIENT_ID = "1example23456789abcdefghij"
ISSUER = f"https://cognito-idp.us-east-1.amazonaws.com/{POOL_ID}"
KID = "6a8f1b2c-3d4e-4f50-8a1b-2c3d4e5f6a7b"
ALICE = "8a1f0c2e-5b6d-4e7f-9a0b-1c2d3e4f5a6b"
BOB = "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f"

# One signing key for the whole run, as an ALB has.
ALB_KEY = ec.generate_private_key(ec.SECP256R1())


def b64(raw: bytes) -> str:
    # urlsafe_b64encode keeps the "=" padding, as the ALB's tokens do.
    return base64.urlsafe_b64encode(raw).decode()


def pem_of(key: ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def alb_token(
    key: ec.EllipticCurvePrivateKey = ALB_KEY,
    header: dict | None = None,
    claims: dict | None = None,
    drop: tuple[str, ...] = (),
    sub: str = ALICE,
) -> str:
    """A token shaped like the ALB's x-amzn-oidc-data, signed with ``key``."""
    now = int(time.time())
    h = {
        "typ": "JWT",
        "kid": KID,
        "alg": "ES256",
        "iss": ISSUER,
        "client": CLIENT_ID,
        "signer": ALB_ARN,
        "exp": now + 120,
    }
    c = {
        "sub": sub,
        "email_verified": "true",
        "email": "user@example.com",
        "username": sub,
        "exp": now + 120,
        "iss": ISSUER,
    }
    h.update(header or {})
    c.update(claims or {})
    for name in drop:
        c.pop(name, None)
    signing_input = f"{b64(json.dumps(h).encode())}.{b64(json.dumps(c).encode())}"
    der = key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return f"{signing_input}.{b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def signed_in(sub: str) -> dict[str, str]:
    """The header the ALB adds for user ``sub``."""
    return {ALB_CLAIMS_HEADER: alb_token(sub=sub)}


class Keys(AlbKeyCache):
    """A key cache that serves ``pem`` for every fetch and records the fetches."""

    def __init__(self, pem: bytes = pem_of(ALB_KEY)) -> None:
        self.urls: list[str] = []

        def fetch(url: str) -> bytes:
            self.urls.append(url)
            return pem

        # No interval between fetches, so a test can fetch several keys in a row.
        super().__init__("us-east-1", fetch=fetch, min_fetch_interval_s=0)


def per_user(
    monkeypatch: pytest.MonkeyPatch, alb_arn: str = ALB_ARN, keys: Keys | None = None
) -> Keys:
    """Switch the API to per-user mode, as the chart configures it, with test keys."""
    monkeypatch.setenv("ACCESS_SCOPE", "per-user")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ALB_ARN", alb_arn)
    monkeypatch.setenv("COGNITO_USER_POOL_ID", POOL_ID)
    monkeypatch.setenv("COGNITO_CLIENT_ID", CLIENT_ID)
    cache = keys or Keys()
    monkeypatch.setattr(identity, "key_cache_for", lambda region: cache)
    get_settings.cache_clear()
    return cache
