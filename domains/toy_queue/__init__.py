"""M/M/c toy queue: the validation domain for AC-ENG-03 and AC-EXP-01.

Arrivals come from the scenario's demand (stream ``demand``); each of ``c``
servers (agent type ``server``) draws its service time from the behavior's
``service_times`` on stream ``service_time``.
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
from simcore.core.sampling import Sampler, make_sampler

__all__ = ["ToyQueueParams", "ToyQueuePlugin", "plugin"]


class ToyQueueParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_time_key: str = Field(
        default="default", description="Which behaviors.service_times entry servers use."
    )


class ToyQueueModel(DispatchingModel):
    def __init__(self, bundle: ScenarioBundle, params: ToyQueueParams, **kw: object) -> None:
        super().__init__(bundle, params, **kw)  # type: ignore[arg-type]
        self.key = params.service_time_key
        self._samplers: dict[str, Sampler] = {}

    def _sampler(self, agent: AgentRuntime) -> Sampler:
        if agent.type not in self._samplers:
            if agent.spec is None or self.key not in agent.spec.service_times:
                raise SimulationError(
                    f"agent type {agent.type!r} has no service_times.{self.key} in behaviors"
                )
            self._samplers[agent.type] = make_sampler(agent.spec.service_times[self.key])
        return self._samplers[agent.type]

    def start_task(self, ctx: SimContext, agent: AgentRuntime, task: Task) -> None:
        duration = self._sampler(agent)(ctx.rng.stream("service_time"))
        ctx.emit(EventType.TASK_STARTED, agent=agent.id, task=task.id, service_time=duration)
        self.activity(agent, "service", duration, "serve", self._served)

    def _served(self, ctx: SimContext, agent: AgentRuntime, _: object) -> None:
        self.complete_task(ctx, agent)


class NumberInSystem:
    """Time-average number of tasks in the system (queue + service), ``L`` in Little's law."""

    name = "toy_queue.system"

    def compute(self, log: EventLog, ctx: KpiContext) -> dict[str, float]:
        level, prev, area = 0, 0.0, 0.0
        for r in log.of_type(EventType.TASK_RELEASED, EventType.TASK_COMPLETED):
            lo, hi = max(prev, ctx.warmup), min(r.time, ctx.end)
            if hi > lo:
                area += level * (hi - lo)
            prev = r.time
            level += 1 if r.type == EventType.TASK_RELEASED else -1
        lo = max(prev, ctx.warmup)
        if ctx.end > lo:
            area += level * (ctx.end - lo)
        return {"number_in_system_mean": area / ctx.window if ctx.window > 0 else float("nan")}


class ToyQueuePlugin(DomainPlugin):
    name = "toy_queue"
    version = "0.1.0"
    description = "M/M/c queue for analytical validation (Erlang C)."

    def entity_types(self) -> Mapping[str, str]:
        return {"server": "A server processing one customer at a time"}

    def kpi_calculators(self) -> Sequence[KpiCalculator]:
        return (NumberInSystem(),)

    def params_model(self) -> type[BaseModel]:
        return ToyQueueParams

    def create_model(self, bundle: ScenarioBundle, params: BaseModel | None) -> ToyQueueModel:
        assert isinstance(params, ToyQueueParams)
        return ToyQueueModel(bundle, params, policies=self.policies())


plugin = ToyQueuePlugin()
