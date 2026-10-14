"""Core KPI calculators on hand-built event logs (FR-OBS-02/03)."""

from __future__ import annotations

import math

import pytest

from simcore.core.obs.eventlog import EventLog, EventType
from simcore.core.obs.kpi import AgentStateKpis, KpiContext, KpiRegistry, QueueKpis, TaskKpis


def _log() -> EventLog:
    log = EventLog()
    log.emit(0.0, EventType.AGENT_STATE, agent="a", state="idle")
    log.emit(1.0, EventType.TASK_RELEASED, task="t1", due_time=5.0)
    log.emit(2.0, EventType.TASK_RELEASED, task="t2", due_time=3.0)
    log.emit(3.0, EventType.TASK_ASSIGNED, agent="a", task="t1")
    log.emit(3.0, EventType.AGENT_STATE, agent="a", state="service")
    log.emit(4.0, EventType.TASK_COMPLETED, agent="a", task="t1")
    log.emit(4.0, EventType.TASK_ASSIGNED, agent="a", task="t2")
    log.emit(6.0, EventType.TASK_COMPLETED, agent="a", task="t2")
    log.emit(6.0, EventType.AGENT_STATE, agent="a", state="idle")
    return log


def test_FR_OBS_03_task_kpis() -> None:
    k = TaskKpis().compute(_log(), KpiContext(0.0, 10.0))
    assert k["tasks_completed"] == 2 and k["throughput"] == pytest.approx(0.2)
    assert k["cycle_time_mean"] == pytest.approx(3.5)  # (3 + 4) / 2
    assert k["wait_time_mean"] == pytest.approx(2.0)  # (2 + 2) / 2
    assert k["due_time_adherence"] == pytest.approx(0.5)


def test_FR_OBS_03_queue_length_time_weighted() -> None:
    k = QueueKpis().compute(_log(), KpiContext(0.0, 10.0))
    # level 1 on [1,2), 2 on [2,3), 1 on [3,4), 0 after => area 4 over 10 s
    assert k["queue_length_mean"] == pytest.approx(0.4)
    assert k["queue_length_max"] == 2
    w = QueueKpis().compute(_log(), KpiContext(2.5, 10.0))
    assert w["queue_length_mean"] == pytest.approx((2 * 0.5 + 1) / 7.5)
    assert w["queue_length_max"] == 2  # level carried into the window


def test_FR_OBS_03_utilization_and_breakdown() -> None:
    k = AgentStateKpis().compute(_log(), KpiContext(0.0, 10.0))
    assert k["utilization"] == pytest.approx(0.3)
    assert k["state_share_idle"] == pytest.approx(0.7)


def test_warmup_excludes_early_tasks() -> None:
    k = TaskKpis().compute(_log(), KpiContext(1.5, 10.0))
    assert k["tasks_released"] == 1 and k["cycle_time_mean"] == pytest.approx(4.0)


def test_registry_rejects_duplicate_keys() -> None:
    reg = KpiRegistry([TaskKpis()])
    with pytest.raises(ValueError, match="already registered"):
        reg.register(TaskKpis())

    class Dup:
        name = "dup"

        def compute(self, log: EventLog, ctx: KpiContext) -> dict[str, float]:
            return {"throughput": 1.0}

    reg.register(Dup())
    with pytest.raises(ValueError, match="more than one"):
        reg.compute(_log(), KpiContext(0.0, 10.0))


def test_empty_log_gives_nan_not_crash() -> None:
    k = TaskKpis().compute(EventLog(), KpiContext(0.0, 10.0))
    assert k["tasks_completed"] == 0 and math.isnan(k["cycle_time_mean"])


def test_event_log_is_append_only_in_time() -> None:
    log = EventLog()
    log.emit(2.0, "x")
    with pytest.raises(ValueError, match="append-only"):
        log.emit(1.0, "y")
