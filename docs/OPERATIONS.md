# Operations runbook

## Install / run

```bash
python3 -V                    # >= 3.10
python3 -m unittest discover -s tests -t . -v     # 86 tests
python3 demo.py                                    # 6 scenarios
python3 -m core doctor --sandbox ./sandbox         # health check

# optional packaging (no runtime dependencies)
python3 -m pip install -e .
agi-kernel doctor --sandbox ./sandbox
```

## Configuration

Precedence: **kwargs > `AGI_*` env > config file > defaults**.
Copy `agi-kernel.example.json` to `agi-kernel.json` (auto-discovered in the cwd)
or pass `--config path.json`.

| Variable | Meaning |
|---|---|
| `AGI_SANDBOX_DIR` | sandbox root |
| `AGI_LEDGER_PATH` | event ledger file (outside the sandbox!) |
| `AGI_METRICS_PATH` | where `metrics.json` is written |
| `AGI_LOG_LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `AGI_LOG_JSON` | `1` → JSON log lines |
| `AGI_MAX_RISK` | deny actions above this risk (0..1) |
| `AGI_ALLOWLIST` / `AGI_DENYLIST` | comma-separated capabilities |
| `AGI_ACTION_TIMEOUT` / `AGI_RUN_TIMEOUT` | seconds |
| `AGI_MAX_ACTIONS`, `AGI_MAX_CONTENT_BYTES`, `AGI_MAX_SANDBOX_FILES` | quotas |
| `AGI_MAX_ATTEMPTS`, `AGI_BACKOFF`, `AGI_RETRY_ON_TIMEOUT` | retry budget |

## CLI

```bash
agi-kernel run     --goal-type create_file --param path=a.txt --param content=hi
agi-kernel run     --goal-file goal.json --timeout 30 --json
agi-kernel replay  [--run-id ID] [--json]
agi-kernel resume  run_abc123
agi-kernel verify  [--quarantine] [--json]     # ledger fsck
agi-kernel doctor  [--json]
agi-kernel metrics [--json]
agi-kernel snapshot --out snapshot.json
agi-kernel rotate  --keep 5
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | `VERIFIED` / healthy |
| 1 | general failure (`FAILED`) |
| 2 | configuration error |
| 3 | denied by policy |
| 4 | unverified (`INCONCLUSIVE`) |
| 5 | ledger corruption detected |
| 6 | aborted / interrupted |

## Signals and shutdown

`SIGINT` / `SIGTERM` are trapped by the CLI and turned into
`Kernel.request_stop()`: the current step finishes, the run is finalised as
`ABORTED`, and the ledger stays consistent. Send a second signal to kill hard.
In-process: call `kernel.request_stop()` and later `kernel.resume_operations()`.

## Recovery procedures

| Symptom | Command | Action |
|---|---|---|
| Run interrupted (crash, kill) | `agi-kernel replay` → find `run_id` | `agi-kernel resume <run_id>` |
| `verify` reports chain invalid | `agi-kernel verify --quarantine` | inspect `<ledger>.quarantine-<ts>`, keep the good prefix |
| Ledger too large | `agi-kernel rotate --keep 5` | archives `events-<ts>.jsonl`, re-anchors the chain |
| Sandbox full of junk | check `doctor` → `sandbox.files` | raise `limits.max_sandbox_files` or clean the sandbox |
| Run hangs | wait for `action_timeout_seconds` | tune limits; the worker thread is a daemon, the kernel never blocks |
| Repeated denials | `replay --json` → `denial_reasons` | fix the goal paths or extend the allowlist deliberately |

## Metrics

`metrics.json` (or `AGI_METRICS_PATH`) contains:

```json
{
  "counters": {"runs_total": 12.0, "runs_verified": 10.0, "runs_denied": 2.0,
               "actions_total": 14.0, "action_failures": 1.0},
  "durations": {"run": {"count": 12, "avg_ms": 4.2, "max_ms": 31.0}}
}
```

Alert ideas: `runs_failed / runs_total`, `verification_failures > 0`,
`action_retries` climbing (upstream flakiness), `chain_valid == false`.

## CI

`.github/workflows/ci.yml` runs the unit tests, the demo and the CLI smoke test
on Python 3.10 → 3.13 with zero third-party packages.
