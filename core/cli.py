#!/usr/bin/env python3
"""Command line interface for the AGI Kernel.

    python -m core run     --goal-type create_file --param path=a.txt --param content=hi
    python -m core replay  --run-id run_abc
    python -m core resume  run_abc
    python -m core verify  --json
    python -m core doctor
    python -m core metrics
    python -m core snapshot --out snapshot.json
    python -m core rotate   --keep 5

Exit codes (stable, for CI and scripts):
    0 ok | 1 error | 2 config | 3 denied | 4 unverified | 5 ledger corrupt | 6 aborted
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .config import Settings
from .errors import ConfigurationError, KernelError
from .kernel import Kernel
from .logging_setup import configure_logging
from .models import ExecStatus, Goal

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_DENIED = 3
EXIT_UNVERIFIED = 4
EXIT_LEDGER = 5
EXIT_ABORTED = 6

_STATUS_EXIT = {
    ExecStatus.VERIFIED: EXIT_OK,
    ExecStatus.DENIED: EXIT_DENIED,
    ExecStatus.INCONCLUSIVE: EXIT_UNVERIFIED,
    ExecStatus.FAILED: EXIT_ERROR,
    ExecStatus.ABORTED: EXIT_ABORTED,
}

#: the kernel currently being driven, so signal handlers can stop it softly
_ACTIVE_KERNEL: Optional[Kernel] = None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _parse_params(pairs: Optional[Sequence[str]]) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ConfigurationError(f"--param must look like key=value (got {pair!r})")
        key, _, raw = pair.partition("=")
        params[key.strip()] = _coerce(raw)
    return params


def _coerce(raw: str) -> Any:
    """Parse JSON scalars when possible, keep the raw string otherwise."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _load_goal(args: argparse.Namespace) -> Goal:
    if args.goal_file:
        path = Path(args.goal_file)
        if not path.is_file():
            raise ConfigurationError(f"goal file not found: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"invalid goal JSON: {exc}") from exc
        if "goal_type" not in data:
            raise ConfigurationError("goal file must contain 'goal_type'")
        data.setdefault("params", {})
        return Goal.from_dict(data)
    if not args.goal_type:
        raise ConfigurationError("either --goal-type or --goal-file is required")
    return Goal(goal_type=args.goal_type, params=_parse_params(args.param))


def _build_settings(args: argparse.Namespace) -> Settings:
    overrides: Dict[str, Any] = {
        "sandbox_dir": args.sandbox,
        "ledger_path": args.ledger,
        "log_level": args.log_level,
        "metrics_path": getattr(args, "metrics_path", None),
    }
    if getattr(args, "json_logs", False):
        overrides["log_json"] = True
    return Settings.load(getattr(args, "config", None), **overrides)


def _kernel(args: argparse.Namespace) -> Kernel:
    settings = _build_settings(args)
    configure_logging(
        settings.log_level,
        json_mode=settings.log_json,
        log_file=settings.log_path,
        quiet=getattr(args, "quiet", False),
    )
    return Kernel.from_settings(settings)


def _print(payload: Any, as_json: bool) -> None:
    if isinstance(payload, str):
        print(payload)
        return
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return
    _print_human(payload)


def _print_human(payload: Any, indent: int = 0) -> None:
    pad = " " * indent
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                print(f"{pad}{key}:")
                _print_human(value, indent + 2)
            else:
                print(f"{pad}{key}: {value}")
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, (dict, list)):
                _print_human(item, indent + 2)
            else:
                print(f"{pad}- {item}")
    else:
        print(f"{pad}{payload}")


def _install_signal_handlers() -> None:
    """Turn SIGINT/SIGTERM into a cooperative stop instead of a hard kill."""

    def handler(signum, _frame):  # noqa: ANN001 - signal signature
        name = signal.Signals(signum).name
        print(f"[agi] received {name}: finishing current step...", file=sys.stderr)
        if _ACTIVE_KERNEL is not None:
            _ACTIVE_KERNEL.request_stop(reason=name)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_run(args: argparse.Namespace) -> int:
    global _ACTIVE_KERNEL
    kernel = _kernel(args)
    _ACTIVE_KERNEL = kernel
    _install_signal_handlers()

    goal = _load_goal(args)
    record = kernel.run(goal, run_id=args.run_id, timeout=args.timeout)

    if args.json:
        _print(record.to_dict(), True)
    else:
        print(f"run_id : {record.run_id}")
        print(f"status : {record.status.value}"
              + (f" ({record.error_code})" if record.error_code else ""))
        if record.plan:
            print(f"plan   : {record.plan.plan_id} "
                  f"risk={record.plan.total_risk:.2f} steps={len(record.plan.actions)}")
        for evidence in record.evidence:
            checks = " ".join(f"{k}={str(v).lower()}" for k, v in evidence.checks.items())
            print(f"evidence[{evidence.capability}]: passed={evidence.passed}")
            print(f"  {checks}")
        if record.error:
            print(f"error  : {record.error}")
    return _STATUS_EXIT.get(record.status, EXIT_ERROR)


