"""Tests for the ALB claims verifier (api/identity.py, threat model T31).

tests/_identity.py builds the tokens the way the Elastic Load Balancing guide
describes the ALB's x-amzn-oidc-data header.
"""

import hashlib
import hmac
import json
import threading
import time

import jwt
import pytest
import urllib3
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from pointcloud_detect.api import identity
from pointcloud_detect.api.identity import (
    MAX_KEY_BYTES,
    AlbIdentityConfig,
    AlbKeyCache,
    IdentityError,
    VerifierUnavailableError,
    fetch_key_pem,
    verify_alb_claims,
)
from tests._identity import (
    ALB_ARN,
    ALB_KEY,
    ALICE,
    CLIENT_ID,
    ISSUER,
    KID,
    POOL_ID,
    Keys,
    alb_token,
)
from tests._identity import b64 as _b64
from tests._identity import pem_of as _pem

SUB = ALICE
CONFIG = AlbIdentityConfig.build(ALB_ARN, POOL_ID, CLIENT_ID)


@pytest.fixture
def alb_key() -> ec.EllipticCurvePrivateKey:
    return ALB_KEY


@pytest.fixture
def keys() -> Keys:
    return Keys()


class TestAccepts:
    def test_an_alb_shaped_token_with_padding(self, alb_key, keys: Keys) -> None:
        token = alb_token(alb_key)
        assert "=" in token  # the padding the guide says the ALB keeps
        assert verify_alb_claims(token, CONFIG, keys) == SUB

    def test_the_key_comes_from_the_regional_endpoint(
        self, alb_key, keys: Keys
    ) -> None:
        verify_alb_claims(alb_token(alb_key), CONFIG, keys)
        assert keys.urls == [
            f"https://public-keys.auth.elb.us-east-1.amazonaws.com/{KID}"
        ]

    def test_an_unpadded_token(self, alb_key, keys: Keys) -> None:
        token = jwt.encode(
            {"sub": SUB, "exp": int(time.time()) + 60, "iss": ISSUER},
            alb_key,
            algorithm="ES256",
            headers={"kid": KID, "signer": ALB_ARN, "client": CLIENT_ID},
        )
        assert verify_alb_claims(token, CONFIG, keys) == SUB

    def test_expiry_within_the_leeway(self, alb_key, keys: Keys) -> None:
        token = alb_token(alb_key, claims={"exp": int(time.time()) - 10})
        assert verify_alb_claims(token, CONFIG, keys) == SUB


class TestRejectsBeforeFetchingAKey:
    """A token that names the wrong ALB, client or key id costs no outbound request."""

    @pytest.mark.parametrize(
        "header",
        [
            {"signer": ALB_ARN.replace("50dc6c495c0c9188", "0123456789abcdef")},
            {"signer": None},
            {"client": "anotherclient"},
            {"client": None},
            {"kid": "../../evil"},
            {"kid": "https://attacker.example/key"},
            {"kid": None},
            {"kid": 7},
            {"alg": "ES384"},
            {"alg": "HS256"},
            {"alg": "none"},
        ],
        ids=lambda h: f"{next(iter(h))}={next(iter(h.values()))}",
    )
    def test_header(self, alb_key, keys: Keys, header: dict) -> None:
        with pytest.raises(IdentityError):
            verify_alb_claims(alb_token(alb_key, header=header), CONFIG, keys)
        assert keys.urls == []

    @pytest.mark.parametrize("token", ["", "garbage", "a.b", "a.b.c.d", "..."])
    def test_not_a_jwt(self, keys: Keys, token: str) -> None:
        with pytest.raises(IdentityError):
            verify_alb_claims(token, CONFIG, keys)
        assert keys.urls == []

    def test_unsigned_token(self, keys: Keys) -> None:
        """alg "none" with an empty signature, built by hand as an attacker would."""
        header = {"alg": "none", "kid": KID, "signer": ALB_ARN, "client": CLIENT_ID}
        claims = {"sub": SUB, "exp": int(time.time()) + 60, "iss": ISSUER}
        token = (
            f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}."
        )
        with pytest.raises(IdentityError):
            verify_alb_claims(token, CONFIG, keys)
        assert keys.urls == []


