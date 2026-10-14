"""Congestion KPIs (FR-INT-06): blocking, space queueing, co-location, travel, edge use."""

from __future__ import annotations

from simcore.core.obs.eventlog import EventLog, EventRecord, EventType
from simcore.core.obs.kpi import KpiContext

__all__ = ["InteractionKpis", "edge_utilization"]


def _clip(start: float, end: float, ctx: KpiContext) -> float:
    return max(0.0, min(end, ctx.end) - max(start, ctx.warmup))


def edge_utilization(log: EventLog, ctx: KpiContext) -> dict[str, float]:
    """Agent-seconds on each edge divided by the window (heat-map data; may exceed 1)."""
    use: dict[str, float] = {}
    for r in log.of_type(EventType.AGENT_MOVE):
        if r.location is None:
            continue
        entry = float(r.data.get("entry", r.time))
        use[r.location] = use.get(r.location, 0.0) + _clip(entry, r.time, ctx)
    return {e: v / ctx.window for e, v in sorted(use.items())} if ctx.window > 0 else {}


class InteractionKpis:
    """Present only when the run moved agents or worked at locations."""

    name = "core.interaction"

    def compute(self, log: EventLog, ctx: KpiContext) -> dict[str, float]:
        moves = list(log.of_type(EventType.AGENT_MOVE))
        services = list(log.of_type(EventType.SERVICE_START, EventType.SERVICE_END))
        if (
            not moves
            and not services
            and not any(True for _ in log.of_type(EventType.BLOCKING, EventType.DEADLOCK))
        ):
            return {}
        out: dict[str, float] = {}
        blocked: dict[str, float] = {}
        events = 0
        agents: set[str] = set()
        for r in log.of_type(EventType.BLOCKING):
            dur = float(r.data.get("duration", 0.0))
            since = float(r.data.get("since", r.time - dur))
            clipped = _clip(since, since + dur, ctx)
            if clipped <= 0:
                continue
            kind = str(r.data.get("kind", "other"))
            blocked[kind] = blocked.get(kind, 0.0) + clipped
            events += 1
            if r.agent:
                agents.add(r.agent)
        fleet = {r.agent for r in log.of_type(EventType.AGENT_CREATED) if r.agent}
        total = sum(blocked.values())
        out["blocked_time_total"] = total
        out["blocked_time_per_agent"] = total / len(fleet) if fleet else 0.0
        out["blocking_events"] = float(events)
        for kind in ("opposing", "following", "space", "no_route"):
            out[f"blocked_time_{kind}"] = sum(v for k, v in blocked.items() if kind in k)
        dl = list(log.of_type(EventType.DEADLOCK))
        # 'timeout' logs "detected" then "resolved"; other strategies log one record.
        out["deadlocks_detected"] = float(
            sum(
                1
                for r in dl
                if r.data.get("status") in ("detected", "unresolved")
                or (r.data.get("status") == "resolved" and r.data.get("strategy") != "timeout")
            )
        )
        out["deadlocks_unresolved"] = float(sum(1 for r in dl if r.data.get("resolved") is False))
        dist = sum(
            float(r.data.get("length", 0.0)) for r in moves if ctx.warmup <= r.time <= ctx.end
        )
        out["travel_distance_total"] = dist
        util = edge_utilization(log, ctx)
        out["edge_utilization_max"] = max(util.values()) if util else 0.0
        out["edge_utilization_mean"] = sum(util.values()) / len(util) if util else 0.0
        out["colocation_time"] = _colocation_time(services, ctx)
        return out


def _colocation_time(records: list[EventRecord], ctx: KpiContext) -> float:
    """Agent-seconds of work done while another agent works in the same group."""
    active: dict[str, int] = {}
    last: dict[str, float] = {}
    total = 0.0
    for r in records:
        g = str(r.data.get("group"))
        k = active.get(g, 0)
        if k >= 2:
            total += k * _clip(last[g], r.time, ctx)
        last[g] = r.time
        active[g] = k + (1 if r.type == EventType.SERVICE_START else -1)
    return total
