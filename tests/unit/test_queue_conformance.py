"""TaskQueue conformance suite (FR-DSP-02/03, AC-DSP-01).

Every queue implementation must pass these tests. The Redis adapter (M5) is
added to ``QUEUE_FACTORIES`` behind the ``redis`` marker.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from simcore.core.dispatch import InMemoryQueue, QueueError, Task, TaskQueue


def _redis() -> TaskQueue:
    import os

    from simcore.core.dispatch.redis_queue import RedisQueue

    return RedisQueue(os.environ["SIMCORE_REDIS_URL"])


QUEUE_FACTORIES: dict[str, Callable[[], TaskQueue]] = {"memory": InMemoryQueue, "redis": _redis}


@pytest.fixture(
    params=[
        "memory",
        pytest.param("redis", marks=pytest.mark.redis),
    ]
)
def queue(request: pytest.FixtureRequest):  # type: ignore[no-untyped-def]
    q = QUEUE_FACTORIES[request.param]()
    yield q
    q.close()


def t(id_: str, priority: int = 0, release: float = 0.0) -> Task:
    return Task(id=id_, priority=priority, release_time=release)


def test_FR_DSP_03_order_priority_release_then_insertion(queue: TaskQueue) -> None:
    for task in (t("c", 0, 5.0), t("a", 0, 1.0), t("hi", 2, 9.0), t("b", 0, 1.0)):
        queue.put(task)
    assert [x.id for x in queue.pending()] == ["hi", "a", "b", "c"]


def test_FR_DSP_02_in_flight_reports_claim_order(queue: TaskQueue) -> None:
    for name in ("a", "b", "c"):
        queue.put(t(name))
    queue.claim("x", "c")
    queue.claim("y", "a")
    assert list(queue.in_flight().items()) == [("c", "x"), ("a", "y")]


def test_FR_DSP_02_claim_ack(queue: TaskQueue) -> None:
    queue.put(t("a"))
    queue.put(t("b"))
    got = queue.claim("agent-1")
    assert got is not None and got.id == "a"
    assert queue.in_flight() == {"a": "agent-1"}
    assert len(queue) == 1
    queue.ack("a")
    assert queue.in_flight() == {}
    with pytest.raises(QueueError):
        queue.ack("a")


def test_FR_DSP_02_claim_specific_and_empty(queue: TaskQueue) -> None:
    assert queue.claim("x") is None
    queue.put(t("a"))
    queue.put(t("b"))
    got = queue.claim("x", "b")
    assert got is not None and got.id == "b"
    with pytest.raises(QueueError, match="not pending"):
        queue.claim("x", "b")


def test_FR_DSP_02_requeue_restores_original_position(queue: TaskQueue) -> None:
    for name in ("a", "b", "c"):
        queue.put(t(name))
    queue.claim("x")  # a
    queue.requeue("a")
    assert [x.id for x in queue.pending()] == ["a", "b", "c"]
    with pytest.raises(QueueError):
        queue.requeue("zzz")


def test_duplicate_ids_rejected(queue: TaskQueue) -> None:
    queue.put(t("a"))
    with pytest.raises(QueueError, match="already"):
        queue.put(t("a"))
    queue.claim("x")
    with pytest.raises(QueueError, match="already"):
        queue.put(t("a"))


def test_task_round_trip_dict() -> None:
    task = Task(
        id="a", type="pick", origin="o", quantity=2.0, due_time=5.0, attributes={"zone": "z"}
    )
    assert Task.from_dict(task.to_dict()) == task
