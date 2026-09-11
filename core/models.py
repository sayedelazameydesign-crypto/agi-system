"""Data objects for the AGI Kernel vertical slice.

Standard library only (Python 3.10+).  Nothing in this module performs I/O:
these are pure value objects that flow through the cognitive loop::

    Goal -> Planner -> Policy -> Executor -> Verifier -> DecisionRecord -> EventLedger
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def utc_now() -> float:
    """Monotonic-free wall clock timestamp (seconds since epoch, UTC)."""
    return time.time()


def new_id(prefix: str) -> str:
    """Short, collision-resistant identifier with a human readable prefix."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def to_plain(obj: Any) -> Any:
    """Recursively convert dataclasses / enums into JSON-serialisable values."""
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_plain(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_plain(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
class ExecStatus(str, Enum):
    """Lifecycle of a single decision record."""

    PROPOSED = "PROPOSED"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    VERIFIED = "VERIFIED"
    DENIED = "DENIED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"

    @property
    def is_terminal(self) -> bool:
        return self in (
            ExecStatus.VERIFIED,
            ExecStatus.DENIED,
            ExecStatus.FAILED,
            ExecStatus.INCONCLUSIVE,
        )

    def __str__(self) -> str:  # nicer f-strings / logging
        return self.value


# --------------------------------------------------------------------------- #
# goal / plan / action
# --------------------------------------------------------------------------- #
@dataclass
class Goal:
    """What the agent wants to achieve.  Carries no behaviour."""

    goal_type: str
    params: Dict[str, Any] = field(default_factory=dict)
    goal_id: str = field(default_factory=lambda: new_id("goal"))
    created_at: float = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return to_plain(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Goal":
        return cls(
            goal_type=data["goal_type"],
            params=dict(data.get("params") or {}),
            goal_id=data.get("goal_id") or new_id("goal"),
            created_at=float(data.get("created_at") or utc_now()),
        )


@dataclass
class Action:
    """A single capability invocation with its own risk estimate."""

    capability: str
    params: Dict[str, Any] = field(default_factory=dict)
    action_id: str = field(default_factory=lambda: new_id("act"))
    rationale: str = ""
    predicted_risk: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return to_plain(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Action":
        return cls(
            capability=data["capability"],
            params=dict(data.get("params") or {}),
            action_id=data.get("action_id") or new_id("act"),
            rationale=data.get("rationale", ""),
            predicted_risk=float(data.get("predicted_risk", 0.0)),
        )


@dataclass
class Plan:
    """An ordered list of actions intended to satisfy a goal."""

    goal_id: str
    actions: List[Action] = field(default_factory=list)
    rationale: str = ""
    plan_id: str = field(default_factory=lambda: new_id("plan"))
    estimated_cost: float = 0.0

    @property
    def capabilities(self) -> List[str]:
        return [a.capability for a in self.actions]

    @property
    def total_risk(self) -> float:
        return round(sum(a.predicted_risk for a in self.actions), 4)

    def to_dict(self) -> Dict[str, Any]:
        return to_plain(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Plan":
        return cls(
            goal_id=data.get("goal_id", ""),
            actions=[Action.from_dict(a) for a in data.get("actions", [])],
            rationale=data.get("rationale", ""),
            plan_id=data.get("plan_id") or new_id("plan"),
            estimated_cost=float(data.get("estimated_cost", 0.0)),
        )


# --------------------------------------------------------------------------- #
# runtime results
# --------------------------------------------------------------------------- #
@dataclass
class Observation:
    """What the executor *claims* happened.  Never trusted by the verifier."""

    capability: str
    ok: bool
    action_id: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    observed_at: float = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return to_plain(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Observation":
        return cls(
            capability=data.get("capability", ""),
            ok=bool(data.get("ok", False)),
            action_id=data.get("action_id", ""),
            data=dict(data.get("data") or {}),
            error=data.get("error"),
            observed_at=float(data.get("observed_at") or utc_now()),
        )


@dataclass
class Evidence:
    """Independent verification result: every check must pass."""

    action_id: str
    capability: str
    checks: Dict[str, bool] = field(default_factory=dict)
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(bool(v) for v in self.checks.values())

    @property
    def failed_checks(self) -> List[str]:
        return [k for k, v in self.checks.items() if not v]

    def to_dict(self) -> Dict[str, Any]:
        payload = to_plain(self)
        payload["passed"] = self.passed
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Evidence":
        return cls(
            action_id=data.get("action_id", ""),
            capability=data.get("capability", ""),
            checks=dict(data.get("checks") or {}),
            details=dict(data.get("details") or {}),
        )


@dataclass
class PolicyDecision:
    """Allow/deny verdict for one action, with an explicit reason."""

    capability: str
    allowed: bool
    reason: str
    risk: float = 0.0
    risk_band: str = "low"

    def to_dict(self) -> Dict[str, Any]:
        return to_plain(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PolicyDecision":
        return cls(
            capability=data.get("capability", ""),
            allowed=bool(data.get("allowed", False)),
            reason=data.get("reason", ""),
            risk=float(data.get("risk", 0.0)),
            risk_band=data.get("risk_band", "low"),
        )


@dataclass
class DecisionRecord:
    """The complete, auditable trace of one cognitive cycle."""

    goal: Goal
    status: ExecStatus = ExecStatus.PROPOSED
    run_id: str = field(default_factory=lambda: new_id("run"))
    plan: Optional[Plan] = None
    alternatives: List[Plan] = field(default_factory=list)
    policy: List[PolicyDecision] = field(default_factory=list)
    observations: List[Observation] = field(default_factory=list)
    evidence: List[Evidence] = field(default_factory=list)
    denial_reasons: List[str] = field(default_factory=list)
    error: Optional[str] = None
    started_at: float = field(default_factory=utc_now)
    finished_at: Optional[float] = None

    # -- convenience ------------------------------------------------------- #
    @property
    def verified(self) -> bool:
        return self.status == ExecStatus.VERIFIED

    @property
    def evidence_passed(self) -> bool:
        return bool(self.evidence) and all(e.passed for e in self.evidence)

    @property
    def duration_ms(self) -> float:
        end = self.finished_at if self.finished_at is not None else utc_now()
        return round((end - self.started_at) * 1000.0, 3)

    def to_dict(self) -> Dict[str, Any]:
        return to_plain(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DecisionRecord":
        record = cls(
            goal=Goal.from_dict(data["goal"]),
            status=ExecStatus(data.get("status", ExecStatus.PROPOSED.value)),
            run_id=data.get("run_id") or new_id("run"),
            plan=Plan.from_dict(data["plan"]) if data.get("plan") else None,
            alternatives=[Plan.from_dict(p) for p in data.get("alternatives", [])],
            policy=[PolicyDecision.from_dict(p) for p in data.get("policy", [])],
            observations=[Observation.from_dict(o) for o in data.get("observations", [])],
            evidence=[Evidence.from_dict(e) for e in data.get("evidence", [])],
            denial_reasons=list(data.get("denial_reasons", [])),
            error=data.get("error"),
            started_at=float(data.get("started_at") or utc_now()),
            finished_at=data.get("finished_at"),
        )
        return record


__all__ = [
    "ExecStatus",
    "Goal",
    "Action",
    "Plan",
    "Observation",
    "Evidence",
    "PolicyDecision",
    "DecisionRecord",
    "new_id",
    "utc_now",
    "to_plain",
]
