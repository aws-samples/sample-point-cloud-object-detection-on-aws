"""Tests for DynamoJobStore's read-modify-write path.

``update_status`` replaces the whole item rather than issuing a partial
``UpdateExpression``, which makes two things easy to get wrong and impossible to
notice at runtime:

1. The TTL. Rebuilding the item recomputes ``expires_at`` unless the existing value is
   carried forward, which restarts the retention clock on every status change. The rows
   that would survive longest are the ones most worth collecting: nothing marks a job
   failed in the cluster path, so a permanently failing job is redelivered by SQS
   indefinitely and each redelivery rewrites "processing".
2. The version guard. A lost update leaves a plausible-looking record that is simply
   missing one writer's field, so nothing raises and nothing looks wrong.

There is no moto or botocore Stubber in this suite, so these use a minimal fake table
that implements only what the store calls: ``get_item``, ``put_item`` with the version
``ConditionExpression``, ``delete_item``, ``scan``, and the exceptions namespace.

The last two classes cover deletion (T60) and the read-side expiry filter: DynamoDB
deletes expired items lazily, so without it records past their retention were still
served.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from pointcloud_detect.api.models import JobConfigRequest, JobResponse, JobStatus
from pointcloud_detect.api.store import DynamoJobStore

DAY = 86400


class ConditionalCheckFailedException(Exception):
    pass


class _Exceptions:
    ConditionalCheckFailedException = ConditionalCheckFailedException


class _Client:
    exceptions = _Exceptions()


class _Meta:
    client = _Client()


class FakeTable:
    """Just enough DynamoDB to exercise the store's conditional put.

    Numbers come back as ``Decimal`` from the real resource API, so they do here too —
    that is what makes the ``int()`` coercion in the store load-bearing rather than
    decorative.
    """

    meta = _Meta()

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.put_calls: list[dict[str, Any]] = []
        # Fires once, immediately before the next put, to simulate another writer
        # landing between the store's read and its conditional write.
        self.on_before_put: Callable[[FakeTable], None] | None = None

    def get_item(self, Key: dict) -> dict:  # noqa: N803 - boto3 casing
        item = self.items.get(Key["job_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(
        self,
        Item: dict,  # noqa: N803
        ConditionExpression: str | None = None,  # noqa: N803
        ExpressionAttributeValues: dict | None = None,  # noqa: N803
    ) -> None:
        if self.on_before_put is not None:
            hook, self.on_before_put = self.on_before_put, None
            hook(self)

        if ConditionExpression and ExpressionAttributeValues:
            expected = ExpressionAttributeValues[":v"]
            current = self.items.get(Item["job_id"])
            if current is not None and int(current.get("version", 0)) != int(expected):
                raise ConditionalCheckFailedException("version mismatch")

        stored = dict(Item)
        for k, v in stored.items():
            if isinstance(v, int) and k in ("version", "expires_at"):
                stored[k] = Decimal(v)
        self.items[Item["job_id"]] = stored
        self.put_calls.append(dict(Item))

    def delete_item(self, Key: dict) -> dict:  # noqa: N803
        self.items.pop(Key["job_id"], None)
        return {}

    def scan(self, **_kwargs: Any) -> dict:
        return {"Items": [dict(i) for i in self.items.values()]}


@pytest.fixture
def store() -> DynamoJobStore:
    # __new__ rather than __init__: the real constructor builds a boto3 resource, and
    # the state it sets up is exactly the two attributes replaced below.
    s = DynamoJobStore.__new__(DynamoJobStore)
    s._table = FakeTable()  # pyright: ignore[reportAttributeAccessIssue]
    s._ttl_days = 90
    return s


def _job(job_id: str = "j1") -> JobResponse:
    return JobResponse(
        job_id=job_id,
        filename="scene.las",
        s3_input_key=f"input/{job_id}/scene.las",
        config=JobConfigRequest(),
        status=JobStatus.queued,
        created_at="2026-01-01T00:00:00Z",  # pyright: ignore[reportArgumentType]
    )


def _table(store: DynamoJobStore) -> FakeTable:
    return store._table  # pyright: ignore[reportAttributeAccessIssue]


class TestTtlIsSetOnCreate:
    def test_create_sets_expiry_about_ninety_days_out(
        self, store: DynamoJobStore
    ) -> None:
        before = int(time.time())
        store.create(_job())
        expiry = int(_table(store).items["j1"]["expires_at"])
        assert before + 90 * DAY <= expiry <= before + 90 * DAY + 5

    def test_ttl_days_zero_writes_no_expiry(self, store: DynamoJobStore) -> None:
        store._ttl_days = 0  # pyright: ignore[reportAttributeAccessIssue]
        store.create(_job())
        assert "expires_at" not in _table(store).items["j1"]


class TestTtlSurvivesUpdates:
    def test_update_does_not_push_the_expiry_forward(
        self, store: DynamoJobStore
    ) -> None:
        store.create(_job())
        original = int(_table(store).items["j1"]["expires_at"])

        # Backdate creation so a recomputed expiry would be obviously different.
        _table(store).items["j1"]["expires_at"] = Decimal(original - 60 * DAY)
        backdated = original - 60 * DAY

        store.update_status("j1", JobStatus.processing)
        assert int(_table(store).items["j1"]["expires_at"]) == backdated

    def test_repeated_updates_never_extend_retention(
        self, store: DynamoJobStore
    ) -> None:
        # The failure mode: nothing marks jobs failed in the cluster, so SQS redelivers
        # and rewrites "processing" indefinitely. Retention must not follow.
        #
        # The expiry is backdated first. Without that, every rewrite recomputes to the
        # same int(time.time()) within one test run, so a regression would land on an
        # identical value and this would pass while the bug was present.
        store.create(_job())
        backdated = int(_table(store).items["j1"]["expires_at"]) - 60 * DAY
        _table(store).items["j1"]["expires_at"] = Decimal(backdated)

        for _ in range(5):
            store.update_status("j1", JobStatus.processing)
        assert int(_table(store).items["j1"]["expires_at"]) == backdated

    def test_item_without_expiry_gains_one_on_update(
        self, store: DynamoJobStore
    ) -> None:
        # Records written before the TTL attribute existed should start expiring once
        # they are next touched, rather than staying immortal.
        store.create(_job())
        del _table(store).items["j1"]["expires_at"]
        before = int(time.time())
        store.update_status("j1", JobStatus.processing)
        expiry = int(_table(store).items["j1"]["expires_at"])
        assert before + 90 * DAY <= expiry <= before + 90 * DAY + 5


class TestOptimisticConcurrency:
    def test_version_increments_on_each_update(self, store: DynamoJobStore) -> None:
        store.create(_job())
        assert int(_table(store).items["j1"]["version"]) == 0
        store.update_status("j1", JobStatus.processing)
        assert int(_table(store).items["j1"]["version"]) == 1

    def test_contended_write_retries_and_still_applies(
        self, store: DynamoJobStore
    ) -> None:
        store.create(_job())

        def competing_write(t: FakeTable) -> None:
            # Another writer lands between our read and our write, bumping version.
            record = json.loads(t.items["j1"]["record"])
            record["tile_count"] = 7
            t.items["j1"] = {
                **t.items["j1"],
                "record": json.dumps(record),
                "version": Decimal(1),
            }

        _table(store).on_before_put = competing_write
        store.update_status("j1", JobStatus.complete, detection_count=3)

        final = json.loads(_table(store).items["j1"]["record"])
        assert final["status"] == "complete"
        assert final["detection_count"] == 3
        # The competing writer's field survived rather than being clobbered.
        assert final["tile_count"] == 7

    def test_unknown_job_id_is_a_noop(self, store: DynamoJobStore) -> None:
        store.update_status("missing", JobStatus.complete)
        assert _table(store).put_calls == []

    def test_gives_up_after_bounded_attempts(self, store: DynamoJobStore) -> None:
        store.create(_job())

        # Every put collides, so the loop must terminate rather than spin.
        def always_collide(t: FakeTable) -> None:
            t.items["j1"] = {
                **t.items["j1"],
                "version": Decimal(int(t.items["j1"]["version"]) + 1),
            }
            t.on_before_put = always_collide

        _table(store).on_before_put = always_collide
        store.update_status("j1", JobStatus.complete)

        record = json.loads(_table(store).items["j1"]["record"])
        assert record["status"] == "queued"


class TestDelete:
    def test_delete_removes_the_item(self, store: DynamoJobStore) -> None:
        store.create(_job("j1"))
        store.create(_job("j2"))
        store.delete("j1")
        assert "j1" not in _table(store).items
        assert store.get("j1") is None
        assert [j.job_id for j in store.list()] == ["j2"]

    def test_deleting_an_unknown_id_is_a_noop(self, store: DynamoJobStore) -> None:
        store.create(_job("j1"))
        store.delete("missing")
        assert store.get("j1") is not None


class TestExpiredRecordsAreNotServed:
    """TTL deletion is lazy; reads must not serve what retention already ended."""

    def _expire(self, store: DynamoJobStore, job_id: str, seconds_ago: int) -> None:
        _table(store).items[job_id]["expires_at"] = Decimal(
            int(time.time()) - seconds_ago
        )

    def test_get_hides_an_expired_record(self, store: DynamoJobStore) -> None:
        store.create(_job("j1"))
        self._expire(store, "j1", 60)
        assert store.get("j1") is None

    def test_list_hides_expired_records(self, store: DynamoJobStore) -> None:
        store.create(_job("old"))
        store.create(_job("new"))
        self._expire(store, "old", 1)
        assert [j.job_id for j in store.list()] == ["new"]
        assert [j.job_id for j in store.list(status="queued")] == ["new"]

    def test_unexpired_and_unset_expiry_are_served(self, store: DynamoJobStore) -> None:
        store.create(_job("live"))
        store.create(_job("legacy"))
        del _table(store).items["legacy"]["expires_at"]
        assert store.get("live") is not None
        assert store.get("legacy") is not None
        assert sorted(j.job_id for j in store.list()) == ["legacy", "live"]
