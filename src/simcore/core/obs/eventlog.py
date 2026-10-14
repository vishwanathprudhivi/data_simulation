"""Append-only, typed, sim-time-stamped event log (FR-OBS-01, AC-ENG-02).

Records are kept columnar in memory and written to Parquet (and optionally
JSONL). Output is byte-identical for identical runs: no wall-clock values are
recorded and the ``data`` column is canonical JSON.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from simcore.core.hashing import canonical_json

__all__ = ["EVENT_SCHEMA", "EventLog", "EventRecord", "EventType"]


class EventType:
    """Core event types. Domains may emit their own (namespaced ``<domain>.<event>``)."""

    SIM_START = "sim.start"
    SIM_END = "sim.end"
    TASK_RELEASED = "task.released"
    TASK_ASSIGNED = "task.assigned"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_REQUEUED = "task.requeued"
    AGENT_CREATED = "agent.created"
    AGENT_STATE = "agent.state"
    AGENT_MOVE = "agent.move"
    BLOCKING = "interaction.blocking"
    COLOCATION = "interaction.colocation"
    DISRUPTION_START = "disruption.start"
    DISRUPTION_END = "disruption.end"
    POLICY_DECISION = "policy.decision"
    WARNING = "sim.warning"


EVENT_SCHEMA = pa.schema(
    [
        ("seq", pa.int64()),
        ("time", pa.float64()),
        ("type", pa.string()),
        ("agent", pa.string()),
        ("task", pa.string()),
        ("location", pa.string()),
        ("data", pa.string()),
    ]
)


@dataclass(frozen=True)
class EventRecord:
    seq: int
    time: float
    type: str
    agent: str | None
    task: str | None
    location: str | None
    data: Mapping[str, Any]


class EventLog:
    def __init__(self) -> None:
        self._seq: list[int] = []
        self._time: list[float] = []
        self._type: list[str] = []
        self._agent: list[str | None] = []
        self._task: list[str | None] = []
        self._location: list[str | None] = []
        self._data: list[str] = []
        # Raw data dicts kept for fast in-process KPI computation.
        self._raw: list[Mapping[str, Any]] = []

    def emit(
        self,
        time: float,
        type_: str,
        *,
        agent: str | None = None,
        task: str | None = None,
        location: str | None = None,
        **data: Any,
    ) -> None:
        if self._time and time < self._time[-1]:
            raise ValueError(f"event log is append-only in time: {time} < {self._time[-1]}")
        self._seq.append(len(self._seq))
        self._time.append(float(time))
        self._type.append(type_)
        self._agent.append(agent)
        self._task.append(task)
        self._location.append(location)
        self._data.append(canonical_json(data) if data else "{}")
        self._raw.append(data)

    def __len__(self) -> int:
        return len(self._seq)

    def __iter__(self) -> Iterator[EventRecord]:
        for i in range(len(self._seq)):
            yield self.record(i)

    def record(self, i: int) -> EventRecord:
        return EventRecord(
            self._seq[i],
            self._time[i],
            self._type[i],
            self._agent[i],
            self._task[i],
            self._location[i],
            self._raw[i],
        )

    def of_type(self, *types: str) -> Iterator[EventRecord]:
        wanted = set(types)
        for i, t in enumerate(self._type):
            if t in wanted:
                yield self.record(i)

    def to_table(self) -> pa.Table:
        return pa.table(
            {
                "seq": pa.array(self._seq, pa.int64()),
                "time": pa.array(self._time, pa.float64()),
                "type": pa.array(self._type, pa.string()),
                "agent": pa.array(self._agent, pa.string()),
                "task": pa.array(self._task, pa.string()),
                "location": pa.array(self._location, pa.string()),
                "data": pa.array(self._data, pa.string()),
            },
            schema=EVENT_SCHEMA,
        )

    def write_parquet(self, path: Path) -> None:
        pq.write_table(
            self.to_table(), path, compression="zstd", write_statistics=True, use_dictionary=True
        )

    def write_jsonl(self, path: Path) -> None:
        with path.open("w", encoding="utf-8") as fh:
            for i in range(len(self._seq)):
                row = {
                    "seq": self._seq[i],
                    "time": self._time[i],
                    "type": self._type[i],
                    "agent": self._agent[i],
                    "task": self._task[i],
                    "location": self._location[i],
                    "data": json.loads(self._data[i]),
                }
                fh.write(canonical_json(row) + "\n")

    @classmethod
    def from_parquet(cls, path: Path) -> EventLog:
        """Reload a log written by :meth:`write_parquet` (FR-OBS-04 replay)."""
        table = pq.read_table(path)
        log = cls()
        cols = {name: table.column(name).to_pylist() for name in EVENT_SCHEMA.names}
        log._seq = cols["seq"]
        log._time = cols["time"]
        log._type = cols["type"]
        log._agent = cols["agent"]
        log._task = cols["task"]
        log._location = cols["location"]
        log._data = cols["data"]
        log._raw = [json.loads(d) for d in log._data]
        return log
