"""Minimal facility-aware test domain: go to origin, work there, go to destination."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from simcore.core.config import ScenarioBundle, bundle_from_specs
from simcore.core.dispatch.task import Task
from simcore.core.model import AgentRuntime, DispatchingModel, SimContext
from simcore.core.obs.eventlog import EventType
from simcore.core.plugins import DomainPlugin
from simcore.specs import BehaviorsSpec, FacilityManifest, ScenarioSpec


class CourierParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_time: float = 0.0


class CourierModel(DispatchingModel):
    def start_task(self, ctx: SimContext, agent: AgentRuntime, task: Task) -> None:
        ctx.emit(EventType.TASK_STARTED, agent=agent.id, task=task.id)
        assert task.origin and task.destination
        self.go(agent, task.origin, lambda: self._at_origin(agent, task))

    def _at_origin(self, agent: AgentRuntime, task: Task) -> None:
        work = self.params.work_time if self.params else 0.0  # type: ignore[attr-defined]
        if work > 0:
            self.work(agent, task.origin or "", work, lambda: self._leave(agent, task))
        else:
            self._leave(agent, task)

    def _leave(self, agent: AgentRuntime, task: Task) -> None:
        self.go(agent, task.destination or "", lambda: self.complete_task(self.ctx, agent))


class CourierPlugin(DomainPlugin):
    name = "courier"
    version = "0.0.1"

    def params_model(self) -> type[BaseModel]:
        return CourierParams

    def create_model(self, bundle: ScenarioBundle, params: BaseModel | None) -> CourierModel:
        return CourierModel(bundle, params, policies=self.policies())


def corridor(
    nodes: dict[str, tuple[float, float]],
    edges: list[tuple[str, str, str, float]],
    directed: tuple[str, ...] = (),
) -> FacilityManifest:
    """Facility with one location per node (location id == node id prefixed 'at_')."""
    return FacilityManifest.model_validate(
        {
            "id": "test",
            "nodes": [{"id": n, "position": list(p)} for n, p in nodes.items()],
            "edges": [
                {"id": e, "from": a, "to": b, "width": w, "directed": e in directed}
                for e, a, b, w in edges
            ],
            "locations": [
                {"id": f"at_{n}", "kind": "spot", "node": n, "capacity": 2} for n in nodes
            ],
        }
    )


def bundle(
    facility: FacilityManifest,
    agents: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    *,
    duration: float = 200.0,
    interaction: dict[str, Any] | None = None,
    fleet_attrs: dict[str, dict[str, Any]] | None = None,
    params: dict[str, Any] | None = None,
) -> ScenarioBundle:
    """``agents``: [{id, speed, width, length, count, start}] -> behaviors + fleet."""
    behaviors = BehaviorsSpec.model_validate(
        {
            "agent_types": [
                {
                    "id": a["id"],
                    "speeds": {"default": a.get("speed", 1.0)},
                    "footprint": {"width": a.get("width", 0.8), "length": a.get("length", 1.0)},
                    "capabilities": a.get("capabilities", []),
                }
                for a in agents
            ]
        }
    )
    scenario = ScenarioSpec.model_validate(
        {
            "id": "t",
            "domain": "courier",
            "duration": duration,
            "fleet": [
                {
                    "agent_type": a["id"],
                    "count": a.get("count", 1),
                    "attributes": {"start": a["start"], **(fleet_attrs or {}).get(a["id"], {})},
                }
                for a in agents
            ],
            "demand": [{"kind": "list", "tasks": tasks}],
            "interaction": interaction or {},
            "params": params or {},
        }
    )
    return bundle_from_specs(scenario, behaviors, facility)
