"""Rebuild agent trajectories from an event log without re-simulating (FR-OBS-04).

Used by ``simcore replay``, the kinematic backend's replay mode and the 2D viewer.
Each ``agent.move`` record carries the edge endpoints, the entry time and the
planned arrival; between moves an agent stays where it last arrived.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from simcore.core.backend import TrajectoryStore
from simcore.core.facility.graph import NavGraph
from simcore.core.obs.eventlog import EventLog, EventType

__all__ = ["agent_states_at", "snapshot", "trajectories_from_log"]

Position = tuple[float, ...]


def trajectories_from_log(log: EventLog, nav: NavGraph) -> TrajectoryStore:
    store = TrajectoryStore()
    for r in log.of_type(EventType.AGENT_CREATED):
        if r.agent is None:
            continue
        pos = r.data.get("position")
        store.add_agent(r.agent, tuple(pos) if pos is not None else None)
    for r in log.of_type(EventType.AGENT_MOVE):
        if r.agent is None or r.agent not in store.spawn:
            continue
        src, dst = r.data.get("source"), r.data.get("target")
        if src is None or dst is None:
            continue
        entry = float(r.data["entry"])
        arrive = r.data.get("arrive")
        end = float(arrive) if arrive is not None else r.time
        store.add_move(r.agent, [nav.positions[src], nav.positions[dst]], [entry, end])
    return store


def agent_states_at(log: EventLog, time: float) -> dict[str, dict[str, Any]]:
    """Last known state (and task) of every agent at ``time``."""
    out: dict[str, dict[str, Any]] = {}
    for r in log.of_type(EventType.AGENT_STATE):
        if r.time > time:
            break
        if r.agent:
            out[r.agent] = {"state": r.data.get("state"), "task": r.task}
    return out


def snapshot(
    log: EventLog, nav: NavGraph, time: float, store: TrajectoryStore | None = None
) -> dict[str, dict[str, Any]]:
    """Positions and states of all agents at ``time``."""
    store = store or trajectories_from_log(log, nav)
    states: Mapping[str, dict[str, Any]] = agent_states_at(log, time)
    return {
        agent: {"position": store.position(agent, time), **states.get(agent, {})}
        for agent in store.spawn
    }
