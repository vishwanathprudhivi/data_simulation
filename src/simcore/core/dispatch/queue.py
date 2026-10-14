"""``TaskQueue`` interface and the default in-memory implementation (FR-DSP-02/03/06).

Semantics shared by every implementation (verified by the conformance suite in
``tests/unit/test_queue_conformance.py``):

* Pending order key: ``(-priority, release_time, insertion_seq)``. Never depends
  on wall-clock or hashing (FR-DSP-03).
* ``claim`` moves a pending task to in-flight for one agent.
* ``ack`` removes an in-flight task permanently.
* ``requeue`` returns an in-flight task to pending with its *original* key.
"""

from __future__ import annotations

import bisect
from abc import ABC, abstractmethod

from simcore.core.dispatch.task import Task

__all__ = ["InMemoryQueue", "QueueError", "TaskQueue"]


class QueueError(RuntimeError):
    pass


OrderKey = tuple[int, float, int]


class TaskQueue(ABC):
    """Abstract queue of work items."""

    @abstractmethod
    def put(self, task: Task) -> None:
        """Add a pending task. Duplicate ids are errors."""

    @abstractmethod
    def pending(self) -> tuple[Task, ...]:
        """Pending tasks in queue order."""

    @abstractmethod
    def claim(self, agent_id: str, task_id: str | None = None) -> Task | None:
        """Claim ``task_id`` (or the head task) for ``agent_id``; ``None`` if empty."""

    @abstractmethod
    def ack(self, task_id: str) -> None:
        """Mark an in-flight task done."""

    @abstractmethod
    def requeue(self, task_id: str) -> None:
        """Return an in-flight task to pending (e.g. on agent failure)."""

    @abstractmethod
    def in_flight(self) -> dict[str, str]:
        """Map of in-flight task id -> claiming agent id, in claim order."""

    @abstractmethod
    def __len__(self) -> int:
        """Number of pending tasks."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release external resources."""


class InMemoryQueue(TaskQueue):
    def __init__(self) -> None:
        self._keys: list[OrderKey] = []
        self._by_key: dict[OrderKey, Task] = {}
        self._key_of: dict[str, OrderKey] = {}
        self._flight: dict[str, tuple[str, Task, OrderKey]] = {}
        self._seq = 0

    def put(self, task: Task) -> None:
        if task.id in self._key_of or task.id in self._flight:
            raise QueueError(f"task {task.id!r} already in queue")
        key = (-task.priority, task.release_time, self._seq)
        self._seq += 1
        self._insert(key, task)

    def _insert(self, key: OrderKey, task: Task) -> None:
        bisect.insort(self._keys, key)
        self._by_key[key] = task
        self._key_of[task.id] = key

    def pending(self) -> tuple[Task, ...]:
        return tuple(self._by_key[k] for k in self._keys)

    def claim(self, agent_id: str, task_id: str | None = None) -> Task | None:
        if task_id is None:
            if not self._keys:
                return None
            key = self._keys[0]
        elif task_id in self._key_of:
            key = self._key_of[task_id]
        else:
            raise QueueError(f"task {task_id!r} is not pending")
        del self._keys[bisect.bisect_left(self._keys, key)]
        task = self._by_key.pop(key)
        del self._key_of[task.id]
        self._flight[task.id] = (agent_id, task, key)
        return task

    def ack(self, task_id: str) -> None:
        if task_id not in self._flight:
            raise QueueError(f"task {task_id!r} is not in flight")
        del self._flight[task_id]

    def requeue(self, task_id: str) -> None:
        if task_id not in self._flight:
            raise QueueError(f"task {task_id!r} is not in flight")
        _, task, key = self._flight.pop(task_id)
        self._insert(key, task)

    def in_flight(self) -> dict[str, str]:
        return {tid: agent for tid, (agent, _, _) in self._flight.items()}

    def __len__(self) -> int:
        return len(self._keys)
