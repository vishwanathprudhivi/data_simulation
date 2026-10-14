"""Warehouse outbound reference domain (BRD Section 9).

M1 placeholder: a pick-and-stage loop with sampled travel and pick times so the
plug-in can be loaded and run alongside ``toy_queue`` (AC-ENG-03). Milestone M3
replaces the sampled travel with facility navigation and geometry-derived
interaction (FR-INT-*), and M6 publishes ``domain.yaml``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from simcore.core.config import ScenarioBundle
from simcore.core.dispatch.task import Task
from simcore.core.model import AgentRuntime, DispatchingModel, SimContext, SimulationError
from simcore.core.obs.eventlog import EventLog, EventType
from simcore.core.obs.kpi import KpiCalculator, KpiContext
from simcore.core.plugins import DomainPlugin
from simcore.core.sampling import make_sampler
from simcore.specs.distributions import DistributionSpec, ExponentialDist

__all__ = ["WarehouseParams", "WarehousePlugin", "plugin"]


class WarehouseParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    travel_time: DistributionSpec = Field(
        default_factory=lambda: ExponentialDist(mean=60.0),
        description="Placeholder one-way travel time until M3 navigation lands (seconds).",
    )
    pallets_per_trip: int = Field(default=3, ge=1, le=3, description="Triple pallet jack: <= 3.")


class WarehouseModel(DispatchingModel):
    def __init__(self, bundle: ScenarioBundle, params: WarehouseParams, **kw: object) -> None:
        super().__init__(bundle, params, **kw)  # type: ignore[arg-type]
        self.wparams = params
        self._travel = make_sampler(params.travel_time)

    def start_task(self, ctx: SimContext, agent: AgentRuntime, task: Task) -> None:
        travel = self._travel(ctx.rng.stream("travel"))
        ctx.emit(EventType.TASK_STARTED, agent=agent.id, task=task.id)
        self.activity(
            agent, "travel", travel, "to_pick", self._at_pick, location=task.origin, leg="to_pick"
        )

    def _at_pick(self, ctx: SimContext, agent: AgentRuntime, _: object) -> None:
        if agent.spec is None or "pick" not in agent.spec.service_times:
            raise SimulationError(f"agent type {agent.type!r} needs service_times.pick")
        pick = make_sampler(agent.spec.service_times["pick"])(ctx.rng.stream("service_time"))
        self.activity(agent, "service", pick, "pick", self._picked)

    def _picked(self, ctx: SimContext, agent: AgentRuntime, _: object) -> None:
        travel = self._travel(ctx.rng.stream("travel"))
        dest = agent.task.destination if agent.task else None
        self.activity(
            agent, "travel", travel, "to_stage", self._staged, location=dest, leg="to_stage"
        )

    def _staged(self, ctx: SimContext, agent: AgentRuntime, _: object) -> None:
        self.complete_task(ctx, agent)


class PalletKpis:
    name = "warehouse.pallets"

    def compute(self, log: EventLog, ctx: KpiContext) -> dict[str, float]:
        done = sum(
            1 for r in log.of_type(EventType.TASK_COMPLETED) if ctx.warmup <= r.time <= ctx.end
        )
        hours = ctx.window / 3600.0
        return {"pallets_staged_per_hour": done / hours if hours > 0 else float("nan")}


class WarehousePlugin(DomainPlugin):
    name = "warehouse"
    version = "0.0.1"
    description = "Warehouse outbound (placeholder until M3)."

    def entity_types(self) -> Mapping[str, str]:
        return {
            "pallet_jack_operator": "Operator with a triple pallet jack",
            "selector": "Selector / picker",
        }

    def kpi_calculators(self) -> Sequence[KpiCalculator]:
        return (PalletKpis(),)

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
