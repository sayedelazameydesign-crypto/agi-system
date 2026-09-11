"""AGI Kernel -- vertical slice.

A dependency-free cognitive loop::

    Goal -> Planner -> Policy -> Executor -> Verifier -> DecisionRecord -> EventLedger

Philosophy: **no claims without evidence.**  A run only reaches ``VERIFIED``
after the verifier independently re-checks the world.
"""

from .event_ledger import EventLedger, LedgerState
from .executor import Executor
from .kernel import Kernel, KernelConfig
from .models import (
    Action,
    DecisionRecord,
    Evidence,
    ExecStatus,
    Goal,
    Observation,
    Plan,
    PolicyDecision,
)
from .planner import Planner, PlanningError, SequentialPlanner
from .policy import PolicyConfig, PolicyEngine
from .verifier import Verifier

__version__ = "0.2.0"

__all__ = [
    "Kernel",
    "KernelConfig",
    "Planner",
    "PlanningError",
    "SequentialPlanner",
    "PolicyEngine",
    "PolicyConfig",
    "PolicyDecision",
    "Executor",
    "Verifier",
    "EventLedger",
    "LedgerState",
    "Goal",
    "Action",
    "Plan",
    "Observation",
    "Evidence",
    "ExecStatus",
    "DecisionRecord",
    "__version__",
]
