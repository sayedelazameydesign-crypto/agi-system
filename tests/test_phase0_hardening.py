"""Phase 0 (hardening) tests: config, errors, clock, metrics, timeouts,
retries, quotas, abort semantics, ledger operations and the CLI.

    python3 -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import (  # noqa: E402
    ConfigurationError,
    ExecStatus,
    Executor,
    FrozenClock,
    Goal,
    Kernel,
    KernelError,
    MetricsRecorder,
    MissingParameterError,
    Observation,
    PlanningError,
    SandboxViolationError,
    Settings,
    SystemClock,
    UnsupportedGoalError,
)


# --------------------------------------------------------------------------- #
# doubles
# --------------------------------------------------------------------------- #
class SlowExecutor(Executor):
    """Sleeps inside the *handlers*, so the kernel's timeout guard applies."""

    def __init__(self, sandbox_dir, delay: float = 0.08):
        super().__init__(sandbox_dir)
        self.delay = delay

    def _write(self, action, path):  # type: ignore[override]
        time.sleep(self.delay)
        return super()._write(action, path)

    def _read(self, action, path):  # type: ignore[override]
        time.sleep(self.delay)
        return super()._read(action, path)

    def _delete(self, action, path):  # type: ignore[override]
        time.sleep(self.delay)
        return super()._delete(action, path)


class FlakyExecutor(Executor):
    """Fails the first ``fail_times`` calls, then behaves normally."""

    def __init__(self, sandbox_dir, fail_times: int = 1):
        super().__init__(sandbox_dir)
        self.calls = 0
        self.fail_times = fail_times

    def execute(self, action, timeout=None):  # type: ignore[override]
        self.calls += 1
        if self.calls <= self.fail_times:
            return Observation(
                capability=action.capability,
                action_id=action.action_id,
                ok=False,
                data={"path": action.params.get("path", ""), "error_code": "TRANSIENT"},
                error="transient failure",
            )
        return super().execute(action, timeout=timeout)


class KeyboardExecutor(Executor):
    def execute(self, action, timeout=None):  # type: ignore[override]
        raise KeyboardInterrupt("simulated crash")


class CrashOnceExecutor(Executor):
    """Simulates a process killed mid-run: fails once, then behaves normally."""

    def __init__(self, sandbox_dir):
        super().__init__(sandbox_dir)
        self.calls = 0

    def execute(self, action, timeout=None):  # type: ignore[override]
        self.calls += 1
        if self.calls == 1:
            raise KeyboardInterrupt("simulated crash")
        return super().execute(action, timeout=timeout)


