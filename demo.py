#!/usr/bin/env python3
"""AGI Kernel demo: runs the whole cognitive loop end to end.

    python3 demo.py                 # throw-away sandbox in /tmp
    python3 demo.py --sandbox ./sb  # keep the sandbox so you can inspect it

Scenarios:
    1. happy path          -> VERIFIED
    2. path traversal      -> DENIED
    3. lying executor      -> INCONCLUSIVE (evidence catches it)
    4. unknown goal type   -> FAILED (the kernel does not crash)
    5. interrupted run     -> resumed and VERIFIED
    6. ledger replay       -> state rebuilt from events, chain verified
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import (  # noqa: E402
    ExecStatus,
    Executor,
    Goal,
    Kernel,
    Observation,
    SequentialPlanner,
    __version__,
)

RULE = "=" * 74


def banner(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


class LyingExecutor(Executor):
    """Executor that claims success while doing nothing (adversarial test)."""

    def execute(self, action, timeout=None):  # type: ignore[override]
        return Observation(
            capability=action.capability,
            action_id=action.action_id,
            ok=True,  # <-- the lie
            data={"path": action.params.get("path", ""), "sha256": "deadbeef" * 8},
        )


class CrashingExecutor(Executor):
    """Fails once (simulating a process crash), then behaves normally."""

    def __init__(self, sandbox_dir, fail_on: int = 1):
        super().__init__(sandbox_dir)
        self.calls = 0
        self.fail_on = fail_on

    def execute(self, action, timeout=None):  # type: ignore[override]
        self.calls += 1
        if self.calls == self.fail_on:
            raise KeyboardInterrupt("simulated crash")
        return super().execute(action, timeout=timeout)


def show(record, label: str) -> None:
    status = record.status.value
    print(f"\n[{label}] status = {status}   (run={record.run_id}, {record.duration_ms:.1f} ms)")
    if record.plan:
        print(f"    plan      : {record.plan.plan_id}  risk={record.plan.total_risk:.2f} "
              f"cost={record.plan.estimated_cost:.2f}  steps={len(record.plan.actions)}")
    for decision in record.policy:
        print(f"    policy    : {decision.capability:<18} allowed={decision.allowed} "
              f"risk={decision.risk:.2f} ({decision.risk_band})")
    for evidence in record.evidence:
        checks = "  ".join(f"{k}={v}" for k, v in evidence.checks.items())
        print(f"    evidence  : {evidence.capability:<18} passed={evidence.passed}")
        print(f"                {checks}")
    if record.error:
        print(f"    error     : {record.error}")


def main() -> int:
    parser = argparse.ArgumentParser(description="AGI Kernel vertical slice demo")
    parser.add_argument("--sandbox", default=None, help="sandbox directory (default: temp dir)")
    parser.add_argument("--keep", action="store_true", help="keep the sandbox directory")
    args = parser.parse_args()

    tmp = None
    if args.sandbox:
        sandbox = Path(args.sandbox).resolve()
        sandbox.mkdir(parents=True, exist_ok=True)
        ledger_path = sandbox.parent / f"{sandbox.name}-ledger" / "events.jsonl"
    else:
        tmp = tempfile.mkdtemp(prefix="agi-sandbox-")
        sandbox = Path(tmp) / "sandbox"
        ledger_path = Path(tmp) / "ledger" / "events.jsonl"

    print(f"AGI Kernel v{__version__}")
    print(f"sandbox : {sandbox}")
    print(f"ledger  : {ledger_path}")

    kernel = Kernel(sandbox, ledger_path)

    # ---------------------------------------------------------------- 1
    banner("1) HAPPY PATH -- Goal(create_file) -> VERIFIED")
    record = kernel.run(
        Goal("create_file", {"path": "reports/hello.txt", "content": "hello agi\n"})
    )
    show(record, "create_file")
    written = sandbox / "reports" / "hello.txt"
    print(f"    on disk   : {written} -> {written.read_text()!r}")

    # ---------------------------------------------------------------- 2
    banner("2) PATH TRAVERSAL -- Goal(path='../../OUTSIDE.txt') -> DENIED")
    denied = kernel.run(
        Goal("create_file", {"path": "../../OUTSIDE_SANDBOX.txt", "content": "nope"})
    )
    show(denied, "traversal")
    escaped = (sandbox.parent.parent / "OUTSIDE_SANDBOX.txt")
    print(f"    outside file created? {escaped.exists()}  (must be False)")

    # ---------------------------------------------------------------- 3
    banner("3) ADVERSARIAL EXECUTOR -- claims success, writes nothing")
    liar_kernel = Kernel(
        sandbox,
        sandbox / "ledger" / "adversarial.jsonl",
        planner=SequentialPlanner(),
        executor=LyingExecutor(sandbox),
    )
    lied = liar_kernel.run(
        Goal("create_file", {"path": "reports/lie.txt", "content": "never written"})
    )
    show(lied, "lying executor")
    print(f"    file on disk? {(sandbox / 'reports' / 'lie.txt').exists()}  (must be False)")

    # ---------------------------------------------------------------- 4
    banner("4) UNKNOWN GOAL TYPE -- must not raise")
    unknown = kernel.run(Goal("launch_missiles", {"target": "nowhere"}))
    show(unknown, "unknown goal")

    # ---------------------------------------------------------------- 5
    banner("5) INTERRUPTED RUN -> resume()")
    run_id = "run_resumable"
    crasher = CrashingExecutor(sandbox, fail_on=1)
    crash_kernel = Kernel(
        sandbox,
        sandbox / "ledger" / "crash.jsonl",
        planner=SequentialPlanner(),
        executor=crasher,
    )
    goal = Goal("create_file", {"path": "reports/resumed.txt", "content": "resumed ok\n"})
    try:
        crash_kernel.run(goal, run_id=run_id)
    except KeyboardInterrupt as exc:
        print(f"    process crashed mid-run: {exc}")
    partial = crash_kernel.replay().runs.get(run_id, {})
    print(f"    ledger knows: status={partial.get('status')} events={partial.get('events')}")
    resumed = crash_kernel.resume(run_id)
    show(resumed, "resumed")
    print(f"    on disk   : {(sandbox / 'reports' / 'resumed.txt').read_text()!r}")

    # ---------------------------------------------------------------- 6
    banner("6) LEDGER REPLAY")
    state = kernel.replay()
    print(f"    events={len(state.events)}  runs={len(state.runs)}  chain_valid={state.chain_valid}")
    for rid, info in state.runs.items():
        print(f"    - {rid}: status={info.get('status')} events={info.get('events')} "
              f"goal={info.get('goal', {}).get('goal_type')}")
    print("    event timeline (first 12):")
    for event in state.events[:12]:
        print(f"      #{event['seq']:<3} {event['type']}")

    banner("SUMMARY")
    outcomes = {
        "happy path": record.status,
        "path traversal": denied.status,
        "lying executor": lied.status,
        "unknown goal": unknown.status,
        "resumed run": resumed.status,
    }
    for name, status in outcomes.items():
        print(f"    {name:<16} -> {status.value}")
    ok = (
        record.status is ExecStatus.VERIFIED
        and denied.status is ExecStatus.DENIED
        and lied.status is ExecStatus.INCONCLUSIVE
        and unknown.status is ExecStatus.FAILED
        and resumed.status is ExecStatus.VERIFIED
        and state.chain_valid
        and not escaped.exists()
    )
    print(f"\nALL EXPECTATIONS MET: {ok}")

    if tmp and not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
