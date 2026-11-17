"""Who is calling: the load balancer's signed user claims, verified (threat model T31).

In public mode the ALB signs every user in with Cognito, then forwards the request with
``x-amzn-oidc-data``: the user's claims as a JWT the ALB signs with ES256. The Elastic
Load Balancing guide ("User claims encoding and signature verification") says to verify
that signature with the public key the ALB publishes for the token's ``kid``, and to
check that the token's ``signer`` is this deployment's ALB, before authorizing on any
claim. That is what verify_alb_claims does. The ``sub`` of a verified token is the
caller, and api/ownership.py decides what they may reach.

The ALB also forwards ``x-amzn-oidc-identity`` and ``x-amzn-oidc-accesstoken``. Both are
unsigned, the guide says they cannot be verified, and anything the NetworkPolicy admits
could set them, so they are never read.

The checks mirror AWS's own verifier for ALB tokens (aws-jwt-verify, AlbJwtVerifier):
ES256 only, a UUID key id, the signer and the app client in the header, the issuer and
the expiry in the claims. Cheap header checks run first, so a token that names another
load balancer or a malformed key id never causes a key fetch.
"""

import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import jwt
import urllib3
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from fastapi import HTTPException, Request

from pointcloud_detect.api.deps import Settings, get_settings
from pointcloud_detect.api.ownership import SHARED, UUID_PATTERN, Principal

logger = logging.getLogger(__name__)

ACCESS_SCOPES: Final = ("per-user", "shared")

# The one header the ALB signs.
ALB_CLAIMS_HEADER: Final = "x-amzn-oidc-data"

# Clock difference allowed on the token's exp, in seconds.
LEEWAY_S: Final = 30

# An Application Load Balancer in the commercial partition, the one the guide gives a
# public-key endpoint for. The Region is taken from here, never from a token.
ALB_ARN_PATTERN: Final = re.compile(
    r"arn:aws:elasticloadbalancing:(?P<region>[a-z]{2}-[a-z]+-\d{1,2}):\d{12}:"
    r"loadbalancer/app/[A-Za-z0-9-]{1,32}/[0-9a-f]{16}"
)
USER_POOL_ID_PATTERN: Final = re.compile(r"(?P<region>[a-z]{2}-[a-z]+-\d{1,2})_\w+")
CLIENT_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9]{1,128}")

KEY_URL: Final = "https://public-keys.auth.elb.{region}.amazonaws.com/{kid}"
# A PEM-encoded P-256 public key is under 200 bytes.
MAX_KEY_BYTES: Final = 4096
KEY_FETCH_TIMEOUT: Final = urllib3.Timeout(connect=2.0, read=3.0)
# One more try on a failed connection, and none on anything else: a slow or erroring
# endpoint is answered 503 rather than waited on.
KEY_FETCH_RETRIES: Final = urllib3.Retry(
    total=1, connect=1, read=0, redirect=0, status=0, other=0
)


class IdentityError(Exception):
    """The request carries no identity that verifies. Answered 401."""


class VerifierUnavailableError(Exception):
    """An identity cannot be checked right now. Answered 503, never served."""


def alb_region(alb_arn: str) -> str:
    """The Region of an Application Load Balancer ARN; ValueError if it is not one."""
    arn = ALB_ARN_PATTERN.fullmatch(alb_arn)
    if arn is None:
        raise ValueError(
            f"ALB_ARN={alb_arn!r} is not an Application Load Balancer ARN "
            "(arn:aws:elasticloadbalancing:<region>:<account>:loadbalancer/app/...)."
        )
    return arn["region"]


def user_pool_issuer(user_pool_id: str) -> str:
    """The ``iss`` of a user pool's tokens; ValueError if the ID is not a pool ID."""
    pool = USER_POOL_ID_PATTERN.fullmatch(user_pool_id)
    if pool is None:
        raise ValueError(
            f"COGNITO_USER_POOL_ID={user_pool_id!r} is not a user pool ID "
            "(<region>_<id>; terraform output cognito, user_pool_id)."
        )
    return f"https://cognito-idp.{pool['region']}.amazonaws.com/{user_pool_id}"