class TestRejectsAfterTheSignatureCheck:
    def test_hmac_keyed_with_the_public_key(self, alb_key, keys: Keys) -> None:
        """The classic confusion: HS256 with the published key as the secret."""
        header = {"kid": KID, "signer": ALB_ARN, "client": CLIENT_ID, "alg": "HS256"}
        claims = {"sub": SUB, "exp": int(time.time()) + 60, "iss": ISSUER}
        signing_input = (
            f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
        )
        mac = hmac.new(_pem(alb_key), signing_input.encode(), hashlib.sha256).digest()
        token = f"{signing_input}.{_b64(mac)}"
        with pytest.raises(IdentityError):
            verify_alb_claims(token, CONFIG, keys)
        # Relabelled ES256, the same bytes are no ECDSA signature.
        relabelled = token.replace(
            signing_input.split(".")[0],
            _b64(json.dumps({**header, "alg": "ES256"}).encode()),
        )
        with pytest.raises(IdentityError):
            verify_alb_claims(relabelled, CONFIG, keys)

    def test_signed_by_another_key(self, keys: Keys) -> None:
        attacker = ec.generate_private_key(ec.SECP256R1())
        with pytest.raises(IdentityError):
            verify_alb_claims(alb_token(attacker), CONFIG, keys)

    def test_payload_changed_after_signing(self, alb_key, keys: Keys) -> None:
        head, _, sig = alb_token(alb_key).split(".")
        other = "11111111-2222-4333-8444-555555555555"
        claims = {"sub": other, "exp": int(time.time()) + 60, "iss": ISSUER}
        forged = f"{head}.{_b64(json.dumps(claims).encode())}.{sig}"
        with pytest.raises(IdentityError):
            verify_alb_claims(forged, CONFIG, keys)

    def test_expired(self, alb_key, keys: Keys) -> None:
        token = alb_token(alb_key, claims={"exp": int(time.time()) - 120})
        with pytest.raises(IdentityError):
            verify_alb_claims(token, CONFIG, keys)

    @pytest.mark.parametrize(
        "iss",
        [
            "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Other",
            "https://attacker.example",
            "",
        ],
    )
    def test_another_issuer(self, alb_key, keys: Keys, iss: str) -> None:
        with pytest.raises(IdentityError):
            verify_alb_claims(alb_token(alb_key, claims={"iss": iss}), CONFIG, keys)

    @pytest.mark.parametrize("claim", ["exp", "iss", "sub"])
    def test_a_required_claim_missing(self, alb_key, keys: Keys, claim: str) -> None:
        with pytest.raises(IdentityError):
            verify_alb_claims(alb_token(alb_key, drop=(claim,)), CONFIG, keys)

    @pytest.mark.parametrize("sub", ["admin", "../x", "", 42, SUB + "/x", None])
    def test_a_user_id_that_is_not_a_uuid(self, alb_key, keys: Keys, sub) -> None:
        with pytest.raises(IdentityError):
            verify_alb_claims(alb_token(alb_key, claims={"sub": sub}), CONFIG, keys)


class TestKeyCache:
    def test_a_key_is_fetched_once(self, alb_key, keys: Keys) -> None:
        token = alb_token(alb_key)
        for _ in range(3):
            verify_alb_claims(token, CONFIG, keys)
        assert len(keys.urls) == 1

    def test_bounded_least_recently_used(self, alb_key) -> None:
        cache = Keys(_pem(alb_key))
        cache._max_keys = 2
        kids = [f"00000000-0000-4000-8000-00000000000{i}" for i in range(3)]
        for kid in kids:
            cache.get(kid)
        cache.get(kids[0])  # evicted by the third, so fetched again
        assert cache.urls[-1].endswith(kids[0])
        assert len(cache.urls) == 4

    def test_a_failed_fetch_is_not_retried_at_once(self) -> None:
        now = [1000.0]
        calls: list[str] = []

        def fail(url: str) -> bytes:
            calls.append(url)
            raise OSError("unreachable")

        cache = AlbKeyCache("us-east-1", fetch=fail, clock=lambda: now[0])
        for _ in range(3):
            with pytest.raises(VerifierUnavailableError):
                cache.get(KID)
        assert len(calls) == 1
        now[0] += 61
        with pytest.raises(VerifierUnavailableError):
            cache.get(KID)
        assert len(calls) == 2

    def test_an_unavailable_key_is_not_an_identity_failure(self, alb_key) -> None:
        """No key means the token cannot be checked: 503, never 401 or served."""

        def fail(url: str) -> bytes:
            raise OSError("unreachable")

        with pytest.raises(VerifierUnavailableError):
            verify_alb_claims(
                alb_token(alb_key), CONFIG, AlbKeyCache("us-east-1", fetch=fail)
            )

    def test_new_key_ids_are_fetched_at_most_once_a_second(self, alb_key) -> None:
        """Tokens naming made-up key ids cannot make the API fetch on every request."""
        now = [1000.0]
        cache = Keys(_pem(alb_key))
        cache._min_fetch_interval_s = 1.0
        cache._clock = lambda: now[0]
        cache.get("00000000-0000-4000-8000-000000000001")
        with pytest.raises(VerifierUnavailableError):
            cache.get("00000000-0000-4000-8000-000000000002")
        assert len(cache.urls) == 1
        now[0] += 1.0
        cache.get("00000000-0000-4000-8000-000000000002")
        assert len(cache.urls) == 2
        # A cached key is served whatever the interval.
        cache.get("00000000-0000-4000-8000-000000000001")
        assert len(cache.urls) == 2

    def test_a_slow_fetch_holds_up_only_new_keys(self, alb_key) -> None:
        pem = _pem(alb_key)
        started, release = threading.Event(), threading.Event()
        known = "00000000-0000-4000-8000-00000000000a"

        def fetch(url: str) -> bytes:
            if not url.endswith(known):
                started.set()
                release.wait(5)
            return pem

        cache = AlbKeyCache(
            "us-east-1", fetch=fetch, min_fetch_interval_s=0, fetch_wait_s=0.1
        )
        cache.get(known)
        slow = threading.Thread(
            target=cache.get, args=("00000000-0000-4000-8000-00000000000b",)
        )
        slow.start()
        assert started.wait(5)
        try:
            # Another new key gives up quickly rather than queueing behind it...
            with pytest.raises(VerifierUnavailableError):
                cache.get("00000000-0000-4000-8000-00000000000c")
            # ...and a cached key does not wait at all.
            assert cache.get(known) is not None
        finally:
            release.set()
            slow.join(5)

    @pytest.mark.parametrize(
        "pem",
        [
            b"not a key",
            _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048)),
            _pem(ec.generate_private_key(ec.SECP384R1())),
        ],
        ids=["garbage", "rsa", "p384"],
    )
    def test_only_a_p256_key_is_accepted(self, pem: bytes) -> None:
        with pytest.raises(VerifierUnavailableError):
            AlbKeyCache("us-east-1", fetch=lambda url: pem).get(KID)


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body
        self.released = False

    def read(self, amt: int) -> bytes:
        return self._body[:amt]

    def release_conn(self) -> None:
        self.released = True