def cmd_replay(args: argparse.Namespace) -> int:
    kernel = _kernel(args)
    state = kernel.replay()
    if args.json:
        _print(
            {
                "chain_valid": state.chain_valid,
                "events": len(state.events),
                "runs": state.runs if args.run_id is None else
                {args.run_id: state.runs.get(args.run_id)},
            },
            True,
        )
        return EXIT_OK if state.chain_valid else EXIT_LEDGER

    print(f"chain_valid : {state.chain_valid}")
    print(f"events      : {len(state.events)}")
    runs = state.runs if args.run_id is None else {k: v for k, v in state.runs.items()
                                                   if k == args.run_id}
    for run_id, info in runs.items():
        goal = (info.get("goal") or {}).get("goal_type", "?")
        print(f"  {run_id}: status={info.get('status')} events={info.get('events')} goal={goal}")
    return EXIT_OK if state.chain_valid else EXIT_LEDGER


def cmd_resume(args: argparse.Namespace) -> int:
    global _ACTIVE_KERNEL
    kernel = _kernel(args)
    _ACTIVE_KERNEL = kernel
    _install_signal_handlers()
    record = kernel.resume(args.run_id, timeout=args.timeout)
    if args.json:
        _print(record.to_dict(), True)
    else:
        print(f"run_id : {record.run_id}")
        print(f"status : {record.status.value}")
        if record.error:
            print(f"error  : {record.error}")
    return _STATUS_EXIT.get(record.status, EXIT_ERROR)


def cmd_verify(args: argparse.Namespace) -> int:
    kernel = _kernel(args)
    report = kernel.ledger.fsck()
    if not report["chain_valid"] and args.quarantine:
        report = kernel.ledger.quarantine()
    if args.json:
        _print(report, True)
    else:
        print(f"ledger      : {report['path']}")
        print(f"events      : {report['events']} (corrupt lines: {report['corrupt_lines']})")
        print(f"chain_valid : {report['chain_valid']}"
              + (f" (first bad seq: {report['first_bad_seq']})"
                 if report["first_bad_seq"] is not None else ""))
        print(f"head_hash   : {report['head_hash']}")
        if report.get("quarantine_path"):
            print(f"quarantined : {report['quarantined']} -> {report['quarantine_path']}")
    return EXIT_OK if report["chain_valid"] else EXIT_LEDGER


def cmd_doctor(args: argparse.Namespace) -> int:
    kernel = _kernel(args)
    health = kernel.health()
    if args.json:
        _print(health, True)
    else:
        print(f"ok          : {health['ok']}")
        print(f"python      : {health['python']} ({health['platform']})")
        sandbox = health["sandbox"]
        print(f"sandbox     : {sandbox['path']} exists={sandbox['exists']} "
              f"writable={sandbox['writable']} files={sandbox['files']}")
        ledger = health["ledger"]
        print(f"ledger      : {ledger['path']} events={ledger['events']} "
              f"chain_valid={ledger['chain_valid']} bytes={ledger['bytes']}")
        counters = health["metrics"]["counters"]
        print(f"metrics     : {counters or '{}'}")
    return EXIT_OK if health["ok"] else EXIT_LEDGER