def check_client_id(client_id: str) -> str:
    if CLIENT_ID_PATTERN.fullmatch(client_id) is None:
        raise ValueError(
            "COGNITO_CLIENT_ID is not an app client ID "
            "(terraform output cognito, client_id)."
        )
    return client_id


@dataclass(frozen=True)
class AlbIdentityConfig:
    """What a token must name: this deployment's ALB, app client and user pool."""

    alb_arn: str
    client_id: str
    issuer: str
    key_region: str

    @classmethod
    def build(
        cls, alb_arn: str, user_pool_id: str, client_id: str
    ) -> "AlbIdentityConfig":
        """Validate the settings; raise ValueError naming the one that is wrong."""
        return cls(
            alb_arn=alb_arn,
            client_id=check_client_id(client_id),
            issuer=user_pool_issuer(user_pool_id),
            key_region=alb_region(alb_arn),
        )


def _http_pool() -> urllib3.PoolManager:
    # Verified HTTPS: urllib3 2 checks the certificate and hostname by default.
    return urllib3.PoolManager()


def fetch_key_pem(url: str) -> bytes:
    """GET ``url``: no redirects, one connection retry, short timeouts, a size cap."""
    resp = _http_pool().request(
        "GET",
        url,
        redirect=False,
        retries=KEY_FETCH_RETRIES,
        preload_content=False,
        timeout=KEY_FETCH_TIMEOUT,
    )
    try:
        if resp.status != 200:
            raise VerifierUnavailableError(f"key endpoint answered HTTP {resp.status}")
        body = resp.read(MAX_KEY_BYTES + 1)
    finally:
        resp.release_conn()
    if len(body) > MAX_KEY_BYTES:
        raise VerifierUnavailableError("key endpoint answered more than a public key")
    return body


def load_alb_key(pem: bytes) -> ec.EllipticCurvePublicKey:
    """Parse a key the ALB published; only an EC P-256 public key is accepted."""
    try:
        key = load_pem_public_key(pem)
    except ValueError as e:
        raise VerifierUnavailableError("key endpoint answered no PEM public key") from e
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise VerifierUnavailableError("key endpoint answered a key that is not EC")
    if not isinstance(key.curve, ec.SECP256R1):
        raise VerifierUnavailableError("key endpoint answered a key that is not P-256")
    return key