# --------------------------------------------------------------------------- #
# base
# --------------------------------------------------------------------------- #
class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agi-p0-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sandbox = self.tmp / "sandbox"
        self.ledger = self.tmp / "ledger" / "events.jsonl"

    def settings(self, **overrides) -> Settings:
        """Build settings, routing nested limits/retry fields automatically."""
        settings = Settings(
            sandbox_dir=self.sandbox,
            ledger_path=self.ledger,
            metrics_path=self.tmp / "metrics.json",
        )
        for key, value in overrides.items():
            if hasattr(settings.limits, key):
                setattr(settings.limits, key, value)
            elif hasattr(settings.retry, key):
                setattr(settings.retry, key, value)
            elif hasattr(settings, key):
                setattr(settings, key, value)
            else:  # pragma: no cover - guard against typos in tests
                raise AttributeError(f"unknown setting: {key}")
        return settings.validate()

    def kernel(self, **kwargs) -> Kernel:
        executor = kwargs.pop("executor", None)
        clock = kwargs.pop("clock", None)
        settings = kwargs.pop("settings", None) or self.settings(**kwargs)
        # executor=None -> the kernel builds one wired with the settings'
        # timeouts and quotas
        return Kernel.from_settings(settings, executor=executor, clock=clock)


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
class TestSettings(Base):
    def test_defaults_are_validated(self):
        settings = self.settings()
        self.assertEqual(settings.schema_version, "1.0")
        self.assertTrue(settings.allowlist)
        self.assertIn("filesystem.write", settings.allowlist)

    def test_ledger_lives_outside_the_sandbox(self):
        settings = self.settings()
        self.assertNotIn(str(self.sandbox), str(settings.resolved_ledger_path()))

    def test_load_from_json_file(self):
        path = self.tmp / "agi-kernel.json"
        path.write_text(
            json.dumps(
                {
                    "sandbox_dir": str(self.sandbox),
                    "ledger_path": str(self.ledger),
                    "max_risk": 0.5,
                    "limits": {"action_timeout_seconds": 3.5},
                    "retry": {"max_attempts": 2},
                }
            ),
            encoding="utf-8",
        )
        settings = Settings.load(path)
        self.assertEqual(settings.max_risk, 0.5)
        self.assertEqual(settings.limits.action_timeout_seconds, 3.5)
        self.assertEqual(settings.retry.max_attempts, 2)

    def test_environment_overrides_file(self):
        path = self.tmp / "agi-kernel.json"
        path.write_text(
            json.dumps({"sandbox_dir": str(self.sandbox), "max_risk": 0.5}),
            encoding="utf-8",
        )
        env = dict(os.environ, AGI_MAX_RISK="0.25", AGI_ACTION_TIMEOUT="9.5")
        old = os.environ.copy()
        try:
            os.environ.update(env)
            settings = Settings.load(path)
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(settings.max_risk, 0.25)
        self.assertEqual(settings.limits.action_timeout_seconds, 9.5)

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(ConfigurationError):
            Settings.from_dict({"sandbox_dir": str(self.sandbox), "nope": 1})

    def test_empty_allowlist_fails_closed(self):
        with self.assertRaises(ConfigurationError):
            self.settings().with_overrides(allowlist=[])

    def test_invalid_risk_is_rejected(self):
        with self.assertRaises(ConfigurationError):
            self.settings().with_overrides(max_risk=5.0)

    def test_invalid_timeout_is_rejected(self):
        settings = self.settings()
        settings.limits.action_timeout_seconds = 0
        with self.assertRaises(ConfigurationError):
            settings.validate()

    def test_invalid_config_file(self):
        path = self.tmp / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            Settings.load(path)

    def test_roundtrip(self):
        settings = self.settings()
        restored = Settings.from_dict(settings.to_dict())
        self.assertEqual(restored.to_dict(), settings.to_dict())


# --------------------------------------------------------------------------- #
# errors & clock
# --------------------------------------------------------------------------- #
class TestErrors(unittest.TestCase):
    def test_hierarchy(self):
        self.assertTrue(issubclass(PlanningError, KernelError))
        self.assertTrue(issubclass(UnsupportedGoalError, PlanningError))
        self.assertTrue(issubclass(MissingParameterError, PlanningError))
        self.assertTrue(issubclass(SandboxViolationError, KernelError))

    def test_codes_are_stable(self):
        self.assertEqual(SandboxViolationError("x").code, "SANDBOX_VIOLATION")
        self.assertEqual(UnsupportedGoalError("x").code, "UNSUPPORTED_GOAL")
        self.assertEqual(MissingParameterError("x").code, "MISSING_PARAMETER")

    def test_error_payload(self):
        error = SandboxViolationError("escaped", path="../x")
        self.assertEqual(error.to_dict()["context"]["path"], "../x")


class TestClock(unittest.TestCase):
    def test_system_clock_advances(self):
        clock = SystemClock()
        self.assertGreaterEqual(clock.monotonic(), 0.0)
        self.assertLessEqual(abs(clock.now() - time.time()), 5.0)

    def test_frozen_clock_is_deterministic(self):
        clock = FrozenClock(1000.0)
        first = clock.now()
        self.assertEqual(first, clock.now())
        clock.advance(5)
        self.assertEqual(clock.now(), 1005.0)
        self.assertEqual(clock.monotonic(), 1005.0)


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
class TestMetrics(Base):
    def test_counters_and_durations(self):
        metrics = MetricsRecorder()
        metrics.inc("a")
        metrics.inc("a", 2)
        metrics.gauge("g", 3.5)
        metrics.observe("run", 0.5)
        metrics.observe("run", 1.5)
        snap = metrics.snapshot()
        self.assertEqual(snap["counters"]["a"], 3.0)
        self.assertEqual(snap["gauges"]["g"], 3.5)
        self.assertEqual(snap["durations"]["run"]["count"], 2)
        self.assertEqual(snap["durations"]["run"]["avg_ms"], 1000.0)

    def test_flush_writes_json(self):
        metrics = MetricsRecorder()
        metrics.inc("runs_total")
        target = metrics.flush(self.tmp / "metrics.json")
        self.assertIsNotNone(target)
        self.assertEqual(json.loads(target.read_text())["counters"]["runs_total"], 1.0)

    def test_kernel_records_metrics(self):
        kernel = self.kernel()
        kernel.run(Goal("create_file", {"path": "a.txt", "content": "x"}))
        counters = kernel.metrics.counters
        self.assertEqual(counters["runs_total"], 1.0)
        self.assertEqual(counters["runs_verified"], 1.0)
        self.assertEqual(counters["actions_total"], 1.0)
        self.assertTrue((self.tmp / "metrics.json").exists())