def cmd_metrics(args: argparse.Namespace) -> int:
    kernel = _kernel(args)
    state = kernel.ledger.replay()
    live = kernel.metrics.snapshot()

    # counters from previous processes live in metrics.json; merge them so the
    # command is useful even in a fresh shell
    persisted: Dict[str, Any] = {}
    path = kernel.settings.resolved_metrics_path()
    if path and Path(path).exists():
        try:
            persisted = json.loads(Path(path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            persisted = {}

    payload = {
        "runs": len(state.runs),
        "events": len(state.events),
        "chain_valid": state.chain_valid,
        "counters": {**persisted.get("counters", {}), **live["counters"]},
        "gauges": {**persisted.get("gauges", {}), **live["gauges"]},
        "durations": {**persisted.get("durations", {}), **live["durations"]},
        "uptime_s": live["uptime_s"],
        "source": "persisted+live" if persisted else "live",
    }
    if args.json:
        _print(payload, True)
    else:
        print(f"runs        : {payload['runs']} (events: {payload['events']}, "
              f"chain_valid: {payload['chain_valid']})")
        print("counters    :")
        for key, value in payload["counters"].items():
            print(f"  {key}: {value}")
        if payload["durations"]:
            print("durations   :")
            for key, value in payload["durations"].items():
                print(f"  {key}: {value}")
    return EXIT_OK


def cmd_snapshot(args: argparse.Namespace) -> int:
    kernel = _kernel(args)
    target = kernel.ledger.snapshot(args.out)
    print(str(target))
    return EXIT_OK


def cmd_rotate(args: argparse.Namespace) -> int:
    kernel = _kernel(args)
    archived = kernel.ledger.rotate(keep=args.keep)
    print(str(archived) if archived else "nothing to rotate")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def _common_options(suppress: bool = False) -> argparse.ArgumentParser:
    """Global options, shared by the root parser and every subcommand.

    Subcommands use ``SUPPRESS`` defaults so they only override the root value
    when the flag is actually repeated after the subcommand name.
    """
    default = argparse.SUPPRESS if suppress else None
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", default=default, help="settings file (JSON/TOML)")
    parser.add_argument("--sandbox", default=default, help="sandbox directory")
    parser.add_argument("--ledger", default=default,
                        help="ledger path (default: next to the sandbox)")
    parser.add_argument("--log-level", default=default, help="DEBUG/INFO/WARNING/ERROR")
    parser.add_argument("--json-logs", action="store_true", default=default,
                        help="emit JSON log lines")
    parser.add_argument("-q", "--quiet", action="store_true", default=default,
                        help="silence log output")
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agi-kernel",
        description="AGI Kernel: verified cognitive loop (no dependencies)",
        parents=[_common_options()],
    )
    sub = parser.add_subparsers(dest="command", required=True)
    shared = [_common_options(suppress=True)]

    run = sub.add_parser("run", parents=shared, help="execute one goal through the kernel")
    run.add_argument("--goal-type")
    run.add_argument("--param", action="append", help="key=value (repeatable)")
    run.add_argument("--goal-file", help="JSON file: {'goal_type': ..., 'params': {...}}")
    run.add_argument("--run-id")
    run.add_argument("--timeout", type=float, default=None, help="run timeout in seconds")
    run.add_argument("--json", action="store_true")
    run.set_defaults(func=cmd_run)

    replay = sub.add_parser("replay", parents=shared, help="rebuild state from the ledger")
    replay.add_argument("--run-id", default=None)
    replay.add_argument("--json", action="store_true")
    replay.set_defaults(func=cmd_replay)

    resume = sub.add_parser("resume", parents=shared, help="continue an interrupted run")
    resume.add_argument("run_id")
    resume.add_argument("--timeout", type=float, default=None)
    resume.add_argument("--json", action="store_true")
    resume.set_defaults(func=cmd_resume)

    verify = sub.add_parser("verify", parents=shared, help="check ledger integrity (fsck)")
    verify.add_argument("--quarantine", action="store_true", help="quarantine the bad tail")
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=cmd_verify)

    doctor = sub.add_parser("doctor", parents=shared, help="health check of the whole system")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    metrics = sub.add_parser("metrics", parents=shared, help="counters and durations")
    metrics.add_argument("--json", action="store_true")
    metrics.set_defaults(func=cmd_metrics)

    snapshot = sub.add_parser("snapshot", parents=shared, help="write a ledger snapshot")
    snapshot.add_argument("--out", default=None)
    snapshot.set_defaults(func=cmd_snapshot)

    rotate = sub.add_parser("rotate", parents=shared, help="archive the ledger and start a new one")
    rotate.add_argument("--keep", type=int, default=5)
    rotate.set_defaults(func=cmd_rotate)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KernelError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("interrupted", file=sys.stderr)
        return EXIT_ABORTED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
