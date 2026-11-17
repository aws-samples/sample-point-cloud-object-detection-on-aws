"""Job owners in both job stores (threat model T31).

A job records who created it, reads and lists filter on it, and a status update,
which the worker makes without knowing the owner, keeps it.
"""

import pytest

from pointcloud_detect.api.models import JobStatus
from pointcloud_detect.api.ownership import ANY_OWNER
from pointcloud_detect.api.store import DynamoJobStore, InMemoryJobStore
from tests._identity import ALICE, BOB
from tests.test_job_store_dynamo import FakeTable, _job


def _dynamo() -> DynamoJobStore:
    s = DynamoJobStore.__new__(DynamoJobStore)
    s._table = FakeTable()  # pyright: ignore[reportAttributeAccessIssue]
    s._ttl_days = 90
    return s


@pytest.fixture(params=["memory", "dynamodb"])
def store(request: pytest.FixtureRequest) -> InMemoryJobStore | DynamoJobStore:
    return InMemoryJobStore() if request.param == "memory" else _dynamo()


class TestOwners:
    def test_the_owner_reads_their_job(self, store) -> None:
        store.create(_job("a1"), owner=ALICE)
        job = store.get("a1", owner=ALICE)
        assert job is not None and job.job_id == "a1"

    def test_another_user_does_not(self, store) -> None:
        store.create(_job("a1"), owner=ALICE)
        assert store.get("a1", owner=BOB) is None

    def test_no_filter_reads_every_job(self, store) -> None:
        store.create(_job("a1"), owner=ALICE)
        assert store.get("a1") is not None
        assert store.get("a1", owner=ANY_OWNER) is not None

    def test_a_job_with_no_owner_belongs_to_no_user(self, store) -> None:
        store.create(_job("old"))
        assert store.get("old", owner=ALICE) is None
        assert store.get("old") is not None

    def test_lists_are_filtered(self, store) -> None:
        store.create(_job("a1"), owner=ALICE)
        store.create(_job("a2"), owner=ALICE)
        store.create(_job("b1"), owner=BOB)
        store.create(_job("old"))
        assert {j.job_id for j in store.list(owner=ALICE)} == {"a1", "a2"}
        assert {j.job_id for j in store.list(owner=BOB)} == {"b1"}
        assert {j.job_id for j in store.list()} == {"a1", "a2", "b1", "old"}

    def test_status_and_owner_filters_combine(self, store) -> None:
        store.create(_job("a1"), owner=ALICE)
        store.create(_job("b1"), owner=BOB)
        store.update_status("b1", status=JobStatus.complete)
        store.update_status("a1", status=JobStatus.complete)
        done = store.list(status=JobStatus.complete, owner=BOB)
        assert [j.job_id for j in done] == ["b1"]

    def test_a_status_update_keeps_the_owner(self, store) -> None:
        """The worker updates by job id alone; the job must stay its owner's."""
        store.create(_job("a1"), owner=ALICE)
        store.update_status("a1", status=JobStatus.processing)
        store.update_status("a1", status=JobStatus.complete, detection_count=3)
        job = store.get("a1", owner=ALICE)
        assert job is not None and job.status == JobStatus.complete
        assert store.get("a1", owner=BOB) is None

    def test_the_owner_is_not_part_of_the_job(self, store) -> None:
        store.create(_job("a1"), owner=ALICE)
        job = store.get("a1", owner=ALICE)
        assert job is not None
        assert ALICE not in job.model_dump_json()

    def test_delete_forgets_the_owner(self, store) -> None:
        store.create(_job("a1"), owner=ALICE)
        store.delete("a1")
        store.create(_job("a1"), owner=BOB)
        assert store.get("a1", owner=ALICE) is None


class TestDynamoItem:
    def test_the_owner_is_a_top_level_attribute(self) -> None:
        store = _dynamo()
        store.create(_job("a1"), owner=ALICE)
        item = store._table.items["a1"]  # pyright: ignore[reportAttributeAccessIssue]
        assert item["owner_id"] == ALICE
        assert ALICE not in item["record"]

    def test_no_owner_writes_no_attribute(self) -> None:
        store = _dynamo()
        store.create(_job("old"))
        item = store._table.items["old"]  # pyright: ignore[reportAttributeAccessIssue]
        assert "owner_id" not in item
