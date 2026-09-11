"""AGI Kernel -- vertical slice.

A dependency-free cognitive loop::

    Goal -> Planner -> Policy -> Executor -> Verifier -> DecisionRecord -> EventLedger

Philosophy: **no claims without evidence.**  A run only reaches ``VERIFIED``
after the verifier independently re-checks the world.
"""

from .clock import DEFAULT_CLOCK, Clock, FrozenClock, SystemClock
from .config import DEFAULT_CONFIG_FILENAME, SCHEMA_VERSION, Limits, RetryPolicy, Settings
from .errors import (
    AbortedError,
    CapabilityDeniedError,
    ConfigurationError,
    ExecutionTimeoutError,
    KernelError,
    LedgerCorruptionError,
    LedgerError,
    MissingParameterError,
    PlanningError,
    PolicyViolation,
    QuotaExceededError,
    RunTimeoutError,
    UnsupportedGoalError,
    SandboxViolationError,
    VerificationError,
)
from .event_ledger import EventLedger, LedgerState
from .executor import Executor
from .kernel import Kernel, KernelConfig
from .logging_setup import configure_logging
from .metrics import MetricsRecorder
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
from .planner import Planner, SequentialPlanner
from .policy import PolicyConfig, PolicyEngine
from .verifier import Verifier

__version__ = "0.3.0"

__all__ = [
    # kernel
    "Kernel",
    "KernelConfig",
    "Planner",
    "PlanningError",
    "UnsupportedGoalError",
    "MissingParameterError",
    "SequentialPlanner",
    "PolicyEngine",
    "PolicyConfig",
    "PolicyDecision",
    "Executor",
    "Verifier",
    "EventLedger",
    "LedgerState",
    # models
    "Goal",
    "Action",
    "Plan",
    "Observation",
    "Evidence",
    "ExecStatus",
    "DecisionRecord",
    # operations (phase 0)
    "Settings",
    "Limits",
    "RetryPolicy",
    "MetricsRecorder",
    "configure_logging",
    "Clock",
    "SystemClock",
    "FrozenClock",
    "DEFAULT_CLOCK",
    "SCHEMA_VERSION",
    "DEFAULT_CONFIG_FILENAME",
    "KernelError",
    "ConfigurationError",
    "PolicyViolation",
    "CapabilityDeniedError",
    "SandboxViolationError",
    "VerificationError",
    "ExecutionTimeoutError",
    "RunTimeoutError",
    "QuotaExceededError",
    "LedgerError",
    "LedgerCorruptionError",
    "AbortedError",
    "__version__",
]
