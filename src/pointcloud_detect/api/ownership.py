"""Who owns an object, and whether a caller may reach it (threat model T31).

api/identity.py works out who is calling; this module holds what the job store, the
routes and the worker need to know about ownership, without importing the verifier's
dependencies.

ACCESS_SCOPE decides what an owner means:

  per-user  each object records the Cognito user id (``sub``) of the user who created
            it, and only that user reaches it;
  shared    nothing is recorded or checked, and every caller reaches every object.
"""

import re
from dataclasses import dataclass
from typing import Final

from pointcloud_detect.api.models import INPUT_KEY_PREFIX

# Cognito user ids and the ALB's signing key ids are both UUIDs.
UUID_PATTERN: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


class AnyOwner:
    """The owner filter every record passes: shared mode, and the worker."""

    def __repr__(self) -> str:
        return "ANY_OWNER"


ANY_OWNER: Final = AnyOwner()

# Either a user id, which only that user's records pass, or ANY_OWNER.
OwnerFilter = str | AnyOwner


def owner_matches(record_owner: str | None, owner: OwnerFilter) -> bool:
    """Whether a record owned by ``record_owner`` passes the filter ``owner``.

    A record with no owner, written in shared mode or before owners were recorded,
    passes only ANY_OWNER: in per-user mode it belongs to no one.
    """
    if isinstance(owner, AnyOwner):
        return True
    return record_owner is not None and record_owner == owner


@dataclass(frozen=True)
class Principal:
    """The caller of one request.

    ``owner`` is the caller's Cognito user id in per-user mode. It is None in shared
    mode, where no identity is read and every caller reaches every object.
    """

    owner: str | None

    @property
    def owner_filter(self) -> OwnerFilter:
        """The filter that selects what this caller may reach."""
        return ANY_OWNER if self.owner is None else self.owner

    def can_access(self, record_owner: str | None) -> bool:
        return owner_matches(record_owner, self.owner_filter)


# The caller in shared mode.
SHARED: Final = Principal(owner=None)


def upload_key_prefix(owner: str | None) -> str:
    """Where a new upload goes: ``uploads/<owner>/``, or ``uploads/`` if shared."""
    return INPUT_KEY_PREFIX if owner is None else f"{INPUT_KEY_PREFIX}{owner}/"


def input_key_owner(key: str) -> str | None:
    """The user a per-user input key belongs to, or None for any other key.

    A per-user key is ``uploads/<user id>/<upload id>/<filename>``, both ids UUIDs,
    which is what the upload route issues in per-user mode. Anything else, such as a
    shared-mode ``uploads/<upload id>/<filename>`` or a file an operator staged by
    hand, belongs to no user. ``key`` is expected to have passed validate_input_key.
    """
    segments = key.split("/")
    if (
        len(segments) == 4
        and UUID_PATTERN.fullmatch(segments[1]) is not None
        and UUID_PATTERN.fullmatch(segments[2]) is not None
    ):
        return segments[1]
    return None
