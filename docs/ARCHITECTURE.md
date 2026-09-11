# Architecture

> "No claims without evidence." — every state transition in this system is
> earned, recorded and independently checkable.

## 1. The loop

```
        ┌──────────────────────────────────────────────────────────────┐
Goal ──►│ Planner ──► Policy ──► Executor ──► Verifier ──► DecisionRecord│──► EventLedger
        └──────────────────────────────────────────────────────────────┘
             ▲            │ deny              │ claim          │ evidence
             │            ▼                   ▼                ▼
          Plan(s)      DENIED            Observation       VERIFIED / INCONCLUSIVE
```

| Step | Component | Contract |
|---|---|---|
| 1 | `Planner.propose(goal)` | returns **several** candidate plans ordered by cost |
| 2 | `PolicyEngine.evaluate_plan` | allow/deny **per action**, with a reason + risk score |
| 3 | `Kernel` | picks the best **fully allowed** plan (alternatives are the fallback) |
| 4 | `Executor.execute` | performs I/O, returns an `Observation` (never raises) |
| 5 | `Verifier.verify` | re-reads the world and returns `Evidence` (checks + passed) |
| 6 | `Kernel._finalize` | writes a `DecisionRecord`, emits `DECISION_RECORDED` |

## 2. Invariants (do not break these)

1. **Fail closed** — an unknown capability, an empty allowlist, or a path that
   cannot be proven to be inside the sandbox is always a denial.
2. **Never trust the executor** — the verifier recomputes SHA-256 from disk and
   compares it with the *intent* declared in the action, never with the
   executor's report.
3. **Never lose the audit trail** — every state change emits an event before
   the transition is considered complete; the ledger lives outside the sandbox.
4. **No exception escapes `Kernel.run()`** — every failure is a terminal status
   plus a coded error. (`KeyboardInterrupt` / `SystemExit` are process-level
   signals and are deliberately re-raised.)
5. **Everything is bounded** — action timeout, run timeout, retry budget,
   payload size, file quota.

## 3. Modules

| Module | Responsibility |
|---|---|
| `models.py` | pure data objects + JSON (de)serialisation |
| `planner.py` | `Planner` interface (`propose`) — swap in an LLM planner here |
| `policy.py` | allowlist, sandbox containment (`resolve()` + `relative_to()`), risk model |
| `executor.py` | capabilities; atomic writes; timeouts; quotas; error codes |
| `verifier.py` | independent, per-capability evidence checks |
| `event_ledger.py` | append-only JSONL + SHA-256 hash chain + fsck/quarantine/rotate |
| `kernel.py` | orchestration, state, concurrency, recovery, health |
| `config.py` | validated `Settings` (file + `AGI_*` env + defaults) |
| `errors.py` | typed taxonomy with stable codes |
| `clock.py` | injectable time (`SystemClock`, `FrozenClock`) |
| `metrics.py` | counters / gauges / durations, flushed to `metrics.json` |
| `logging_setup.py` | text or JSON structured logging |
| `cli.py` | `agi-kernel` command surface |

## 4. State machine

```
PROPOSED ──► AUTHORIZED ──► EXECUTING ──┬──► VERIFIED        (all evidence passed)
                                        ├──► INCONCLUSIVE    (claim ok, evidence missing)
                                        ├──► FAILED          (planning/execution/verify error)
                                        └──► ABORTED         (operator stop / signal)
        └──► DENIED                     (policy rejected every plan)
```

`ABORTED` is operator-initiated and therefore **resumable by re-running the
goal**; `DENIED`/`FAILED`/`INCONCLUSIVE`/`VERIFIED` are verdicts and are
returned as-is by `resume()`.

## 5. Extension points

**Add a capability** (3 files, no kernel change):

1. `executor.py` — add `handlers["domain.verb"] = self._handler`
2. `verifier.py` — add `_verify_domain_verb()` returning `{check_name: bool}`
3. `config.py` / allowlist — add the capability name

**Add a planner**: implement `Planner.propose(goal) -> list[Plan]` and pass it
to `Kernel(planner=...)`. An LLM-backed planner fits this interface; keep it
deterministic at the *action* level so the verifier can still check it.

**Add a policy rule**: extend `PolicyEngine.evaluate()` — it owns every
authorisation decision; do not add checks to the executor.

## 6. Testing strategy

* `tests/test_vertical_slice.py` — behaviour of the loop (happy path, denial,
  adversarial executor, resume, concurrency, ledger).
* `tests/test_phase0_hardening.py` — operability (config, errors, clock,
  metrics, timeouts, retries, quotas, abort, ledger ops, CLI subprocesses).
* Adversarial doubles (`LyingExecutor`, `TamperingExecutor`, `ExplodingExecutor`,
  `CrashOnceExecutor`) prove the verifier, not the executor, grants `VERIFIED`.
* No test writes outside a `tempfile` directory: the suite is portable.
