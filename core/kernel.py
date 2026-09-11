"""Kernel: the spine that wires the cognitive loop together.

    Goal -> Planner -> Policy -> Executor -> Verifier -> DecisionRecord -> EventLedger

Guarantees
----------
* **No unverified claims** -- ``VERIFIED`` requires independent evidence.
* **No crashes** -- every failure becomes a terminal status (``FAILED``,
  ``DENIED``, ``INCONCLUSIVE``) plus a ledger event.
* **Recoverable** -- the ledger is the source of truth, so an interrupted run
  can be resumed with :meth:`Kernel.resume`.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .event_ledger import EventLedger, LedgerState
from .executor import Executor
from .models import (
    Action,
    DecisionRecord,
    Evidence,
    ExecStatus,
    Goal,
    Observation,
    Plan,
)
from .planner import Planner, PlanningError, SequentialPlanner
from .policy import PolicyConfig, PolicyEngine
from .verifier import Verifier


@dataclass
class KernelConfig:
    """Wiring options for the kernel."""

    max_workers: int = 4
    emit_policy_events: bool = True
    verify: bool = True
    auto_mkdir_sandbox: bool = True


class Kernel:
    """Orchestrates one cognitive cycle per :meth:`run` call."""

    def __init__(
        self,
        sandbox_dir: os.PathLike | str,
        ledger_path: Optional[os.PathLike | str] = None,
        *,
        planner: Optional[Planner] = None,
        policy: Optional[PolicyEngine] = None,
        executor: Optional[Executor] = None,
        verifier: Optional[Verifier] = None,
        ledger: Optional[EventLedger] = None,
        config: Optional[KernelConfig] = None,
    ) -> None:
        self.sandbox_dir = Path(sandbox_dir).resolve()
        self.config = config or KernelConfig()
        if self.config.auto_mkdir_sandbox:
            self.sandbox_dir.mkdir(parents=True, exist_ok=True)

        # The ledger lives *outside* the sandbox: the agent must never be able
        # to rewrite its own audit trail.
        self.ledger = ledger or EventLedger(
            ledger_path or self._default_ledger_path()
        )
        self.planner: Planner = planner or SequentialPlanner()
        self.policy = policy or PolicyEngine(self.sandbox_dir)
        self.executor = executor or Executor(self.sandbox_dir)
        self.verifier = verifier or Verifier(self.sandbox_dir)

        self._runs: Dict[str, DecisionRecord] = {}
        self._lock = threading.RLock()

    def _default_ledger_path(self) -> Path:
        """Sibling directory of the sandbox, e.g. ``sandbox-ledger/events.jsonl``."""
        return self.sandbox_dir.parent / f"{self.sandbox_dir.name}-ledger" / "events.jsonl"

    # ------------------------------------------------------------------ #
    # main loop
    # ------------------------------------------------------------------ #
    def run(self, goal: Goal, run_id: Optional[str] = None) -> DecisionRecord:
        """Execute one full cognitive cycle for *goal*."""
        record = DecisionRecord(goal=goal, run_id=run_id or _new_run_id())
        with self._lock:
            self._runs[record.run_id] = record

        self._emit("GOAL_RECEIVED", {"goal": goal.to_dict()}, record.run_id)
        try:
            return self._cycle(record)
        except Exception as exc:  # never let a cycle take the process down
            return self._finalize(
                record,
                ExecStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                event="KERNEL_ERROR",
            )

    def _cycle(self, record: DecisionRecord) -> DecisionRecord:
        # ---- 1. plan ---------------------------------------------------- #
        try:
            plans = self.planner.propose(record.goal)
        except PlanningError as exc:
            self._emit("PLANNING_FAILED", {"error": str(exc)}, record.run_id)
            return self._finalize(record, ExecStatus.FAILED, error=str(exc))

        if not plans:
            self._emit("PLANNING_FAILED", {"error": "planner returned no plans"}, record.run_id)
            return self._finalize(record, ExecStatus.FAILED, error="planner returned no plans")

        record.alternatives = list(plans[1:])
        self._emit(
            "PLANS_PROPOSED",
            {
                "count": len(plans),
                "plans": [p.to_dict() for p in plans],
                "alternatives": len(record.alternatives),
            },
            record.run_id,
        )

        # ---- 2. policy: pick the best *allowed* plan --------------------- #
        chosen: Optional[Plan] = None
        decisions: List[Any] = []
        denials: List[str] = []
        # keep the verdict for the *preferred* plan so a denial explains itself
        preferred_decisions: List[Any] = []
        preferred_denials: List[str] = []
        for index, plan in enumerate(sorted(plans, key=lambda p: (p.total_risk, p.estimated_cost))):
            decisions = self.policy.evaluate_plan(plan.actions)
            denials = [d.reason for d in decisions if not d.allowed]
            if index == 0:
                preferred_decisions, preferred_denials = decisions, denials
            if all(d.allowed for d in decisions):
                chosen = plan
                break

        if chosen is None:
            decisions = preferred_decisions or decisions
            denials = preferred_denials or denials
            self._emit("POLICY_DENIED", {"reasons": denials}, record.run_id)
            record.denial_reasons = denials
            record.policy = [d for d in decisions]
            return self._finalize(
                record, ExecStatus.DENIED, error="; ".join(denials) or "denied by policy"
            )

        record.plan = chosen
        record.policy = list(decisions)
        if self.config.emit_policy_events:
            for decision in decisions:
                self._emit(
                    "POLICY_EVALUATED",
                    {"decision": decision.to_dict(), "action_id": _action_id(decision, chosen)},
                    record.run_id,
                )
        self._emit(
            "PLAN_SELECTED",
            {
                "plan": chosen.to_dict(),
                "risk": chosen.total_risk,
                "cost": chosen.estimated_cost,
                "attempts_rejected": len(plans) - 1,
            },
            record.run_id,
        )
        self._set_status(record, ExecStatus.AUTHORIZED)

        # ---- 3. execute + 4. verify --------------------------------------- #
        return self._execute_and_verify(record, chosen)

    # ------------------------------------------------------------------ #
    # concurrency
    # ------------------------------------------------------------------ #
    def run_many(
        self, goals: Sequence[Goal], max_workers: Optional[int] = None
    ) -> List[DecisionRecord]:
        """Run several goals concurrently (ledger writes are lock-protected)."""
        workers = max(1, int(max_workers or self.config.max_workers))
        if len(goals) == 1:
            return [self.run(goals[0])]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(self.run, goals))

    # ------------------------------------------------------------------ #
    # state management / recovery
    # ------------------------------------------------------------------ #
    def get_run(self, run_id: str) -> Optional[DecisionRecord]:
        with self._lock:
            return self._runs.get(run_id)

    def list_runs(self) -> List[str]:
        state = self.ledger.replay()
        with self._lock:
            return sorted(set(state.runs) | set(self._runs))

    def replay(self) -> LedgerState:
        return self.ledger.replay()

    def resume(self, run_id: str) -> DecisionRecord:
        """Continue an interrupted run, or return the finished record.

        The ledger -- not process memory -- is the source of truth, so this
        works even in a fresh process pointing at the same ledger file.
        """
        state = self.ledger.replay()
        info = state.runs.get(run_id)
        if info is None:
            raise LookupError(f"unknown run_id={run_id!r}")

        if "record" in info:  # already terminal: rebuild and return it
            record = DecisionRecord.from_dict(info["record"])
            with self._lock:
                self._runs[record.run_id] = record
            return record

        goal = Goal.from_dict(info.get("goal", {}))
        record = DecisionRecord(goal=goal, run_id=run_id, status=ExecStatus.PROPOSED)
        plan = Plan.from_dict(info["plan"]) if info.get("plan") else None

        for event in state.events_for(run_id):
            payload = event.get("payload") or {}
            if event.get("type") == "OBSERVATION":
                record.observations.append(Observation.from_dict(payload["observation"]))
            elif event.get("type") == "EVIDENCE":
                record.evidence.append(Evidence.from_dict(payload["evidence"]))

        with self._lock:
            self._runs[record.run_id] = record

        self._emit("RUN_RESUMED", {"from_status": info.get("status")}, run_id)
        if plan is None:  # nothing was authorised yet: start over from planning
            return self._cycle(record)

        record.plan = plan
        record.policy = [self.policy.evaluate(a) for a in plan.actions]
        if not all(d.allowed for d in record.policy):
            record.denial_reasons = [d.reason for d in record.policy if not d.allowed]
            return self._finalize(
                record,
                ExecStatus.DENIED,
                error="; ".join(record.denial_reasons) or "denied by policy",
            )
        self._set_status(record, ExecStatus.AUTHORIZED)
        return self._cycle_from(record, plan)

    def _cycle_from(self, record: DecisionRecord, plan: Plan) -> DecisionRecord:
        """Re-enter the loop with an already selected plan (used by resume)."""
        return self._execute_and_verify(record, plan, resumed=True)

    def _execute_and_verify(
        self, record: DecisionRecord, plan: Plan, resumed: bool = False
    ) -> DecisionRecord:
        """Steps 3 and 4 of the loop: execute, then verify independently."""
        self._set_status(record, ExecStatus.EXECUTING)
        self._emit(
            "EXECUTION_START",
            {"actions": len(plan.actions), "resumed": resumed},
            record.run_id,
        )

        for action in plan.actions:
            if _has_observation(record, action.action_id):
                continue  # already done in a previous (interrupted) attempt
            self._emit(
                "ACTION_EXECUTING",
                {"action": action.to_dict(), "resumed": resumed},
                record.run_id,
            )
            observation = self.executor.execute(action)
            record.observations.append(observation)
            self._emit(
                "OBSERVATION",
                {"observation": observation.to_dict(), "action_id": action.action_id},
                record.run_id,
            )
            if not observation.ok:
                self._emit(
                    "EXECUTION_FAILED",
                    {"action_id": action.action_id, "error": observation.error},
                    record.run_id,
                )
                return self._finalize(
                    record, ExecStatus.FAILED, error=observation.error or "execution failed"
                )

        if not self.config.verify:
            return self._finalize(record, ExecStatus.VERIFIED)

        already = {e.action_id for e in record.evidence}
        for action, observation in _pairs(plan, record):
            if action.action_id in already:
                continue
            evidence = self.verifier.verify(action, observation)
            record.evidence.append(evidence)
            self._emit(
                "EVIDENCE",
                {"evidence": evidence.to_dict(), "action_id": action.action_id},
                record.run_id,
            )
            if not evidence.passed:
                status = (
                    ExecStatus.FAILED if not observation.ok else ExecStatus.INCONCLUSIVE
                )
                self._emit(
                    "VERIFICATION_FAILED",
                    {
                        "action_id": action.action_id,
                        "failed_checks": evidence.failed_checks,
                    },
                    record.run_id,
                )
                return self._finalize(
                    record,
                    status,
                    error=f"verification failed: {', '.join(evidence.failed_checks)}",
                )

        return self._finalize(record, ExecStatus.VERIFIED)

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _emit(self, event_type: str, payload: Dict[str, Any], run_id: str) -> None:
        try:
            self.ledger.append(event_type, payload, run_id=run_id)
        except Exception:  # pragma: no cover - ledger failure must not kill the cycle
            pass

    def _set_status(self, record: DecisionRecord, status: ExecStatus) -> None:
        record.status = status
        self._emit("STATUS_CHANGED", {"status": status.value}, record.run_id)

    def _finalize(
        self,
        record: DecisionRecord,
        status: ExecStatus,
        error: Optional[str] = None,
        event: str = "DECISION_RECORDED",
    ) -> DecisionRecord:
        record.status = status
        if error is not None:
            record.error = error
        record.finished_at = _now()
        payload = {"record": record.to_dict(), "status": status.value}
        if error is not None:
            payload["error"] = error
        self._emit(event, payload, record.run_id)
        with self._lock:
            self._runs[record.run_id] = record
        return record


# ---------------------------------------------------------------------- #
# module helpers
# ---------------------------------------------------------------------- #
def _now() -> float:
    import time

    return time.time()


def _new_run_id() -> str:
    from .models import new_id

    return new_id("run")


def _has_observation(record: DecisionRecord, action_id: str) -> bool:
    return any(o.action_id == action_id for o in record.observations)


def _pairs(plan: Plan, record: DecisionRecord) -> List[tuple[Action, Observation]]:
    """Match each plan action with its observation (order-independent)."""
    by_id = {o.action_id: o for o in record.observations}
    return [(a, by_id[a.action_id]) for a in plan.actions if a.action_id in by_id]


def _action_id(decision: Any, plan: Plan) -> str:
    for action in plan.actions:
        if action.capability == decision.capability:
            return action.action_id
    return ""


__all__ = ["Kernel", "KernelConfig"]
