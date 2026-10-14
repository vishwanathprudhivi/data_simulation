"""Policy interface, built-in dispatch policies, reasoning client (FR-AGT-02, FR-POL-01..05)."""

from simcore.core.policy.base import (
    BUILTIN_POLICIES,
    AgentView,
    Assignment,
    DispatchAction,
    DispatchObservation,
    FifoPolicy,
    NearestTaskPolicy,
    Policy,
    PolicyError,
    TaskView,
    ZoneAffinityPolicy,
    _register_plan_policies,
    make_policy,
)
from simcore.core.policy.plan import (
    PlanFollowerPolicy,
    SolverCallbackPolicy,
    assignment_min_distance,
    load_plan,
)
from simcore.core.policy.reasoning import (
    DisabledReasoningClient,
    MockReasoningClient,
    ReasoningClient,
    ReasoningDisabledError,
    ReasoningRequest,
    ReasoningResponse,
)

_register_plan_policies()

__all__ = [
    "BUILTIN_POLICIES",
    "AgentView",
    "Assignment",
    "DisabledReasoningClient",
    "DispatchAction",
    "DispatchObservation",
    "FifoPolicy",
    "MockReasoningClient",
    "NearestTaskPolicy",
    "PlanFollowerPolicy",
    "Policy",
    "PolicyError",
    "ReasoningClient",
    "ReasoningDisabledError",
    "ReasoningRequest",
    "ReasoningResponse",
    "SolverCallbackPolicy",
    "TaskView",
    "ZoneAffinityPolicy",
    "assignment_min_distance",
    "load_plan",
    "make_policy",
]
