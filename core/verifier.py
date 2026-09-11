"""Verifier layer: independent, evidence-producing checks.

The verifier never trusts the executor.  It re-reads the world from disk and
compares it against the *intent* declared in the action (expected content,
expected hash, expected path), not against the executor's own report.

A decision reaches ``VERIFIED`` only when every check of every action passes.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Callable, Dict, List

from .executor import sha256_file
from .models import Action, Evidence, Observation


class Verifier:
    """Produces :class:`Evidence` for an action/observation pair."""

    def __init__(self, sandbox_dir: os.PathLike | str) -> None:
        self.sandbox_root = Path(sandbox_dir).resolve()

    # -- public API --------------------------------------------------------- #
    def verify(self, action: Action, observation: Observation) -> Evidence:
        handler: Callable[[Action, Observation], Dict[str, bool]] = getattr(
            self, f"_verify_{action.capability.replace('.', '_')}", self._verify_unknown
        )
        checks, details = self._run(action, observation, handler)
        return Evidence(
            action_id=action.action_id,
            capability=action.capability,
            checks=checks,
            details=details,
        )

    def verify_many(
        self, pairs: List[tuple[Action, Observation]]
    ) -> List[Evidence]:
        return [self.verify(action, obs) for action, obs in pairs]

    # -- internals ---------------------------------------------------------- #
    def _run(
        self,
        action: Action,
        observation: Observation,
        handler: Callable[[Action, Observation], Dict[str, bool]],
    ) -> tuple[Dict[str, bool], Dict[str, Any]]:
        details: Dict[str, Any] = {
            "capability": action.capability,
            "path": str(action.params.get("path", "")),
            "executor_error": observation.error,
        }
        try:
            checks = {"claimed_ok": bool(observation.ok)}
            checks.update(handler(action, observation))
        except Exception as exc:  # a broken verifier must not crash the kernel
            checks = {"claimed_ok": bool(observation.ok), "verifier_error": False}
            details["verifier_error"] = f"{type(exc).__name__}: {exc}"
        return checks, details

    # -- per-capability checks ---------------------------------------------- #
    def _verify_filesystem_write(
        self, action: Action, observation: Observation
    ) -> Dict[str, bool]:
        path = self._resolve(action)
        expected = action.params.get("content", "")
        if isinstance(expected, bytes):
            expected_bytes = expected
        else:
            expected_bytes = str(expected).encode("utf-8")

        checks: Dict[str, bool] = {
            "file_exists": bool(path and path.is_file()),
            "correct_path": bool(path and self._same_path(path, observation)),
        }
        if not path or not path.is_file():
            checks.update({"correct_content": False, "hash_matches": False})
            return checks

        actual_bytes = path.read_bytes()
        expected_hash = hashlib.sha256(expected_bytes).hexdigest()
        actual_hash = sha256_file(path)

        if action.params.get("append"):
            # for appends we can only prove the new bytes are present at the end
            checks["content_appended"] = actual_bytes.endswith(expected_bytes)
            checks["hash_matches"] = actual_hash == observation.data.get("sha256", "")
        else:
            checks["correct_content"] = actual_bytes == expected_bytes
            checks["hash_matches"] = actual_hash == expected_hash
        return checks

    def _verify_filesystem_read(
        self, action: Action, observation: Observation
    ) -> Dict[str, bool]:
        path = self._resolve(action)
        checks: Dict[str, bool] = {}
        missing_ok = bool(action.params.get("missing_ok"))
        exists = bool(path and path.exists())
        checks["file_exists"] = exists or missing_ok
        checks["correct_path"] = bool(path and self._same_path(path, observation))

        if exists and path.is_file():
            disk = path.read_bytes()
            checks["observation_matches_disk"] = (
                observation.data.get("sha256", "") == sha256_file(path)
            )
            expected = action.params.get("expected_content")
            if expected is not None:
                expected_bytes = (
                    expected if isinstance(expected, bytes) else str(expected).encode("utf-8")
                )
                checks["correct_content"] = disk == expected_bytes
            expected_hash = action.params.get("expected_sha256")
            if expected_hash:
                checks["hash_matches"] = sha256_file(path) == expected_hash
        return checks

    def _verify_filesystem_delete(
        self, action: Action, observation: Observation
    ) -> Dict[str, bool]:
        path = self._resolve(action)
        return {
            "correct_path": bool(path and self._same_path(path, observation)),
            "path_absent": bool(path and not path.exists()),
        }

    def _verify_filesystem_mkdir(
        self, action: Action, observation: Observation
    ) -> Dict[str, bool]:
        path = self._resolve(action)
        return {
            "correct_path": bool(path and self._same_path(path, observation)),
            "dir_exists": bool(path and path.is_dir()),
        }

    def _verify_filesystem_list(
        self, action: Action, observation: Observation
    ) -> Dict[str, bool]:
        path = self._resolve(action)
        checks: Dict[str, bool] = {
            "correct_path": bool(path and self._same_path(path, observation)),
            "dir_exists": bool(path and path.is_dir()),
        }
        if path and path.is_dir():
            entries = sorted(
                {f"{p.name}/" for p in path.iterdir() if p.is_dir()}
                | {p.name for p in path.iterdir() if not p.is_dir()}
            )
            checks["entries_match_disk"] = entries == sorted(
                observation.data.get("entries", [])
            )
        return checks

    def _verify_unknown(
        self, action: Action, observation: Observation
    ) -> Dict[str, bool]:
        return {"known_capability": False}

    # -- helpers ------------------------------------------------------------ #
    def _resolve(self, action: Action) -> Path | None:
        raw = action.params.get("path")
        if raw is None:
            return None
        try:
            candidate = Path(raw)
            resolved = (
                candidate.resolve()
                if candidate.is_absolute()
                else (self.sandbox_root / candidate).resolve()
            )
        except (OSError, ValueError):
            return None
        try:
            resolved.relative_to(self.sandbox_root)
        except ValueError:
            return None  # outside the sandbox -> nothing can be verified
        return resolved

    def _same_path(self, resolved: Path, observation: Observation) -> bool:
        claimed = observation.data.get("path", "")
        if not claimed:
            return False
        try:
            claimed_path = Path(claimed)
            claimed_resolved = (
                claimed_path.resolve()
                if claimed_path.is_absolute()
                else (self.sandbox_root / claimed_path).resolve()
            )
        except (OSError, ValueError):
            return False
        return claimed_resolved == resolved


__all__ = ["Verifier"]
