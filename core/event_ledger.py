"""Append-only JSONL event ledger: replay, hash chaining, locking, fsck.

Properties:
  * one JSON object per line, never rewritten (append-only)
  * ``seq`` + ``prev``/``hash`` form a tamper-evident SHA-256 chain
  * process-safe (``threading.RLock``) and, on POSIX, cross-process safe
    (``flock``); elsewhere a portable ``.lock`` file is used
  * ``replay()`` rebuilds kernel state from the events alone
  * ``fsck()`` / ``quarantine()`` / ``snapshot()`` / ``rotate()`` make it
    operable in production without any external tooling
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .config import SCHEMA_VERSION
from .errors import LedgerLockError

try:  # pragma: no cover - platform dependent
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False

GENESIS = "0" * 64


@dataclass
class LedgerState:
    """State reconstructed purely by replaying the ledger."""

    events: List[Dict[str, Any]] = field(default_factory=list)
    runs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    chain_valid: bool = True
    errors: List[str] = field(default_factory=list)

    @property
    def run_ids(self) -> List[str]:
        return list(self.runs)

    def events_for(self, run_id: str) -> List[Dict[str, Any]]:
        return [e for e in self.events if e.get("run_id") == run_id]


class EventLedger:
    """Durable, dependency-free event log."""

    def __init__(self, path: os.PathLike | str, chain: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.chain = chain
        self._lock = threading.RLock()
        self._seq_cache: Optional[int] = None
        self._prev_cache: Optional[str] = None

    # -- writing ------------------------------------------------------------ #
    def append(
        self,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        run_id: Optional[str] = None,
        actor: str = "kernel",
    ) -> Dict[str, Any]:
        """Append one event and return the persisted (hashed) event."""
        with self._lock:
            seq, prev = self._tail_meta()
            event: Dict[str, Any] = {
                "v": SCHEMA_VERSION,
                "seq": seq,
                "ts": time.time(),
                "type": event_type,
                "run_id": run_id,
                "actor": actor,
                "payload": payload or {},
                "prev": prev,
            }
            event["hash"] = self._hash(event)
            line = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
            self._write_line(line)
            self._seq_cache = seq
            self._prev_cache = event["hash"]
            return event

    # -- reading ------------------------------------------------------------ #
    def read_all(self) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        if not self.path.exists():
            return events
        with self._lock, open(self.path, "r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    events.append(
                        {
                            "SEQ_CORRUPT": line_no,
                            "type": "LEDGER_CORRUPTION",
                            "payload": {"raw": line},
                        }
                    )
        return events

    @property
    def head_hash(self) -> str:
        """Hash of the last event, or ``GENESIS`` for an empty ledger."""
        with self._lock:
            if self._prev_cache is not None:
                return self._prev_cache
            return self._tail_meta()[1]

    def verify_chain(self, events: Optional[List[Dict[str, Any]]] = None) -> bool:
        """Recompute every hash and linkage; True when nothing was tampered with."""
        events = events if events is not None else self.read_all()
        prev = GENESIS
        for event in events:
            if event.get("type") == "LEDGER_CORRUPTION":
                return False
            if event.get("prev") != prev:
                return False
            clone = dict(event)
            clone.pop("hash", None)
            if self._hash(clone) != event.get("hash"):
                return False
            prev = event["hash"]
        return True

    def first_bad_seq(self, events: Optional[List[Dict[str, Any]]] = None) -> Optional[int]:
        """Sequence number of the first event that breaks the chain."""
        events = events if events is not None else self.read_all()
        prev = GENESIS
        for event in events:
            if event.get("type") == "LEDGER_CORRUPTION":
                return event.get("SEQ_CORRUPT")
            clone = dict(event)
            clone.pop("hash", None)
            if event.get("prev") != prev or self._hash(clone) != event.get("hash"):
                return event.get("seq")
            prev = event["hash"]
        return None

    def replay(self) -> LedgerState:
        """Rebuild run state from the event stream (source of truth)."""
        events = self.read_all()
        state = LedgerState(events=events, chain_valid=self.verify_chain(events))
        if not state.chain_valid:
            state.errors.append("hash chain verification failed")
        for event in events:
            run_id = event.get("run_id")
            if not run_id:
                continue
            run = state.runs.setdefault(
                run_id, {"run_id": run_id, "events": 0, "status": "PROPOSED"}
            )
            run["events"] += 1
            etype = event.get("type", "")
            payload = event.get("payload") or {}
            if etype == "GOAL_RECEIVED":
                run["goal"] = payload.get("goal", {})
            elif etype == "DECISION_RECORDED":
                run["record"] = payload.get("record", {})
                run["status"] = payload.get("status", run["status"])
            elif etype == "STATUS_CHANGED":
                run["status"] = payload.get("status", run["status"])
            elif etype == "EXECUTION_START":
                if run["status"] == "AUTHORIZED":
                    run["status"] = "EXECUTING"
            elif etype == "POLICY_DENIED":
                run["status"] = "DENIED"
                run["denial_reasons"] = payload.get("reasons", [])
            elif etype == "PLANNING_FAILED":
                run["status"] = "FAILED"
                run["error"] = payload.get("error")
            elif etype == "PLAN_SELECTED":
                run["plan"] = payload.get("plan", {})
                if run["status"] == "PROPOSED":
                    run["status"] = "AUTHORIZED"
            elif etype == "KERNEL_ERROR":
                run["status"] = "FAILED"
                run["error"] = payload.get("error")
            elif etype == "RUN_ABORTED":
                run["status"] = "ABORTED"
                run["error"] = payload.get("error")
        return state

    # -- operations ---------------------------------------------------------- #
    def fsck(self) -> Dict[str, Any]:
        """Health report: corrupt lines, chain validity, head hash."""
        events = self.read_all()
        corrupt = [e for e in events if e.get("type") == "LEDGER_CORRUPTION"]
        return {
            "path": str(self.path),
            "exists": self.path.exists(),
            "schema_version": SCHEMA_VERSION,
            "lines": len(events),
            "events": len(events) - len(corrupt),
            "corrupt_lines": len(corrupt),
            "chain_valid": self.verify_chain(events),
            "first_bad_seq": self.first_bad_seq(events),
            "head_hash": self.head_hash,
            "bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    def quarantine(self) -> Dict[str, Any]:
        """Move the untrustworthy tail to ``<path>.quarantine-<ts>``.

        Everything up to (but excluding) the first bad event is kept; the rest
        is preserved next to the ledger for forensic analysis.
        """
        report = self.fsck()
        if report["chain_valid"]:
            return {**report, "quarantined": 0, "quarantine_path": None}

        lines = self.path.read_text(encoding="utf-8").splitlines()
        bad = report["first_bad_seq"]
        keep_until = len(lines)
        if bad is not None:
            for index, line in enumerate(lines):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    keep_until = index
                    break
                if event.get("seq") == bad:
                    keep_until = index
                    break

        good, bad_lines = lines[:keep_until], lines[keep_until:]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        quarantine_path = Path(f"{self.path}.quarantine-{stamp}")
        with self._lock:
            quarantine_path.write_text(
                "\n".join(bad_lines) + ("\n" if bad_lines else ""), encoding="utf-8"
            )
            self.path.write_text(
                "\n".join(good) + ("\n" if good else ""), encoding="utf-8"
            )
            self._seq_cache = None
            self._prev_cache = None
        return {**self.fsck(), "quarantined": len(bad_lines),
                "quarantine_path": str(quarantine_path)}

    def snapshot(self, path: Optional[os.PathLike | str] = None) -> Path:
        """Write a JSON summary (head hash + runs) next to the ledger."""
        target = Path(path) if path else Path(f"{self.path}.snapshot.json")
        state = self.replay()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ledger": str(self.path),
                    "head_hash": self.head_hash,
                    "chain_valid": state.chain_valid,
                    "events": len(state.events),
                    "runs": {
                        rid: {
                            "status": info.get("status"),
                            "events": info.get("events"),
                            "goal": (info.get("goal") or {}).get("goal_type"),
                        }
                        for rid, info in state.runs.items()
                    },
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return target

    def rotate(self, keep: int = 5) -> Optional[Path]:
        """Start a fresh ledger file, archiving the current one.

        The new chain is re-anchored from ``GENESIS`` and a ``LEDGER_ROTATED``
        event records the previous head hash so the two files stay linkable.
        """
        with self._lock:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return None
            stamp = time.strftime("%Y%m%d-%H%M%S")
            archived = self.path.with_name(f"{self.path.stem}-{stamp}.jsonl")
            previous_head = self.head_hash
            shutil.move(str(self.path), str(archived))
            self._seq_cache = None
            self._prev_cache = None
            self.append(
                "LEDGER_ROTATED",
                {"archived": str(archived), "previous_head": previous_head},
                run_id=None,
                actor="ledger",
            )
            self._prune_archives(keep)
            return archived

    def _prune_archives(self, keep: int) -> None:
        if keep < 0:
            return
        archives = sorted(
            self.path.parent.glob(f"{self.path.stem}-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in archives[max(keep, 0):]:
            try:
                old.unlink()
            except OSError:  # pragma: no cover - best effort
                pass

    def clear(self) -> None:
        """Truncate the ledger (used by tests/demo for a clean slate)."""
        with self._lock:
            self.path.write_text("", encoding="utf-8")
            self._seq_cache = None
            self._prev_cache = None

    def __len__(self) -> int:
        return len(self.read_all())

    # -- internals ---------------------------------------------------------- #
    def _tail_meta(self) -> tuple[int, str]:
        """Return ``(next_seq, prev_hash)``; cached to keep appends O(1)."""
        if self._seq_cache is not None and self._prev_cache is not None:
            return self._seq_cache + 1, self._prev_cache
        seq, prev = -1, GENESIS
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    seq = int(event.get("seq", seq))
                    prev = event.get("hash", prev)
        return seq + 1, prev

    def _write_line(self, line: str) -> None:
        """Atomic-ish append: single write() call under an exclusive lock."""
        with open(self.path, "a", encoding="utf-8") as handle:
            if _HAS_FCNTL:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # type: ignore[union-attr]
                try:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]
            else:
                with self._portable_lock():
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())

    @contextmanager
    def _portable_lock(self, timeout: float = 5.0, interval: float = 0.01) -> Iterator[None]:
        """Fallback lock for platforms without ``flock`` (e.g. Windows)."""
        lock_path = Path(str(self.path) + ".lock")
        deadline = time.monotonic() + timeout
        acquired = False
        while time.monotonic() < deadline:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                acquired = True
                break
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
                time.sleep(interval)
        if not acquired:
            raise LedgerLockError(f"could not acquire ledger lock: {lock_path}")
        try:
            yield
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _hash(event: Dict[str, Any]) -> str:
        canonical = json.dumps(event, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def temp_ledger(prefix: str = "agi-ledger-") -> EventLedger:
    """Convenience helper: a ledger inside a throw-away temp directory."""
    directory = tempfile.mkdtemp(prefix=prefix)
    return EventLedger(Path(directory) / "events.jsonl")


__all__ = ["EventLedger", "LedgerState", "temp_ledger", "GENESIS", "SCHEMA_VERSION"]
