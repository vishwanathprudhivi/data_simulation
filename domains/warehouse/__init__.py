"""Warehouse outbound reference domain (BRD Section 9).

Agents: ``pallet_jack_operator`` (triple pallet jack, up to 3 pallets per trip,
loaded vs empty speed) and ``selector`` (walking picker). Work items: pick-and-stage
assignments released in waves with due times (dock departures). Congestion and
travel penalties come from the facility geometry through the core interaction
model; nothing here assumes congestion constants.

Flow per task: travel empty to the pick face -> pick (per-pallet time x quantity,
with bay co-location penalties) -> travel loaded to the staging lane -> drop.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from simcore.core.config import ScenarioBundle
from simcore.core.dispatch.task import Task
from simcore.core.facility.graph import Edge
from simcore.core.model import AgentRuntime, DispatchingModel, SimContext, SimulationError
from simcore.core.obs.eventlog import EventLog, EventType
from simcore.core.obs.kpi import AVAILABLE_EXCLUDED_STATES, KpiCalculator, KpiContext
from simcore.core.plugins import DomainPlugin
from simcore.core.sampling import Sampler, make_sampler
from simcore.specs.common import Duration
from simcore.specs.scenario import DisruptionSpec

__all__ = ["SpeedWindow", "WarehouseParams", "WarehousePlugin", "plugin"]


class SpeedWindow(BaseModel):
    """Speed re-timing: multiply speeds in some aisles during a time window."""

    model_config = ConfigDict(extra="forbid")

    zones: list[str] = Field(description="Zone (aisle) ids; empty = everywhere.")
    start: Duration = 0.0
    end: Duration | None = None
    factor: float = Field(gt=0)
    agent_types: list[str] = Field(default_factory=list, description="Empty = all agents.")


class WarehouseParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pick_time_key: str = Field(default="pick", description="behaviors service_times key.")
    drop_time_key: str = Field(default="drop", description="behaviors service_times key.")
    per_unit: bool = Field(default=True, description="Pick/drop time is per pallet/unit.")
    zone_speed_factors: dict[str, float] = Field(
        default_factory=dict, description="Static speed factor per zone (aisle) id."
    )
    speed_windows: list[SpeedWindow] = Field(default_factory=list)


class WarehouseModel(DispatchingModel):
    def __init__(self, bundle: ScenarioBundle, params: WarehouseParams, **kw: object) -> None:
        super().__init__(bundle, params, **kw)  # type: ignore[arg-type]
        self.wp = params
        self._samplers: dict[tuple[str, str], Sampler] = {}

    def setup(self, ctx: SimContext) -> None:
        if ctx.nav is None:
            raise SimulationError(
                "the warehouse domain needs a facility; set 'facility:' in the scenario"
            )
        super().setup(ctx)

    def _time(self, ctx: SimContext, agent: AgentRuntime, key: str, quantity: float) -> float:
        if (agent.type, key) not in self._samplers:
            if agent.spec is None or key not in agent.spec.service_times:
                raise SimulationError(f"agent type {agent.type!r} needs service_times.{key}")
            self._samplers[(agent.type, key)] = make_sampler(agent.spec.service_times[key])
        draw = self._samplers[(agent.type, key)](ctx.rng.stream("service_time"))
        return draw * (quantity if self.wp.per_unit else 1.0)

    def start_task(self, ctx: SimContext, agent: AgentRuntime, task: Task) -> None:
        if task.origin is None or task.destination is None:
            raise SimulationError(f"task {task.id} needs an origin pick face and a destination")
        capacity = agent.spec.capacity if agent.spec else 1.0
        if task.quantity > capacity:
            raise SimulationError(
                f"task {task.id} has {task.quantity} units but {agent.type} carries {capacity}"
            )
        # Draw both service times up front so CRN draws follow task order, not timing.
        pick = self._time(ctx, agent, self.wp.pick_time_key, task.quantity)
        drop = self._time(ctx, agent, self.wp.drop_time_key, task.quantity)
        ctx.emit(
            EventType.TASK_STARTED, agent=agent.id, task=task.id, pick_time=pick, drop_time=drop
        )
        self.go(
            agent,
            task.origin,
            lambda: self.work(
                agent,
                task.origin or "",
                pick,
                lambda: self.go(
                    agent,
                    task.destination or "",
                    lambda: self.work(
                        agent,
                        task.destination or "",
                        drop,
                        lambda: self.complete_task(ctx, agent),
                        state="drop",
                    ),
                    profile="loaded",
                ),
                state="pick",
            ),
            profile="empty",
        )

    def speed_modifier(self, agent: AgentRuntime, edge: Edge, time: float) -> float:
        factor = self.wp.zone_speed_factors.get(edge.zone or "", 1.0)
        for w in self.wp.speed_windows:
            in_zone = not w.zones or (edge.zone in w.zones)
            in_time = w.start <= time and (w.end is None or time < w.end)
            in_type = not w.agent_types or agent.type in w.agent_types
            if in_zone and in_time and in_type:
                factor *= w.factor
        return factor

    def on_disruption(self, ctx: SimContext, spec: DisruptionSpec, active: bool) -> None:
        if spec.type != "dock_door_closed":
            return
        door = spec.target
        assert ctx.nav is not None and self.services is not None
        lanes = [
            loc.id for loc in ctx.nav.locations.values() if loc.attributes.get("dock_door") == door
        ]
        if not lanes:
            raise SimulationError(f"dock_door_closed: no staging lane belongs to door {door!r}")
        for lane in lanes:
            self.services.set_capacity(lane, 0 if active else None)


def _labor_seconds(log: EventLog, ctx: KpiContext) -> float:
    current: dict[str, tuple[str, float]] = {}
    total = 0.0

    def add(state: str, a: float, b: float) -> None:
        nonlocal total
        if state not in AVAILABLE_EXCLUDED_STATES:
            total += max(0.0, min(b, ctx.end) - max(a, ctx.warmup))

    for r in log.of_type(EventType.AGENT_STATE):
        if r.agent is None:
            continue
        if r.agent in current:
            add(*current[r.agent], r.time)
        current[r.agent] = (str(r.data.get("state")), r.time)
    for state, since in current.values():
        add(state, since, ctx.end)
    return total


class WarehouseKpis:
    """Domain KPIs (BRD 9): PPLH-equivalent, travel per pallet, bay co-location, dock on-time."""

    name = "warehouse.outbound"

    def compute(self, log: EventLog, ctx: KpiContext) -> dict[str, float]:
        released: dict[str, tuple[float, str | None, float | None, float]] = {}
        for r in log.of_type(EventType.TASK_RELEASED):
            if r.task:
                due = r.data.get("due_time")
                released[r.task] = (
                    r.time,
                    r.data.get("destination"),
                    None if due is None else float(due),
                    float(r.data.get("quantity", 1.0)),
                )
        done = {
            r.task: r.time
            for r in log.of_type(EventType.TASK_COMPLETED)
            if r.task and ctx.warmup <= r.time <= ctx.end
        }
        pallets = sum(released[t][3] for t in done if t in released)
        labor_h = _labor_seconds(log, ctx) / 3600.0
        distance = sum(
            float(r.data.get("length", 0.0))
            for r in log.of_type(EventType.AGENT_MOVE)
            if ctx.warmup <= r.time <= ctx.end
        )
        # A departure = (staging lane's door or lane, due time). On time if every task
        # bound for it finished by its due time.
        departures: dict[tuple[str, float], bool] = {}
        for t, (rel, dest, due, _) in released.items():
            if due is None or dest is None or rel < ctx.warmup or due > ctx.end:
                continue
            key = (dest, due)
            ok = t in done and done[t] <= due
            departures[key] = departures.get(key, True) and ok
        group_overlap = 0.0
        active: dict[str, int] = {}
        last: dict[str, float] = {}
        for r in log.of_type(EventType.SERVICE_START, EventType.SERVICE_END):
            if r.data.get("location_kind") != "pick_face":
                continue
            g = str(r.data.get("group"))
            k = active.get(g, 0)
            if k >= 2:
                group_overlap += k * max(0.0, min(r.time, ctx.end) - max(last[g], ctx.warmup))
            last[g] = r.time
            active[g] = k + (1 if r.type == EventType.SERVICE_START else -1)
        out = {
            "pallets_moved": pallets,
            "pallets_per_labor_hour": pallets / labor_h if labor_h > 0 else float("nan"),
            "travel_distance_per_pallet": distance / pallets if pallets else float("nan"),
            "bay_colocation_time": group_overlap,
        }
        if departures:
            out["dock_on_time_rate"] = sum(departures.values()) / len(departures)
            out["dock_departures"] = float(len(departures))
        return out


class WarehousePlugin(DomainPlugin):
    name = "warehouse"
    version = "0.3.1"
    description = "Warehouse outbound: triple pallet jacks and selectors on a geometric layout."

    def entity_types(self) -> Mapping[str, str]:
        return {
            "pallet_jack_operator": "Operator with a triple pallet jack (<= 3 pallets per trip)",
            "selector": "Selector / picker on foot",
            "pick_face": "Bay location where pallets or cases are picked",
            "staging_lane": "Outbound staging lane in front of a dock door",
        }

    def kpi_calculators(self) -> Sequence[KpiCalculator]:
        return (WarehouseKpis(),)

    def scenario_event_types(self) -> Sequence[str]:
        return ("dock_door_closed",)

    def params_model(self) -> type[BaseModel]:
        return WarehouseParams

    def asset_bindings(self) -> Mapping[str, str]:
        return {
            "pallet_jack_operator": "placeholder://capsule",
            "selector": "placeholder://capsule",
        }

    def create_model(self, bundle: ScenarioBundle, params: BaseModel | None) -> WarehouseModel:
        assert isinstance(params, WarehouseParams)
        return WarehouseModel(bundle, params, policies=self.policies())


plugin = WarehousePlugin()