# --------------------------------------------------------------------------- #
# executor hardening
# --------------------------------------------------------------------------- #
class TestExecutorHardening(Base):
    def test_action_timeout(self):
        kernel = self.kernel(executor=SlowExecutor(self.sandbox, delay=0.15),
                             action_timeout_seconds=0.01)
        record = kernel.run(Goal("create_file", {"path": "slow.txt", "content": "x"}))
        self.assertIs(record.status, ExecStatus.FAILED)
        self.assertEqual(record.error_code, "EXECUTION_TIMEOUT")
        self.assertIn("timeout", (record.error or "").lower())

    def test_atomic_write_leaves_no_temp_files(self):
        kernel = self.kernel()
        record = kernel.run(Goal("create_file", {"path": "a.txt", "content": "payload"}))
        self.assertIs(record.status, ExecStatus.VERIFIED)
        leftovers = list(self.sandbox.glob(".*tmp*"))
        self.assertEqual(leftovers, [])
        self.assertEqual((self.sandbox / "a.txt").read_text(), "payload")

    def test_file_quota(self):
        kernel = self.kernel(max_sandbox_files=1)
        first = kernel.run(Goal("create_file", {"path": "one.txt", "content": "1"}))
        second = kernel.run(Goal("create_file", {"path": "two.txt", "content": "2"}))
        self.assertIs(first.status, ExecStatus.VERIFIED)
        self.assertIs(second.status, ExecStatus.FAILED)
        self.assertEqual(second.error_code, "QUOTA_EXCEEDED")

    def test_retry_budget_recovers_transient_failure(self):
        settings = self.settings()
        settings.retry.max_attempts = 3
        settings.retry.backoff_seconds = 0.0
        kernel = Kernel.from_settings(
            settings, executor=FlakyExecutor(self.sandbox, fail_times=2)
        )
        record = kernel.run(Goal("create_file", {"path": "retry.txt", "content": "x"}))
        self.assertIs(record.status, ExecStatus.VERIFIED)
        self.assertEqual(kernel.metrics.counters.get("action_retries"), 2.0)

    def test_retries_are_off_by_default(self):
        kernel = self.kernel(executor=FlakyExecutor(self.sandbox, fail_times=1))
        record = kernel.run(Goal("create_file", {"path": "noretry.txt", "content": "x"}))
        self.assertIs(record.status, ExecStatus.FAILED)
        self.assertIsNone(kernel.metrics.counters.get("action_retries"))

    def test_base_exception_is_not_swallowed(self):
        kernel = self.kernel(executor=KeyboardExecutor(self.sandbox))
        with self.assertRaises(KeyboardInterrupt):
            kernel.run(Goal("create_file", {"path": "x.txt", "content": "x"}))


