"""Drive a stage from a recorded event log without re-simulating (FR-OBS-04, AC-X-01)."""

from __future__ import annotations

from collections.abc import Callable

from simcore.adapters.omniverse.compat import StageAPI
from simcore.core.facility.graph import NavGraph
from simcore.core.obs.eventlog import EventLog
from simcore.core.obs.replay import trajectories_from_log

__all__ = ["replay_to_stage"]


def replay_to_stage(
    log: EventLog,
    nav: NavGraph,
    stage: StageAPI,
    *,
    start: float = 0.0,
    end: float | None = None,
    dt: float = 0.5,
    on_frame: Callable[[float], None] | None = None,
) -> int:
    """Write poses for each frame in ``[start, end]``; returns the number of frames."""
    store = trajectories_from_log(log, nav)
    stage.open(nav.manifest.usd_stage)
    for agent in store.spawn:
        stage.add_agent(agent, None, (1.0, 0.8))
    end = end if end is not None else max((r.time for r in log), default=start)
    frames = 0
    t = start
    while t <= end + 1e-9:
        for agent in store.spawn:
            pos = store.position(agent, t)
            if pos is not None:
                stage.set_pose(agent, pos, store.heading(agent, t))
        stage.update(t)
        if on_frame is not None:
            on_frame(t)
        frames += 1
        t += dt
    return frames
