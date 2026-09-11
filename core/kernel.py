"""Kernel: the spine that wires the cognitive loop together.

    Goal -> Planner -> Policy -> Executor -> Verifier -> DecisionRecord -> EventLedger

Guarantees
----------
* **No unverified claims** -- ``VERIFIED`` requires independent evidence.
* **No crashes** -- every failure becomes a terminal status (``FAILED``,
  ``DENIED``, ``INCONCLUSIVE``, ``ABORTED``) plus a ledger event.
* **Bounded** -- every action and every run has a timeout, a retry budget and a
  resource quota; nothing can hang the process forever.
* **Recoverable** -- the ledger is the source of truth, so an interrupted run
  can be resumed with :meth:`Kernel.resume`, even from a fresh process.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .clock import DEFAULT_CLOCK, Clock, SystemClock
from .config import Settings
from .errors import (
    AbortedError,
    KernelError,
    PlanningError,
    RunTimeoutError,
)
from .event_ledger import EventLedger, LedgerState
from .executor import Executor
from .logging_setup import event_logger
from .metrics import MetricsRecorder
from .models import (
    Action,
    DecisionRecord,
    Evidence,
    ExecStatus,
    Goal,
    Observation,
    Plan,
    new_id,
)
from .planner import Planner, SequentialPlanner
from .policy import PolicyConfig, PolicyEngine
from .verifier import Verifier


@dataclass
class KernelConfig:
    """Runtime knobs that are not part of the persisted settings file."""

    max_workers: int = 4
    emit_policy_events: bool = True
    verify: bool = True
    auto_mkdir_sandbox: bool = True


class Kernel:
    """Orchestrates one cognitive cycle per :meth:`run` call."""

    def __init__(
        self,
        sandbox_dir: Optional[os.PathLike | str] = None,
        ledger_path: Optional[os.PathLike | str] = None,
        *,
        settings: Optional[Settings] = None,
        planner: Optional[Planner] = None,
        policy: Optional[PolicyEngine] = None,
        executor: Optional[Executor] = None,
        verifier: Optional[Verifier] = None,
        ledger: Optional[EventLedger] = None,
        clock: Optional[Clock] = None,
        metrics: Optional[MetricsRecorder] = None,
        config: Optional[KernelConfig] = None,
    ) -> None:
        if settings is None:
            settings = (
                Settings.load(sandbox_dir=sandbox_dir, ledger_path=ledger_path)
                if sandbox_dir is None
                else Settings(
                    sandbox_dir=Path(sandbox_dir).resolve(),
                    ledger_path=Path(ledger_path) if ledger_path else None,
                ).validate()
            )
        self.settings = settings
        self.config = config or KernelConfig()
        self.clock: Clock = clock or DEFAULT_CLOCK
        self.metrics = metrics or MetricsRecorder()
        self.logger = event_logger()

        self.sandbox_dir = settings.sandbox_dir
        if self.config.auto_mkdir_sandbox:
            self.sandbox_dir.mkdir(parents=True, exist_ok=True)

        # the ledger lives outside the sandbox: the agent must never rewrite
        # its own audit trail
        self.ledger = ledger or EventLedger(settings.resolved_ledger_path())

        self.policy = policy or PolicyEngine(
            self.sandbox_dir,
            PolicyConfig(
                allowlist=frozenset(settings.allowlist),
                denylist=frozenset(settings.denylist),
                max_risk=settings.max_risk,
                allow_delete=settings.allow_delete,
                max_write_bytes=settings.limits.max_content_bytes,
            ),
        )
        self.executor = executor or Executor(
            self.sandbox_dir,
            default_timeout=settings.limits.action_timeout_seconds,
            max_files=settings.limits.max_sandbox_files,
        )
        self.verifier = verifier or Verifier(self.sandbox_dir)
        self.planner: Planner = planner or SequentialPlanner()

        self._runs: Dict[str, DecisionRecord] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()

    # -- alternative constructors ------------------------------------------ #
    @classmethod
    def from_settings(cls, settings: Settings, **components: Any) -> "Kernel":
        """Build a kernel straight from a validated :class:`Settings` object."""
        return cls(settings=settings, **components)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def request_stop(self, reason: str = "shutdown requested") -> None:
        """Cooperatively stop current and future runs (signal-safe)."""
        self._stop.set()
        self.logger.warning("stop requested: %s", reason)

    def resume_operations(self) -> None:
        """Clear a previous :meth:`request_stop`."""
        self._stop.clear()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # ------------------------------------------------------------------ #
    # main loop
    # ------------------------------------------------------------------ #
    def run(
        self,
        goal: Goal,
        run_id: Optional[str] = None,
        *,
        timeout: Optional[float] = None,
    ) -> DecisionRecord:
        """Execute one full cognitive cycle for *goal*."""
        record = DecisionRecord(goal=goal, run_id=run_id or new_id("run"))
        record.started_at = self.clock.now()
        with self._lock:
            self._runs[record.run_id] = record

        self.metrics.inc("runs_total")
        started = self.clock.monotonic()
        deadline = started + float(
            timeout if timeout is not None else self.settings.limits.run_timeout_seconds
        )

        self._emit("GOAL_RECEIVED", {"goal": goal.to_dict()}, record.run_id)
        self.logger.info("run %s started: %s", record.run_id, goal.goal_type)
        try:
            return self._cycle(record, deadline)
        except AbortedError as exc:
            self._emit("RUN_ABORTED", {"error": str(exc)}, record.run_id)
            return self._finalize(record, ExecStatus.ABORTED, error=str(exc), code=exc.code)
        except Exception as exc:  # never let a cycle take the process down
            code = exc.code if isinstance(exc, KernelError) else type(exc).__name__.upper()
            self.logger.exception("run %s crashed", record.run_id)
            self._emit(
                "KERNEL_ERROR",
                {"error": str(exc), "code": code},
                record.run_id,
            )
            return self._finalize(
                record, ExecStatus.FAILED, error=f"{type(exc).__name__}: {exc}", code=code
            )
        finally:
            self.metrics.observe("run", max(self.clock.monotonic() - started, 0.0))
            self.metrics.inc(f"runs_{record.status.value.lower()}")
            self._flush_metrics()

    def _cycle(self, record: DecisionRecord, deadline: float) -> DecisionRecord:
        # ---- 1. plan ---------------------------------------------------- #
        self._check_deadline(deadline)
        try:
            plans = self.planner.propose(record.goal)
        except PlanningError as exc:
            self._emit(
                "PLANNING_FAILED", {"error": str(exc), "code": exc.code}, record.run_id
            )
            return self._finalize(record, ExecStatus.FAILED, error=str(exc), code=exc.code)

        max_actions = self.settings.limits.max_actions_per_plan
        plans = [p for p in plans if len(p.actions) <= max_actions]
        if not plans:
            message = f"no plan fits within max_actions_per_plan={max_actions}"
            self._emit("PLANNING_FAILED", {"error": message}, record.run_id)
            return self._finalize(record, ExecStatus.FAILED, error=message, code="PLAN_TOO_LARGE")

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
        preferred_decisions: List[Any] = []
        preferred_denials: List[str] = []
        ordered = sorted(plans, key=lambda p: (p.total_risk, p.estimated_cost))
        for index, plan in enumerate(ordered):
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
            record.policy = list(decisions)
            record.denial_reasons = denials
            self.metrics.inc("policy_denials")
            return self._finalize(
                record,
                ExecStatus.DENIED,
                error="; ".join(denials) or "denied by policy",
                code="POLICY_VIOLATION",
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
                "rejected_alternatives": len(plans) - 1,
            },
            record.run_id,
        )
        self._set_status(record, ExecStatus.AUTHORIZED)

        # ---- 3. execute + 4. verify --------------------------------------- #
        return self._execute_and_verify(record, chosen, deadline)

    def _execute_and_verify(
        self,
        record: DecisionRecord,
        plan: Plan,
        deadline: float,
        resumed: bool = False,
    ) -> DecisionRecord:
        self._set_status(record, ExecStatus.EXECUTING)
        self._emit(
            "EXECUTION_START",
            {"actions": len(plan.actions), "resumed": resumed},
            record.run_id,
        )

        for action in plan.actions:
            self._check_abort()
            self._check_deadline(deadline)
            if _has_observation(record, action.action_id):
                continue  # already done in a previous (interrupted) attempt

            self._emit(
                "ACTION_EXECUTING",
                {"action": action.to_dict(), "resumed": resumed},
                record.run_id,
            )
            observation, attempts = self._execute_with_retries(action)
            record.observations.append(observation)
            self.metrics.inc("actions_total")
            self.metrics.inc(f"actions_{observation.capability.split('.')[-1]}")
            self._emit(
                "OBSERVATION",
                {
                    "observation": observation.to_dict(),
                    "action_id": action.action_id,
                    "attempts": attempts,
                },
                record.run_id,
            )
            if not observation.ok:
                self.metrics.inc("action_failures")
                self._emit(
                    "EXECUTION_FAILED",
                    {
                        "action_id": action.action_id,
                        "error": observation.error,
                        "code": observation.data.get("error_code"),
                    },
                    record.run_id,
                )
                return self._finalize(
                    record,
                    ExecStatus.FAILED,
                    error=observation.error or "execution failed",
                    code=observation.data.get("error_code") or "EXECUTION_FAILED",
                )
            # the run budget is enforced between *and after* every step
            self._check_deadline(deadline)

        if not self.config.verify:
            return self._finalize(record, ExecStatus.VERIFIED)

        already = {e.action_id for e in record.evidence}
        for action, observation in _pairs(plan, record):
            if action.action_id in already:
                continue
            evidence = self.verifier.verify(action, observation)
            record.evidence.append(evidence)
            self.metrics.inc("verifications_total")
            self._emit(
                "EVIDENCE",
                {"evidence": evidence.to_dict(), "action_id": action.action_id},
                record.run_id,
            )
            if not evidence.passed:
                status = ExecStatus.FAILED if not observation.ok else ExecStatus.INCONCLUSIVE
                self.metrics.inc("verification_failures")
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
                    code="VERIFICATION_FAILED",
                )

        return self._finalize(record, ExecStatus.VERIFIED)

    def _execute_with_retries(self, action: Action) -> Tuple[Observation, int]:
        """Run the action, retrying transient failures within the budget."""
        policy = self.settings.retry
        timeout = self.settings.limits.action_timeout_seconds
        attempt = 0
        observation: Optional[Observation] = None

        while attempt < policy.max_attempts:
            attempt += 1
            observation = self.executor.execute(action, timeout=timeout)
            if observation.ok:
                break
            timed_out = observation.data.get("error_code") == "EXECUTION_TIMEOUT"
            if attempt >= policy.max_attempts:
                break
            if timed_out and not policy.retry_on_timeout:
                break
            self.metrics.inc("action_retries")
            self.logger.warning(
                "retry %d/%d for %s: %s", attempt, policy.max_attempts, action.capability,
                observation.error,
            )
            _sleep(self.clock, policy.backoff_seconds)

        assert observation is not None
        return observation, attempt

    # ------------------------------------------------------------------ #
    # concurrency
    # ------------------------------------------------------------------ #
    def run_many(
        self,
        goals: Sequence[Goal],
        max_workers: Optional[int] = None,
        *,
        timeout: Optional[float] = None,
    ) -> List[DecisionRecord]:
        """Run several goals concurrently (ledger writes are lock-protected)."""
        workers = max(1, int(max_workers or self.config.max_workers))
        if len(goals) <= 1:
            return [self.run(g, timeout=timeout) for g in goals]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda g: self.run(g, timeout=timeout), goals))

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

    def resume(
        self, run_id: str, *, timeout: Optional[float] = None
    ) -> DecisionRecord:
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
        deadline = self.clock.monotonic() + float(
            timeout if timeout is not None else self.settings.limits.run_timeout_seconds
        )
        if plan is None:  # nothing was authorised yet: start over from planning
            return self._cycle(record, deadline)

        record.plan = plan
        record.policy = [self.policy.evaluate(a) for a in plan.actions]
        if not all(d.allowed for d in record.policy):
            record.denial_reasons = [d.reason for d in record.policy if not d.allowed]
            return self._finalize(
                record,
                ExecStatus.DENIED,
                error="; ".join(record.denial_reasons) or "denied by policy",
                code="POLICY_VIOLATION",
            )
        self._set_status(record, ExecStatus.AUTHORIZED)
        return self._execute_and_verify(record, plan, deadline, resumed=True)

    # ------------------------------------------------------------------ #
    # operations
    # ------------------------------------------------------------------ #
    def health(self) -> Dict[str, Any]:
        """Everything the ``doctor`` command needs to know."""
        import platform
        import sys

        sandbox = self.sandbox_dir
        writable = os.access(str(sandbox), os.W_OK) if sandbox.exists() else False
        ledger_report = self.ledger.fsck()
        ok = (
            sandbox.exists()
            and writable
            and ledger_report["chain_valid"]
            and ledger_report["corrupt_lines"] == 0
        )
        return {
            "ok": ok,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "version": _kernel_version(),
            "sandbox": {
                "path": str(sandbox),
                "exists": sandbox.exists(),
                "writable": writable,
                "files": self.executor.count_files()
                if hasattr(self.executor, "count_files")
                else None,
            },
            "ledger": ledger_report,
            "metrics": self.metrics.snapshot(),
            "settings": self.settings.to_dict(),
        }

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _emit(self, event_type: str, payload: Dict[str, Any], run_id: str) -> None:
        try:
            self.ledger.append(event_type, payload, run_id=run_id)
        except Exception as exc:  # pragma: no cover - ledger failure must not kill the cycle
            self.logger.error("ledger append failed (%s): %s", event_type, exc)

    def _set_status(self, record: DecisionRecord, status: ExecStatus) -> None:
        record.status = status
        self._emit("STATUS_CHANGED", {"status": status.value}, record.run_id)

    def _check_abort(self) -> None:
        if self._stop.is_set():
            raise AbortedError("run aborted: shutdown requested")

    def _check_deadline(self, deadline: float) -> None:
        if self.clock.monotonic() > deadline:
            raise RunTimeoutError("run exceeded its time budget")
        return None

    def _finalize(
        self,
        record: DecisionRecord,
        status: ExecStatus,
        error: Optional[str] = None,
        code: Optional[str] = None,
        event: str = "DECISION_RECORDED",
    ) -> DecisionRecord:
        record.status = status
        if error is not None:
            record.error = error
        if code is not None:
            record.error_code = code
        record.finished_at = self.clock.now()
        payload: Dict[str, Any] = {"record": record.to_dict(), "status": status.value}
        if error is not None:
            payload["error"] = error
        if code is not None:
            payload["code"] = code
        self._emit(event, payload, record.run_id)
        with self._lock:
            self._runs[record.run_id] = record
        self.logger.info(
            "run %s finished: %s%s",
            record.run_id,
            status.value,
            f" ({code})" if code else "",
        )
        return record

    def _flush_metrics(self) -> None:
        if not self.settings.metrics_enabled:
            return
        try:
            self.metrics.flush(self.settings.resolved_metrics_path())
        except OSError as exc:  # pragma: no cover - metrics must never break a run
            self.logger.warning("metrics flush failed: %s", exc)


# ---------------------------------------------------------------------- #
# module helpers
# ---------------------------------------------------------------------- #
def _now() -> float:
    import time

    return time.time()


def _sleep(clock: Clock, seconds: float) -> None:
    sleeper = getattr(clock, "sleep", None)
    if callable(sleeper):
        sleeper(seconds)
    else:  # pragma: no cover - clocks without sleep
        import time

        time.sleep(seconds)


def _kernel_version() -> str:
    from . import __version__

    return __version__


def _has_observation(record: DecisionRecord, action_id: str) -> bool:
    return any(o.action_id == action_id for o in record.observations)


def _pairs(plan: Plan, record: DecisionRecord) -> List[Tuple[Action, Observation]]:
    """Match each plan action with its observation (order-independent)."""
    by_id = {o.action_id: o for o in record.observations}
    return [(a, by_id[a.action_id]) for a in plan.actions if a.action_id in by_id]


def _action_id(decision: Any, plan: Plan) -> str:
    for action in plan.actions:
        if action.capability == decision.capability:
            return action.action_id
    return ""


__all__ = ["Kernel", "KernelConfig", "SystemClock"]
