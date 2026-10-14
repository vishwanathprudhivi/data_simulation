"""Redis-backed ``TaskQueue`` (FR-DSP-02/06, AC-DSP-01).

Same semantics as ``InMemoryQueue`` (verified by the shared conformance suite).
Pending order is kept in a sorted set scored 0 and ordered lexicographically by an
order-preserving encoding of ``(-priority, release_time, seq)``; claim is a Lua
script so it is atomic. Redis is optional: nothing imports this module unless a
Redis queue is requested.
"""

from __future__ import annotations

import json
import struct
import uuid
from typing import Any

from simcore.core.dispatch.queue import QueueError, TaskQueue
from simcore.core.dispatch.task import Task

__all__ = ["RedisQueue", "order_key"]

_CLAIM = """
local key = ARGV[2]
if key == '' then
  local head = redis.call('ZRANGE', KEYS[1], 0, 0)
  if #head == 0 then return false end
  key = head[1]
end
if redis.call('ZREM', KEYS[1], key) == 0 then return false end
local tid = string.sub(key, 38)
local n = redis.call('INCR', KEYS[4])
redis.call('HSET', KEYS[2], tid, string.format('%012x', n) .. '|' .. ARGV[1] .. '|' .. key)
redis.call('HDEL', KEYS[3], tid)
return tid
"""


def _float_key(x: float) -> str:
    """Hex encoding of a float whose string order matches numeric order."""
    (bits,) = struct.unpack(">Q", struct.pack(">d", x))
    bits = bits ^ 0xFFFFFFFFFFFFFFFF if bits >> 63 else bits | (1 << 63)
    return f"{bits:016x}"


def order_key(priority: int, release_time: float, seq: int, task_id: str) -> str:
    """``8 hex (inverted priority) | 16 hex (release) | 12 hex (seq) | id`` (37-char prefix)."""
    inv = 0x7FFFFFFF - max(-0x7FFFFFFF, min(0x7FFFFFFF, priority))
    return f"{inv:08x}{_float_key(release_time)}{seq:012x}:{task_id}"


class RedisQueue(TaskQueue):
    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        namespace: str | None = None,
        client: Any = None,
    ) -> None:
        if client is None:
            import redis

            client = redis.Redis.from_url(url, decode_responses=True)
        self.r = client
        ns = namespace or f"simcore:q:{uuid.uuid4().hex}"  # isolation only; no effect on order
        self.k_pending, self.k_flight = f"{ns}:pending", f"{ns}:flight"
        self.k_key_of, self.k_tasks, self.k_seq = f"{ns}:key_of", f"{ns}:tasks", f"{ns}:seq"
        self.k_claims = f"{ns}:claims"
        self._claim = self.r.register_script(_CLAIM)

    def put(self, task: Task) -> None:
        if self.r.hexists(self.k_key_of, task.id) or self.r.hexists(self.k_flight, task.id):
            raise QueueError(f"task {task.id!r} already in queue")
        seq = int(self.r.incr(self.k_seq)) - 1
        key = order_key(task.priority, task.release_time, seq, task.id)
        pipe = self.r.pipeline(transaction=True)
        pipe.hset(self.k_tasks, task.id, json.dumps(task.to_dict(), sort_keys=True))
        pipe.hset(self.k_key_of, task.id, key)
        pipe.zadd(self.k_pending, {key: 0})
        pipe.execute()

    def _load(self, task_id: str) -> Task:
        raw = self.r.hget(self.k_tasks, task_id)
        if raw is None:
            raise QueueError(f"unknown task {task_id!r}")
        return Task.from_dict(json.loads(raw))

    def pending(self) -> tuple[Task, ...]:
        keys = self.r.zrange(self.k_pending, 0, -1)
        return tuple(self._load(k[37:]) for k in keys)

    def claim(self, agent_id: str, task_id: str | None = None) -> Task | None:
        key = ""
        if task_id is not None:
            key = self.r.hget(self.k_key_of, task_id) or ""
            if not key:
                raise QueueError(f"task {task_id!r} is not pending")
        tid = self._claim(
            keys=[self.k_pending, self.k_flight, self.k_key_of, self.k_claims], args=[agent_id, key]
        )
        if tid is None:
            if task_id is not None:
                raise QueueError(f"task {task_id!r} is not pending")
            return None
        return self._load(str(tid))

    def ack(self, task_id: str) -> None:
        if not self.r.hdel(self.k_flight, task_id):
            raise QueueError(f"task {task_id!r} is not in flight")
        self.r.hdel(self.k_tasks, task_id)

    def requeue(self, task_id: str) -> None:
        entry = self.r.hget(self.k_flight, task_id)
        if entry is None:
            raise QueueError(f"task {task_id!r} is not in flight")
        key = entry.split("|", 2)[2]
        pipe = self.r.pipeline(transaction=True)
        pipe.hdel(self.k_flight, task_id)
        pipe.hset(self.k_key_of, task_id, key)
        pipe.zadd(self.k_pending, {key: 0})
        pipe.execute()

    def in_flight(self) -> dict[str, str]:
        raw = self.r.hgetall(self.k_flight)
        items = sorted((v.split("|", 2)[0], k, v.split("|", 2)[1]) for k, v in raw.items())
        return {tid: agent for _, tid, agent in items}  # claim order

    def __len__(self) -> int:
        return int(self.r.zcard(self.k_pending))

    def close(self) -> None:
        self.r.delete(
            self.k_pending, self.k_flight, self.k_key_of, self.k_tasks, self.k_seq, self.k_claims
        )