# --------------------------------------------------------------------------- #
# kernel lifecycle
# --------------------------------------------------------------------------- #
class TestKernelLifecycle(Base):
    def test_run_timeout(self):
        kernel = self.kernel(
            executor=SlowExecutor(self.sandbox, delay=0.05),
            run_timeout_seconds=0.01,
        )
        (self.sandbox / "victim.txt").write_text("x")
        record = kernel.run(Goal("delete_file", {"path": "victim.txt"}))
        self.assertIs(record.status, ExecStatus.FAILED)
        self.assertEqual(record.error_code, "RUN_TIMEOUT")

    def test_cooperative_abort(self):
        kernel = self.kernel()
        kernel.request_stop("test")
        record = kernel.run(Goal("create_file", {"path": "a.txt", "content": "x"}))
        self.assertIs(record.status, ExecStatus.ABORTED)
        self.assertEqual(record.error_code, "ABORTED")
        self.assertFalse((self.sandbox / "a.txt").exists())
        types = [e["type"] for e in kernel.replay().events_for(record.run_id)]
        self.assertIn("RUN_ABORTED", types)

    def test_service_recovers_after_abort(self):
        kernel = self.kernel()
        kernel.request_stop("test")
        aborted = kernel.run(Goal("create_file", {"path": "a.txt", "content": "x"}))
        self.assertIs(aborted.status, ExecStatus.ABORTED)

        kernel.resume_operations()
        self.assertFalse(kernel.stopping)
        recovered = kernel.run(Goal("create_file", {"path": "a.txt", "content": "x"}))
        self.assertIs(recovered.status, ExecStatus.VERIFIED)

    def test_resume_replays_a_crashed_run(self):
        kernel = self.kernel(executor=CrashOnceExecutor(self.sandbox))
        with self.assertRaises(KeyboardInterrupt):
            kernel.run(Goal("create_file", {"path": "a.txt", "content": "x"}),
                       run_id="crash-1")
        resumed = kernel.resume("crash-1")
        self.assertIs(resumed.status, ExecStatus.VERIFIED)

    def test_health_report(self):
        kernel = self.kernel()
        kernel.run(Goal("create_file", {"path": "a.txt", "content": "x"}))
        health = kernel.health()
        self.assertTrue(health["ok"])
        self.assertTrue(health["sandbox"]["exists"])
        self.assertTrue(health["ledger"]["chain_valid"])
        self.assertIn("counters", health["metrics"])

    def test_error_codes_on_records(self):
        kernel = self.kernel()
        denied = kernel.run(Goal("create_file", {"path": "../out.txt", "content": "x"}))
        self.assertEqual(denied.error_code, "POLICY_VIOLATION")
        failed = kernel.run(Goal("nope", {}))
        self.assertEqual(failed.error_code, "UNSUPPORTED_GOAL")

    def test_missing_param_error_code(self):
        kernel = self.kernel()
        record = kernel.run(Goal("create_file", {"content": "no path"}))
        self.assertEqual(record.error_code, "MISSING_PARAMETER")

    def test_events_carry_schema_version(self):
        kernel = self.kernel()
        record = kernel.run(Goal("create_file", {"path": "a.txt", "content": "x"}))
        events = kernel.replay().events_for(record.run_id)
        self.assertTrue(all(e.get("v") == "1.0" for e in events))

    def test_frozen_clock_keeps_runs_deterministic(self):
        settings = self.settings()
        kernel = Kernel.from_settings(settings, clock=FrozenClock(1_000.0))
        record = kernel.run(Goal("create_file", {"path": "a.txt", "content": "x"}))
        self.assertEqual(record.duration_ms, 0.0)


