"""Policy layer: capability allowlist, sandbox containment and risk scoring.

The policy engine is the *only* component allowed to authorise an action.  The
executor never checks permissions and the verifier never checks policy: one
responsibility per component.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional

from .models import Action, PolicyDecision

#: Capabilities the kernel may use.  Anything else is denied by default.
DEFAULT_ALLOWLIST: FrozenSet[str] = frozenset(
    {
        "filesystem.read",
        "filesystem.write",
        "filesystem.mkdir",
        "filesystem.list",
        "filesystem.delete",
    }
)

#: Default deny: destructive capabilities must be enabled explicitly.
DEFAULT_DENYLIST: FrozenSet[str] = frozenset()


@dataclass
class PolicyConfig:
    """Tunable policy knobs."""

    allowlist: FrozenSet[str] = DEFAULT_ALLOWLIST
    denylist: FrozenSet[str] = DEFAULT_DENYLIST
    max_risk: float = 0.85
    allow_delete: bool = True
    max_write_bytes: int = 8 * 1024 * 1024  # 8 MiB guard rail

    def __post_init__(self) -> None:
        self.allowlist = frozenset(self.allowlist)
        self.denylist = frozenset(self.denylist)


class PolicyEngine:
    """Decides whether an action may run, and how risky it is."""

    #: base risk per capability (0..1)
    BASE_RISK: Dict[str, float] = {
        "filesystem.read": 0.05,
        "filesystem.list": 0.05,
        "filesystem.mkdir": 0.15,
        "filesystem.write": 0.20,
        "filesystem.delete": 0.55,
    }

    def __init__(
        self,
        sandbox_dir: os.PathLike | str,
        config: Optional[PolicyConfig] = None,
    ) -> None:
        self.sandbox_dir = Path(sandbox_dir)
        # resolve() folds ".." and follows symlinks -> prevents path traversal.
        self.sandbox_root = self.sandbox_dir.resolve()
        self.config = config or PolicyConfig()
        self.sandbox_root.mkdir(parents=True, exist_ok=True)

    # -- public API --------------------------------------------------------- #
    def evaluate(self, action: Action) -> PolicyDecision:
        """Return an allow/deny decision with an explicit reason."""
        capability = action.capability

        if capability in self.config.denylist:
            return self._deny(action, f"capability {capability!r} is explicitly denied", 1.0)
        if not self.config.allowlist:
            return self._deny(action, "allowlist is empty: fail closed", 1.0)
        if capability not in self.config.allowlist:
            return self._deny(
                action,
                f"capability {capability!r} is not in the allowlist "
                f"({sorted(self.config.allowlist)})",
                1.0,
            )
        if capability == "filesystem.delete" and not self.config.allow_delete:
            return self._deny(action, "deletion is disabled by policy", 0.9)

        # ---- path containment (applies to every filesystem capability) ---- #
        raw_path = action.params.get("path")
        if raw_path is None:
            return self._deny(action, "action is missing the required 'path' param", 0.5)
        if not isinstance(raw_path, (str, os.PathLike)):
            return self._deny(action, "param 'path' must be a string or PathLike", 0.5)

        resolved, err = self.contain(raw_path)
        if err is not None:
            return self._deny(action, err, 0.95)

        assert resolved is not None  # narrow for type checkers
        if resolved == self.sandbox_root and capability in (
            "filesystem.delete",
            "filesystem.write",
        ):
            return self._deny(
                action, f"refusing {capability} on the sandbox root itself", 1.0
            )

        risk = self.risk(action, resolved)

        # ---- content guard rails ------------------------------------------ #
        content = action.params.get("content")
        if content is not None:
            size = len(content) if isinstance(content, str) else len(str(content))
            if size > self.config.max_write_bytes:
                return self._deny(
                    action,
                    f"payload of {size} bytes exceeds max_write_bytes="
                    f"{self.config.max_write_bytes}",
                    0.9,
                )

        if risk > self.config.max_risk:
            return self._deny(
                action,
                f"risk {risk:.2f} exceeds max_risk={self.config.max_risk:.2f}",
                risk,
            )

        return PolicyDecision(
            capability=capability,
            allowed=True,
            reason=f"allowed: {capability} inside sandbox",
            risk=risk,
            risk_band=self._band(risk),
        )

    def evaluate_plan(self, actions: Iterable[Action]) -> List[PolicyDecision]:
        return [self.evaluate(a) for a in actions]

    def contains(self, path: os.PathLike | str) -> bool:
        """True when *path* resolves to a location inside the sandbox."""
        _, err = self.contain(path)
        return err is None

    def contain(self, path: os.PathLike | str) -> tuple[Optional[Path], Optional[str]]:
        """Resolve *path* and verify it stays inside the sandbox.

        Returns ``(resolved_path, None)`` on success or ``(None, reason)`` when
        the path escapes the sandbox (path traversal, absolute paths outside the
        sandbox, symlinks pointing out, ...).
        """
        try:
            candidate = Path(path)
            if candidate.is_absolute():
                resolved = candidate.resolve()
            else:
                resolved = (self.sandbox_root / candidate).resolve()
        except (OSError, ValueError) as exc:  # e.g. embedded NUL byte
            return None, f"path {str(path)!r} is not resolvable: {exc}"

        try:
            resolved.relative_to(self.sandbox_root)
        except ValueError:
            return None, (
                f"path escapes the sandbox: {str(path)!r} resolves to {resolved} "
                f"which is outside {self.sandbox_root}"
            )
        return resolved, None

    # -- risk model --------------------------------------------------------- #
    def risk(self, action: Action, resolved: Optional[Path] = None) -> float:
        """Score an action in [0, 1]: capability x blast radius x payload size."""
        risk = self.BASE_RISK.get(action.capability, 0.5)

        if resolved is None:
            resolved, err = self.contain(action.params.get("path", ""))
            if err is not None:
                return 1.0

        # deleting a directory is broader than deleting a single file
        if action.capability == "filesystem.delete":
            if resolved.is_dir():
                try:
                    entries = sum(1 for _ in resolved.iterdir())
                except OSError:
                    entries = 0
                risk += 0.25 if entries else 0.05
                risk += min(entries / 1000.0, 0.15)
            risk += 0.0 if action.params.get("missing_ok") else 0.05

        # writing/creating deep new trees costs more than touching what exists
        if action.capability in ("filesystem.write", "filesystem.mkdir"):
            if not resolved.exists():
                depth = len(resolved.relative_to(self.sandbox_root).parts)
                risk += min(depth * 0.01, 0.05)

        # payload size contributes sub-linearly
        content = action.params.get("content")
        if content is not None:
            size = len(content) if isinstance(content, str) else len(str(content))
            risk += min(math.log1p(size) / 50.0, 0.15)

        # appending keeps history; overwriting destroys it
        if action.capability == "filesystem.write" and not action.params.get("append"):
            if resolved.exists():
                risk += 0.03

        return round(min(max(risk, 0.0), 1.0), 4)

    @staticmethod
    def _band(risk: float) -> str:
        if risk < 0.34:
            return "low"
        if risk < 0.67:
            return "medium"
        return "high"

    # -- internals ---------------------------------------------------------- #
    def _deny(self, action: Action, reason: str, risk: float) -> PolicyDecision:
        return PolicyDecision(
            capability=action.capability,
            allowed=False,
            reason=reason,
            risk=round(min(max(risk, 0.0), 1.0), 4),
            risk_band=self._band(risk),
        )


__all__ = ["PolicyEngine", "PolicyConfig", "DEFAULT_ALLOWLIST", "DEFAULT_DENYLIST"]
