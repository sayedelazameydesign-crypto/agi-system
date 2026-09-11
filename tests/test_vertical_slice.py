"""Unit tests for the AGI Kernel vertical slice.

Portable: no hardcoded paths, everything lives in ``tempfile`` directories, so
the suite runs unchanged on Linux, macOS and Windows.

    python3 -m unittest discover -s tests -v
    python3 tests/test_vertical_slice.py          # also works
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import (  # noqa: E402
    Action,
    EventLedger,
    ExecStatus,
    Executor,
    Goal,
    Kernel,
    Observation,
    Plan,
    PlanningError,
    PolicyEngine,
    SequentialPlanner,
    Verifier,
)


# --------------------------------------------------------------------------- #
# test doubles
# --------------------------------------------------------------------------- #
class LyingExecutor(Executor):
    """Claims success without touching the filesystem."""

    def execute(self, action, timeout=None):  # type: ignore[override]
        return Observation(
            capability=action.capability,
            action_id=action.action_id,
            ok=True,
            data={"path": action.params.get("path", ""), "sha256": "0" * 64},
        )


class TamperingExecutor(Executor):
    """Writes content that differs from what the goal asked for."""

    def _write(self, action, path):  # type: ignore[override]
        action = Action(
            capability=action.capability,
            params={**action.params, "content": "TAMPERED"},
            action_id=action.action_id,
        )
        return super()._write(action, path)


class ExplodingExecutor(Executor):
    """Raises instead of returning an Observation."""

    def execute(self, action, timeout=None):  # type: ignore[override]
        raise RuntimeError("boom")


class CrashingExecutor(Executor):
    """Raises ``KeyboardInterrupt`` once (simulates a killed process)."""

    def __init__(self, sandbox_dir, fail_on: int = 1):
        super().__init__(sandbox_dir)
        self.calls = 0
        self.fail_on = fail_on

    def execute(self, action, timeout=None):  # type: ignore[override]
        self.calls += 1
        if self.calls == self.fail_on:
            raise KeyboardInterrupt("simulated crash")
        return super().execute(action, timeout=timeout)


# --------------------------------------------------------------------------- #
# base fixture
# --------------------------------------------------------------------------- #
class KernelTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agi-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sandbox = self.tmp / "sandbox"
        self.ledger_path = self.tmp / "ledger" / "events.jsonl"
        self.kernel = Kernel(self.sandbox, self.ledger_path)

    # helpers ------------------------------------------------------------- #
    def kernel_with(self, executor=None, planner=None, name="alt"):
        return Kernel(
            self.sandbox,
            self.tmp / "ledger" / f"{name}.jsonl",
            planner=planner or SequentialPlanner(),
            executor=executor or Executor(self.sandbox),
        )

    def run_goal(self, goal, **kwargs):
        return self.kernel.run(goal, **kwargs)


# --------------------------------------------------------------------------- #
# happy path & evidence
# --------------------------------------------------------------------------- #
class TestHappyPath(KernelTestCase):
    def test_create_file_is_verified(self):
        record = self.run_goal(
            Goal("create_file", {"path": "reports/a.txt", "content": "hello"})
        )
        self.assertIs(record.status, ExecStatus.VERIFIED)
        self.assertTrue(record.evidence_passed)
        self.assertEqual(len(record.evidence), 1)
        checks = record.evidence[0].checks
        for key in ("claimed_ok", "file_exists", "correct_path", "correct_content",
                    "hash_matches"):
            self.assertTrue(checks[key], f"check {key} failed")
        self.assertEqual((self.sandbox / "reports" / "a.txt").read_text(), "hello")

    def test_decision_record_is_json_serialisable(self):
        record = self.run_goal(Goal("create_file", {"path": "a.txt", "content": "x"}))
        payload = json.dumps(record.to_dict())  # must not raise
        self.assertIn("VERIFIED", payload)

    def test_parent_directories_are_created(self):
        record = self.run_goal(
            Goal("create_file", {"path": "deep/nested/dir/f.txt", "content": "x"})
        )
        self.assertIs(record.status, ExecStatus.VERIFIED)
        self.assertTrue((self.sandbox / "deep/nested/dir/f.txt").is_file())

    def test_status_sequence_is_recorded(self):
        record = self.run_goal(Goal("create_file", {"path": "s.txt", "content": "x"}))
        types = [e["type"] for e in self.kernel.replay().events_for(record.run_id)]
        for expected in ("GOAL_RECEIVED", "PLANS_PROPOSED", "PLAN_SELECTED",
                         "EXECUTION_START", "OBSERVATION", "EVIDENCE",
                         "DECISION_RECORDED"):
            self.assertIn(expected, types)
        self.assertIs(record.status, ExecStatus.VERIFIED)


# --------------------------------------------------------------------------- #
# policy & sandbox
# --------------------------------------------------------------------------- #
class TestPolicy(KernelTestCase):
    def test_path_traversal_is_denied(self):
        record = self.run_goal(
            Goal("create_file", {"path": "../../OUTSIDE_SANDBOX.txt", "content": "x"})
        )
        self.assertIs(record.status, ExecStatus.DENIED)
        self.assertFalse((self.tmp.parent / "OUTSIDE_SANDBOX.txt").exists())
        self.assertIsNone(record.plan)
        events = [e["type"] for e in self.kernel.replay().events_for(record.run_id)]
        self.assertIn("POLICY_DENIED", events)
        self.assertNotIn("EXECUTION_START", events)

    def test_absolute_path_outside_sandbox_is_denied(self):
        outside = self.tmp / "outside.txt"
        record = self.run_goal(
            Goal("create_file", {"path": str(outside), "content": "x"})
        )
        self.assertIs(record.status, ExecStatus.DENIED)
        self.assertFalse(outside.exists())

    def test_unknown_capability_is_denied(self):
        decision = self.kernel.policy.evaluate(
            Action("process.execute", {"path": "x", "cmd": "rm -rf /"})
        )
        self.assertFalse(decision.allowed)
        self.assertIn("allowlist", decision.reason)

    def test_writing_the_sandbox_root_is_denied(self):
        record = self.run_goal(Goal("create_file", {"path": ".", "content": "x"}))
        self.assertIs(record.status, ExecStatus.DENIED)

    def test_delete_escapes_are_denied(self):
        victim = self.tmp / "victim.txt"
        victim.write_text("do not delete me")
        record = self.run_goal(Goal("delete_file", {"path": str(victim)}))
        self.assertIs(record.status, ExecStatus.DENIED)
        self.assertTrue(victim.exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlink_escape_is_denied(self):
        outside_dir = self.tmp / "outside"
        outside_dir.mkdir()
        link = self.sandbox / "escape"
        try:
            os.symlink(outside_dir, link)
        except (OSError, NotImplementedError):  # pragma: no cover
            self.skipTest("cannot create symlinks in this environment")
        record = self.run_goal(
            Goal("create_file", {"path": "escape/pwned.txt", "content": "x"})
        )
        self.assertIs(record.status, ExecStatus.DENIED)
        self.assertFalse((outside_dir / "pwned.txt").exists())

    def test_contains_helper(self):
        self.assertTrue(self.kernel.policy.contains("a/b.txt"))
        self.assertFalse(self.kernel.policy.contains("../a.txt"))


# --------------------------------------------------------------------------- #
# risk model
# --------------------------------------------------------------------------- #
class TestRiskModel(KernelTestCase):
    def test_risk_is_not_constant(self):
        policy = self.kernel.policy
        read = policy.risk(Action("filesystem.read", {"path": "a.txt"}))
        write = policy.risk(Action("filesystem.write", {"path": "a.txt", "content": "x"}))
        delete = policy.risk(Action("filesystem.delete", {"path": "a.txt"}))
        self.assertLess(read, write)
        self.assertLess(write, delete)

    def test_risk_bands(self):
        decision = self.kernel.policy.evaluate(
            Action("filesystem.read", {"path": "a.txt"})
        )
        self.assertEqual(decision.risk_band, "low")
        self.assertGreater(decision.risk, 0.0)

    def test_planner_sets_a_risk_prior(self):
        goal = Goal("create_file", {"path": "a.txt", "content": "x" * 5000})
        plans = SequentialPlanner().propose(goal)
        self.assertTrue(all(a.predicted_risk > 0.0 for p in plans for a in p.actions))


# --------------------------------------------------------------------------- #
# failure handling (no crashes)
# --------------------------------------------------------------------------- #
class TestFailureModes(KernelTestCase):
    def test_unknown_goal_type_fails_cleanly(self):
        record = self.run_goal(Goal("launch_missiles", {"target": "x"}))
        self.assertIs(record.status, ExecStatus.FAILED)
        self.assertIsNotNone(record.error)

    def test_missing_required_param_fails_cleanly(self):
        record = self.run_goal(Goal("create_file", {"content": "no path"}))
        self.assertIs(record.status, ExecStatus.FAILED)
        self.assertIn("path", record.error or "")

    def test_lying_executor_is_not_verified(self):
        kernel = self.kernel_with(LyingExecutor(self.sandbox), name="liar")
        record = kernel.run(Goal("create_file", {"path": "lie.txt", "content": "x"}))
        self.assertIs(record.status, ExecStatus.INCONCLUSIVE)
        self.assertFalse((self.sandbox / "lie.txt").exists())
        self.assertIn("file_exists", record.evidence[0].failed_checks)

    def test_tampered_content_is_detected(self):
        kernel = self.kernel_with(TamperingExecutor(self.sandbox), name="tamper")
        record = kernel.run(Goal("create_file", {"path": "t.txt", "content": "expected"}))
        self.assertIs(record.status, ExecStatus.INCONCLUSIVE)
        self.assertIn("hash_matches", record.evidence[0].failed_checks)

    def test_executor_exception_does_not_escape_the_kernel(self):
        kernel = self.kernel_with(ExplodingExecutor(self.sandbox), name="boom")
        record = kernel.run(Goal("create_file", {"path": "b.txt", "content": "x"}))
        self.assertIs(record.status, ExecStatus.FAILED)
        self.assertIn("boom", record.error or "")


# --------------------------------------------------------------------------- #
# capabilities
# --------------------------------------------------------------------------- #
class TestCapabilities(KernelTestCase):
    def test_ensure_dir(self):
        record = self.run_goal(Goal("ensure_dir", {"path": "new/dir"}))
        self.assertIs(record.status, ExecStatus.VERIFIED)
        self.assertTrue((self.sandbox / "new/dir").is_dir())

    def test_write_read_list_delete(self):
        self.assertIs(
            self.run_goal(
                Goal("create_file", {"path": "d/x.txt", "content": "payload"})
            ).status,
            ExecStatus.VERIFIED,
        )
        read = self.run_goal(
            Goal("read_file", {"path": "d/x.txt", "expected_content": "payload"})
        )
        self.assertIs(read.status, ExecStatus.VERIFIED)
        self.assertEqual(read.observations[0].data["content"], "payload")

        listed = self.run_goal(Goal("list_dir", {"path": "d"}))
        self.assertIs(listed.status, ExecStatus.VERIFIED)
        self.assertIn("x.txt", listed.observations[0].data["entries"])

        deleted = self.run_goal(Goal("delete_file", {"path": "d/x.txt"}))
        self.assertIs(deleted.status, ExecStatus.VERIFIED)
        self.assertFalse((self.sandbox / "d/x.txt").exists())

    def test_read_missing_file_fails(self):
        record = self.run_goal(Goal("read_file", {"path": "nope.txt"}))
        self.assertIs(record.status, ExecStatus.FAILED)

    def test_verifier_refuses_paths_outside_sandbox(self):
        verifier = Verifier(self.sandbox)
        action = Action("filesystem.write", {"path": "../outside.txt", "content": "x"})
        evidence = verifier.verify(
            action,
            Observation(capability=action.capability, action_id=action.action_id, ok=True,
                        data={"path": "../outside.txt"}),
        )
        self.assertFalse(evidence.passed)


# --------------------------------------------------------------------------- #
# planner
# --------------------------------------------------------------------------- #
class TestPlanner(unittest.TestCase):
    def setUp(self):
        self.planner = SequentialPlanner()

    def test_alternatives_are_produced(self):
        goal = Goal("create_file", {"path": "a/b.txt", "content": "x"})
        plans = self.planner.propose(goal)
        self.assertGreater(len(plans), 1)
        self.assertEqual(plans[0].goal_id, goal.goal_id)
        self.assertTrue(all(isinstance(p, Plan) for p in plans))

    def test_plans_are_ordered_by_cost(self):
        plans = self.planner.propose(
            Goal("create_file", {"path": "a/b.txt", "content": "x"})
        )
        costs = [p.estimated_cost for p in plans]
        self.assertEqual(costs, sorted(costs))

    def test_unsupported_goal_raises_planning_error(self):
        with self.assertRaises(PlanningError):
            self.planner.propose(Goal("does_not_exist", {}))

    def test_missing_param_raises_planning_error(self):
        with self.assertRaises(PlanningError):
            self.planner.propose(Goal("create_file", {"content": "x"}))

    def test_delete_goal_has_verify_first_alternative(self):
        plans = self.planner.propose(Goal("delete_file", {"path": "a.txt"}))
        self.assertIn("filesystem.read", plans[0].capabilities)


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #
class TestLedger(KernelTestCase):
    def test_replay_reconstructs_run(self):
        record = self.run_goal(Goal("create_file", {"path": "r.txt", "content": "x"}))
        state = self.kernel.replay()
        self.assertTrue(state.chain_valid)
        self.assertIn(record.run_id, state.runs)
        self.assertEqual(state.runs[record.run_id]["status"], "VERIFIED")
        self.assertEqual(
            state.runs[record.run_id]["goal"]["goal_type"], "create_file"
        )

    def test_tampering_is_detected(self):
        self.run_goal(Goal("create_file", {"path": "r.txt", "content": "x"}))
        lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[0])
        event["payload"] = {"goal": {"goal_type": "HACKED"}}
        lines[0] = json.dumps(event, sort_keys=True)
        self.ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertFalse(EventLedger(self.ledger_path).verify_chain())

    def test_concurrent_appends_stay_consistent(self):
        ledger = EventLedger(self.tmp / "concurrent.jsonl")
        errors: list = []

        def worker(n: int) -> None:
            try:
                for i in range(25):
                    ledger.append("TEST_EVENT", {"n": n, "i": i}, run_id=f"run-{n}")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        events = ledger.read_all()
        self.assertEqual(len(events), 200)
        self.assertEqual(len({e["seq"] for e in events}), 200)
        self.assertTrue(ledger.verify_chain(events))

    def test_ledger_is_append_only(self):
        self.run_goal(Goal("create_file", {"path": "r.txt", "content": "x"}))
        before = self.ledger_path.read_text(encoding="utf-8")
        self.run_goal(Goal("create_file", {"path": "r2.txt", "content": "y"}))
        after = self.ledger_path.read_text(encoding="utf-8")
        self.assertTrue(after.startswith(before))

    def test_clear_resets_state(self):
        self.run_goal(Goal("create_file", {"path": "r.txt", "content": "x"}))
        self.kernel.ledger.clear()
        self.assertEqual(len(self.kernel.ledger), 0)


# --------------------------------------------------------------------------- #
# state management / resume / concurrency
# --------------------------------------------------------------------------- #
class TestKernelState(KernelTestCase):
    def test_runs_are_tracked_in_memory(self):
        record = self.run_goal(Goal("create_file", {"path": "a.txt", "content": "x"}))
        self.assertIs(self.kernel.get_run(record.run_id), record)
        self.assertIn(record.run_id, self.kernel.list_runs())

    def test_resume_completes_an_interrupted_run(self):
        run_id = "run-resume-me"
        kernel = self.kernel_with(CrashingExecutor(self.sandbox, fail_on=1), name="crash")
        goal = Goal("create_file", {"path": "resume.txt", "content": "resumed"})
        with self.assertRaises(KeyboardInterrupt):
            kernel.run(goal, run_id=run_id)

        partial = kernel.replay().runs[run_id]
        self.assertNotIn("record", partial)  # no terminal event -> resumable

        record = kernel.resume(run_id)
        self.assertIs(record.status, ExecStatus.VERIFIED)
        self.assertEqual((self.sandbox / "resume.txt").read_text(), "resumed")

    def test_resume_is_a_no_op_for_finished_runs(self):
        record = self.run_goal(Goal("create_file", {"path": "a.txt", "content": "x"}))
        again = self.kernel.resume(record.run_id)
        self.assertIs(again.status, ExecStatus.VERIFIED)
        self.assertEqual(again.run_id, record.run_id)

    def test_resume_of_unknown_run_raises(self):
        with self.assertRaises(LookupError):
            self.kernel.resume("run-does-not-exist")

    def test_run_many_is_thread_safe(self):
        goals = [
            Goal("create_file", {"path": f"many/f{i}.txt", "content": f"c{i}"})
            for i in range(12)
        ]
        records = self.kernel.run_many(goals, max_workers=4)
        self.assertEqual(len(records), 12)
        self.assertTrue(all(r.status is ExecStatus.VERIFIED for r in records))
        for i in range(12):
            self.assertEqual((self.sandbox / f"many/f{i}.txt").read_text(), f"c{i}")
        self.assertTrue(self.kernel.replay().chain_valid)

    def test_fresh_process_can_replay_state(self):
        record = self.run_goal(Goal("create_file", {"path": "a.txt", "content": "x"}))
        fresh = Kernel(self.sandbox, self.ledger_path)  # simulates a restart
        state = fresh.replay()
        self.assertEqual(state.runs[record.run_id]["status"], "VERIFIED")
        rebuilt = fresh.resume(record.run_id)
        self.assertIs(rebuilt.status, ExecStatus.VERIFIED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