# --------------------------------------------------------------------------- #
# ledger operations
# --------------------------------------------------------------------------- #
class TestLedgerOperations(Base):
    def _kernel_with_events(self) -> Kernel:
        kernel = self.kernel()
        kernel.run(Goal("create_file", {"path": "a.txt", "content": "x"}))
        kernel.run(Goal("create_file", {"path": "b.txt", "content": "y"}))
        return kernel

    def test_fsck_healthy(self):
        kernel = self._kernel_with_events()
        report = kernel.ledger.fsck()
        self.assertTrue(report["chain_valid"])
        self.assertEqual(report["corrupt_lines"], 0)
        self.assertGreater(report["events"], 0)

    def test_fsck_detects_tampering(self):
        kernel = self._kernel_with_events()
        lines = self.ledger.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[0])
        event["payload"] = {"goal": {"goal_type": "HACKED"}}
        lines[0] = json.dumps(event, sort_keys=True)
        self.ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
        report = kernel.ledger.fsck()
        self.assertFalse(report["chain_valid"])
        self.assertEqual(report["first_bad_seq"], 0)

    def test_quarantine_repairs_the_chain(self):
        kernel = self._kernel_with_events()
        lines = self.ledger.read_text(encoding="utf-8").splitlines()
        lines[-1] = json.dumps({"v": "1.0", "seq": 999, "type": "BROKEN", "prev": "x"})
        self.ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertFalse(kernel.ledger.fsck()["chain_valid"])

        report = kernel.ledger.quarantine()
        self.assertEqual(report["quarantined"], 1)
        self.assertTrue(kernel.ledger.fsck()["chain_valid"])
        self.assertTrue(Path(report["quarantine_path"]).exists())

    def test_snapshot_writes_json(self):
        kernel = self._kernel_with_events()
        target = kernel.ledger.snapshot(self.tmp / "snap.json")
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(data["events"], len(kernel.ledger))
        self.assertEqual(len(data["runs"]), 2)
        self.assertTrue(data["chain_valid"])

    def test_rotate_archives_and_reanchors(self):
        kernel = self._kernel_with_events()
        archived = kernel.ledger.rotate(keep=3)
        self.assertIsNotNone(archived)
        self.assertTrue(archived.exists())
        events = kernel.ledger.read_all()
        self.assertEqual(events[0]["type"], "LEDGER_ROTATED")
        self.assertTrue(kernel.ledger.fsck()["chain_valid"])

    def test_rotate_on_empty_ledger_is_a_noop(self):
        kernel = self.kernel()
        self.assertIsNone(kernel.ledger.rotate())


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
class TestCLI(Base):
    def cli(self, *args: str, expect: int = 0) -> dict:
        result = subprocess.run(
            [sys.executable, "-m", "core", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        if expect is not None:
            self.assertEqual(
                result.returncode, expect,
                msg=f"stdout={result.stdout}\nstderr={result.stderr}",
            )
        return {"stdout": result.stdout, "stderr": result.stderr, "code": result.returncode}

    def base_args(self) -> list:
        return ["--sandbox", str(self.sandbox), "--ledger", str(self.ledger), "-q"]

    def test_run_verified(self):
        out = self.cli(*self.base_args(), "run", "--goal-type", "create_file",
                       "--param", "path=cli.txt", "--param", "content=hello", "--json")
        payload = json.loads(out["stdout"])
        self.assertEqual(payload["status"], "VERIFIED")
        self.assertEqual((self.sandbox / "cli.txt").read_text(), "hello")

    def test_run_denied_exit_code(self):
        self.cli(*self.base_args(), "run", "--goal-type", "create_file",
                 "--param", "path=../../escape.txt", "--param", "content=x",
                 expect=3)

    def test_goal_file(self):
        path = self.tmp / "goal.json"
        path.write_text(json.dumps({"goal_type": "create_file",
                                    "params": {"path": "gf.txt", "content": "g"}}))
        self.cli(*self.base_args(), "run", "--goal-file", str(path), expect=0)
        self.assertEqual((self.sandbox / "gf.txt").read_text(), "g")

    def test_replay_and_verify(self):
        self.cli(*self.base_args(), "run", "--goal-type", "create_file",
                 "--param", "path=cli.txt", "--param", "content=hello", expect=0)
        self.cli(*self.base_args(), "replay", "--json", expect=0)
        self.cli(*self.base_args(), "verify", "--json", expect=0)

    def test_doctor(self):
        out = self.cli(*self.base_args(), "doctor", "--json", expect=0)
        self.assertTrue(json.loads(out["stdout"])["ok"])

    def test_metrics_command(self):
        self.cli(*self.base_args(), "run", "--goal-type", "create_file",
                 "--param", "path=cli.txt", "--param", "content=hello", expect=0)
        out = self.cli(*self.base_args(), "metrics", "--json", expect=0)
        self.assertIn("counters", json.loads(out["stdout"]))

    def test_snapshot_and_rotate(self):
        self.cli(*self.base_args(), "run", "--goal-type", "create_file",
                 "--param", "path=cli.txt", "--param", "content=hello", expect=0)
        out = self.cli(*self.base_args(), "snapshot", "--out", str(self.tmp / "s.json"))
        self.assertTrue((self.tmp / "s.json").exists())
        self.cli(*self.base_args(), "rotate", expect=0)

    def test_bad_config_exit_code(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{nope", encoding="utf-8")
        self.cli("--config", str(bad), "doctor", expect=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