class AlbKeyCache:
    """The ALB's public keys for one Region, by key id.

    A key is fetched once and kept, up to ``max_keys`` (least recently used first out).
    Requests for a cached key never wait on a fetch. Fetches are bounded so that tokens
    naming key ids the ALB never used cannot tie up the API: one fetch at a time, a
    request waits at most ``fetch_wait_s`` for its turn, at most one fetch starts per
    ``min_fetch_interval_s``, and a key id whose fetch failed is not fetched again for
    ``retry_after_s``. A request refused by any of these is answered 503; the next one
    for a real key is served once its key is fetched.
    """

    def __init__(
        self,
        region: str,
        fetch: Callable[[str], bytes] = fetch_key_pem,
        max_keys: int = 8,
        retry_after_s: float = 10.0,
        min_fetch_interval_s: float = 1.0,
        fetch_wait_s: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.region = region
        self._fetch = fetch
        self._max_keys = max_keys
        self._retry_after_s = retry_after_s
        self._min_fetch_interval_s = min_fetch_interval_s
        self._fetch_wait_s = fetch_wait_s
        self._clock = clock
        self._keys: OrderedDict[str, ec.EllipticCurvePublicKey] = OrderedDict()
        self._failed: OrderedDict[str, float] = OrderedDict()
        self._last_fetch_at: float | None = None
        self._lock = threading.Lock()
        # One fetch at a time, so a burst of requests for a new key makes one call.
        self._fetch_lock = threading.Lock()

    def _cached(self, kid: str) -> ec.EllipticCurvePublicKey | None:
        with self._lock:
            key = self._keys.get(kid)
            if key is not None:
                self._keys.move_to_end(kid)
                return key
            failed_at = self._failed.get(kid)
            if (
                failed_at is not None
                and self._clock() - failed_at < self._retry_after_s
            ):
                raise VerifierUnavailableError("the signing key could not be fetched")
            return None

    def get(self, kid: str) -> ec.EllipticCurvePublicKey:
        """The public key for ``kid``, which the caller has checked is a UUID."""
        key = self._cached(kid)
        if key is not None:
            return key
        if not self._fetch_lock.acquire(timeout=self._fetch_wait_s):
            raise VerifierUnavailableError("the signing key could not be fetched")
        try:
            key = self._cached(kid)
            if key is not None:
                return key
            now = self._clock()
            if (
                self._last_fetch_at is not None
                and now - self._last_fetch_at < self._min_fetch_interval_s
            ):
                raise VerifierUnavailableError("the signing key could not be fetched")
            self._last_fetch_at = now
            try:
                key = load_alb_key(
                    self._fetch(KEY_URL.format(region=self.region, kid=kid))
                )
            except Exception as e:
                with self._lock:
                    self._failed[kid] = self._clock()
                    while len(self._failed) > 64:
                        self._failed.popitem(last=False)
                if isinstance(e, VerifierUnavailableError):
                    raise
                raise VerifierUnavailableError(
                    "the signing key could not be fetched"
                ) from e
            with self._lock:
                self._keys[kid] = key
                self._failed.pop(kid, None)
                while len(self._keys) > self._max_keys:
                    self._keys.popitem(last=False)
            return key
        finally:
            self._fetch_lock.release()


def verify_alb_claims(token: str, config: AlbIdentityConfig, keys: AlbKeyCache) -> str:
    """The Cognito user id of a verified ALB claims token.

    Raises IdentityError when the token does not verify, and VerifierUnavailableError
    when its signing key cannot be obtained.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise IdentityError("the claims are not a JWT") from e
    if header.get("alg") != "ES256":
        raise IdentityError("the claims are not signed with ES256")
    kid = header.get("kid")
    if not isinstance(kid, str) or UUID_PATTERN.fullmatch(kid) is None:
        raise IdentityError("the claims name no valid key id")
    if header.get("signer") != config.alb_arn:
        raise IdentityError("the claims were not signed by this deployment's ALB")
    if header.get("client") != config.client_id:
        raise IdentityError("the claims are for another app client")
    key = keys.get(kid)
    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=["ES256"],
            issuer=config.issuer,
            leeway=LEEWAY_S,
            options={"require": ["exp", "iss", "sub"]},
        )
    except jwt.PyJWTError as e:
        raise IdentityError(f"the claims did not verify ({type(e).__name__})") from e
    sub = claims["sub"]
    if not isinstance(sub, str) or UUID_PATTERN.fullmatch(sub) is None:
        raise IdentityError("the claims name no valid user id")
    return sub


def identity_config(settings: Settings) -> AlbIdentityConfig | None:
    """The per-user verification settings, or None while ALB_ARN is unset.

    The ALB must be in the API's own Region (AWS_REGION): its keys are fetched from
    that Region's endpoint, and an ARN from anywhere else is a misconfiguration.
    """
    if not settings.alb_arn.strip():
        return None
    config = AlbIdentityConfig.build(
        settings.alb_arn.strip(),
        settings.cognito_user_pool_id.strip(),
        settings.cognito_client_id.strip(),
    )
    if config.key_region != settings.aws_region.strip():
        raise ValueError(
            f"ALB_ARN names an ALB in {config.key_region}, but the API runs in "
            f"{settings.aws_region.strip()} (AWS_REGION)."
        )
    return config


def require_access_settings(settings: Settings, job_store_backend: str) -> None:
    """Raise, naming what is wrong, unless ACCESS_SCOPE and its settings are usable.

    Called when the AWS-mode API starts (api/app.py), beside require_buckets. There is
    no default scope: guessing "shared" would show every user everyone's scans, and
    guessing "per-user" would lock out a deployment with no ALB. ALB_ARN may still be
    unset, because the ALB exists only after the first deploy; until deploy.sh sets
    it, every /api request is answered 503.
    """
    scope = settings.access_scope.strip()
    if scope not in ACCESS_SCOPES:
        raise RuntimeError(
            f"ACCESS_SCOPE={scope!r} is not valid; set it to per-user or shared. "
            "per-user (the default for public mode in the backend chart) lets each "
            "signed-in user reach only their own jobs; shared lets every caller reach "
            "every job."
        )
    if scope == "shared":
        logger.info("ACCESS_SCOPE=shared: every caller reaches every job.")
        return
    if job_store_backend != "dynamodb":
        raise RuntimeError(
            "ACCESS_SCOPE=per-user needs JOB_STORE_BACKEND=dynamodb. It exists for the "
            "public-mode deployment, whose ALB signs users in."
        )
    missing = [
        env
        for env, value in (
            ("COGNITO_USER_POOL_ID", settings.cognito_user_pool_id),
            ("COGNITO_CLIENT_ID", settings.cognito_client_id),
        )
        if not value.strip()
    ]
    if missing:
        raise RuntimeError(
            f"ACCESS_SCOPE=per-user needs {', '.join(missing)} "
            "(terraform output cognito). deploy.sh passes them."
        )
    try:
        user_pool_issuer(settings.cognito_user_pool_id.strip())
        check_client_id(settings.cognito_client_id.strip())
        config = identity_config(settings)
    except ValueError as e:
        raise RuntimeError(str(e)) from e
    if config is None:
        logger.warning(
            "ACCESS_SCOPE=per-user and ALB_ARN is unset: every /api request is "
            "answered 503 until deploy.sh sets ALB_ARN, once the load balancer exists."
        )


_key_caches: dict[str, AlbKeyCache] = {}
_key_caches_lock = threading.Lock()


def key_cache_for(region: str) -> AlbKeyCache:
    with _key_caches_lock:
        cache = _key_caches.get(region)
        if cache is None:
            cache = _key_caches[region] = AlbKeyCache(region)
        return cache


def current_principal(request: Request) -> Principal:
    """Who is calling. Every /api router runs this first (api/app.py).

    A plain ``def``, so FastAPI runs it in its threadpool: a key fetch blocks.
    """
    settings = get_settings()
    scope = settings.access_scope.strip()
    if scope == "shared":
        return SHARED
    if scope != "per-user":
        # Startup refuses this (require_access_settings). Refused, never served shared.
        raise HTTPException(503, "The API's access scope is not configured.")
    try:
        config = identity_config(settings)
    except ValueError:
        logger.exception("The per-user sign-in settings are invalid")
        config = None
    if config is None:
        raise HTTPException(503, "Sign-in verification is not configured yet.")
    values = request.headers.getlist(ALB_CLAIMS_HEADER)
    if len(values) != 1:
        logger.warning(
            "Refused a request with %d %s headers", len(values), ALB_CLAIMS_HEADER
        )
        raise HTTPException(401, "Sign-in required.")
    try:
        owner = verify_alb_claims(values[0], config, key_cache_for(config.key_region))
    except IdentityError as e:
        logger.warning("Refused a request whose identity did not verify: %s", e)
        raise HTTPException(401, "Sign-in required.") from None
    except VerifierUnavailableError as e:
        logger.error("Could not verify a request's identity: %s", e)
        raise HTTPException(
            503, "Sign-in could not be verified just now; try again."
        ) from None
    return Principal(owner=owner)