class _Pool:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.kwargs: dict = {}

    def request(self, method: str, url: str, **kwargs) -> _Response:
        self.kwargs = {"method": method, "url": url, **kwargs}
        return self.response


class TestFetch:
    def _pool(self, monkeypatch: pytest.MonkeyPatch, status: int, body: bytes) -> _Pool:
        pool = _Pool(_Response(status, body))
        monkeypatch.setattr(identity, "_http_pool", lambda: pool)
        return pool

    def test_no_redirects_or_retries_and_a_timeout(self, monkeypatch) -> None:
        pool = self._pool(monkeypatch, 200, b"pem")
        assert fetch_key_pem("https://example.invalid/k") == b"pem"
        assert pool.kwargs["redirect"] is False
        retries = pool.kwargs["retries"]
        assert isinstance(retries, urllib3.Retry)
        assert (retries.connect, retries.read, retries.redirect, retries.status) == (
            1,
            0,
            0,
            0,
        )
        assert pool.kwargs["timeout"] is identity.KEY_FETCH_TIMEOUT
        assert pool.response.released

    def test_an_error_status(self, monkeypatch) -> None:
        pool = self._pool(monkeypatch, 403, b"denied")
        with pytest.raises(VerifierUnavailableError):
            fetch_key_pem("https://example.invalid/k")
        assert pool.response.released

    def test_an_oversized_answer(self, monkeypatch) -> None:
        self._pool(monkeypatch, 200, b"x" * (MAX_KEY_BYTES + 1))
        with pytest.raises(VerifierUnavailableError):
            fetch_key_pem("https://example.invalid/k")


class TestConfig:
    def test_issuer_and_region(self) -> None:
        assert CONFIG.issuer == ISSUER
        assert CONFIG.key_region == "us-east-1"

    @pytest.mark.parametrize(
        "arn",
        [
            "",
            "arn:aws:elasticloadbalancing:us-east-1:123456789012:"
            "loadbalancer/net/nlb/50dc6c495c0c9188",
            "arn:aws-cn:elasticloadbalancing:cn-north-1:123456789012:"
            "loadbalancer/app/a/50dc6c495c0c9188",
            ALB_ARN + "/extra",
            " " + ALB_ARN,
        ],
    )
    def test_not_an_alb_arn(self, arn: str) -> None:
        with pytest.raises(ValueError, match="ALB_ARN"):
            AlbIdentityConfig.build(arn, POOL_ID, CLIENT_ID)

    @pytest.mark.parametrize("pool", ["", "us-east-1", "AbCdEf123", "us-east-1_ab/c"])
    def test_not_a_pool_id(self, pool: str) -> None:
        with pytest.raises(ValueError, match="COGNITO_USER_POOL_ID"):
            AlbIdentityConfig.build(ALB_ARN, pool, CLIENT_ID)

    @pytest.mark.parametrize("client", ["", "a b", "x" * 129, "abc/def"])
    def test_not_a_client_id(self, client: str) -> None:
        with pytest.raises(ValueError, match="COGNITO_CLIENT_ID"):
            AlbIdentityConfig.build(ALB_ARN, POOL_ID, client)
