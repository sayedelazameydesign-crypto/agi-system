"""Configuration: file + environment + sane defaults.

Nothing here requires a third-party package: configuration files are plain
JSON (``tomllib`` is used when available for ``.toml`` files, but is optional).

Precedence::

    explicit kwargs  >  environment (AGI_*)  >  config file  >  defaults
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .errors import ConfigurationError
from .policy import DEFAULT_ALLOWLIST

#: Version of the on-disk event schema (bumped whenever events change shape).
SCHEMA_VERSION = "1.0"

#: File name searched for in the working directory when no path is given.
DEFAULT_CONFIG_FILENAME = "agi-kernel.json"

ENV_PREFIX = "AGI_"


@dataclass
class Limits:
    """Resource guard rails.  Every one of them has a hard, finite default."""

    action_timeout_seconds: float = 10.0
    run_timeout_seconds: float = 120.0
    max_actions_per_plan: int = 32
    max_content_bytes: int = 8 * 1024 * 1024  # 8 MiB
    max_sandbox_files: int = 5_000

    def validate(self) -> None:
        if self.action_timeout_seconds <= 0:
            raise ConfigurationError("limits.action_timeout_seconds must be > 0")
        if self.run_timeout_seconds <= 0:
            raise ConfigurationError("limits.run_timeout_seconds must be > 0")
        if self.max_actions_per_plan < 1:
            raise ConfigurationError("limits.max_actions_per_plan must be >= 1")
        if self.max_content_bytes < 1:
            raise ConfigurationError("limits.max_content_bytes must be >= 1")
        if self.max_sandbox_files < 1:
            raise ConfigurationError("limits.max_sandbox_files must be >= 1")


@dataclass
class RetryPolicy:
    """Execution retries.  Off by default: retries never hide bad evidence."""

    max_attempts: int = 1
    backoff_seconds: float = 0.05
    retry_on_timeout: bool = False

    def validate(self) -> None:
        if self.max_attempts < 1:
            raise ConfigurationError("retry.max_attempts must be >= 1")
        if self.backoff_seconds < 0:
            raise ConfigurationError("retry.backoff_seconds must be >= 0")


@dataclass
class Settings:
    """Everything the kernel needs to boot, in one validated object."""

    sandbox_dir: Path
    ledger_path: Optional[Path] = None
    metrics_path: Optional[Path] = None
    log_path: Optional[Path] = None

    log_level: str = "INFO"
    log_json: bool = False

    allowlist: Sequence[str] = field(default_factory=lambda: tuple(sorted(DEFAULT_ALLOWLIST)))
    denylist: Sequence[str] = field(default_factory=tuple)
    max_risk: float = 0.85
    allow_delete: bool = True

    limits: Limits = field(default_factory=Limits)
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    metrics_enabled: bool = True
    schema_version: str = SCHEMA_VERSION

    # -- derived ----------------------------------------------------------- #
    def resolved_ledger_path(self) -> Path:
        """Ledger lives next to the sandbox, never inside it."""
        if self.ledger_path:
            return Path(self.ledger_path)
        return (
            self.sandbox_dir.parent / f"{self.sandbox_dir.name}-ledger" / "events.jsonl"
        )

    def resolved_metrics_path(self) -> Optional[Path]:
        if self.metrics_path:
            return Path(self.metrics_path)
        if self.metrics_enabled:
            return self.resolved_ledger_path().parent / "metrics.json"
        return None

    def validate(self) -> "Settings":
        if not self.sandbox_dir:
            raise ConfigurationError("sandbox_dir is required")
        if not 0.0 <= self.max_risk <= 1.0:
            raise ConfigurationError("max_risk must be within [0, 1]")
        if not self.allowlist:
            raise ConfigurationError("allowlist must not be empty (fail closed)")
        self.limits.validate()
        self.retry.validate()
        return self

    # -- serialisation ------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        return {
            "sandbox_dir": str(self.sandbox_dir),
            "ledger_path": str(self.ledger_path) if self.ledger_path else None,
            "metrics_path": str(self.metrics_path) if self.metrics_path else None,
            "log_path": str(self.log_path) if self.log_path else None,
            "log_level": self.log_level,
            "log_json": self.log_json,
            "allowlist": list(self.allowlist),
            "denylist": list(self.denylist),
            "max_risk": self.max_risk,
            "allow_delete": self.allow_delete,
            "limits": {
                "action_timeout_seconds": self.limits.action_timeout_seconds,
                "run_timeout_seconds": self.limits.run_timeout_seconds,
                "max_actions_per_plan": self.limits.max_actions_per_plan,
                "max_content_bytes": self.limits.max_content_bytes,
                "max_sandbox_files": self.limits.max_sandbox_files,
            },
            "retry": {
                "max_attempts": self.retry.max_attempts,
                "backoff_seconds": self.retry.backoff_seconds,
                "retry_on_timeout": self.retry.retry_on_timeout,
            },
            "metrics_enabled": self.metrics_enabled,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Settings":
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ConfigurationError(
                f"unknown settings: {sorted(unknown)}", unknown=sorted(unknown)
            )
        payload = dict(data)
        limits = payload.pop("limits", None)
        retry = payload.pop("retry", None)
        sandbox = payload.pop("sandbox_dir", None)
        if sandbox is None:
            raise ConfigurationError("sandbox_dir is required in the config file")

        settings = cls(
            sandbox_dir=Path(sandbox).expanduser(),
            ledger_path=_opt_path(payload.pop("ledger_path", None)),
            metrics_path=_opt_path(payload.pop("metrics_path", None)),
            log_path=_opt_path(payload.pop("log_path", None)),
            **payload,
        )
        if limits:
            settings.limits = Limits(**limits)
        if retry:
            settings.retry = RetryPolicy(**retry)
        return settings

    # -- loading ------------------------------------------------------------ #
    @classmethod
    def load(
        cls,
        path: Optional[Path | str] = None,
        *,
        use_env: bool = True,
        **overrides: Any,
    ) -> "Settings":
        """Build settings from defaults < file < env < overrides."""
        data: Dict[str, Any] = {}
        config_path = Path(path) if path else None
        if config_path is None:
            candidate = Path.cwd() / DEFAULT_CONFIG_FILENAME
            config_path = candidate if candidate.is_file() else None
        if config_path is not None:
            if not config_path.is_file():
                raise ConfigurationError(f"config file not found: {config_path}")
            data.update(cls._read_file(config_path))

        if use_env:
            data.update(cls._read_env(os.environ))

        clean = {k: v for k, v in overrides.items() if v is not None}
        data.update(clean)

        if "sandbox_dir" not in data:
            data["sandbox_dir"] = "./sandbox"
        data["sandbox_dir"] = Path(data["sandbox_dir"]).expanduser().resolve()

        settings = cls.from_dict(data)
        return settings.validate()

    def with_overrides(self, **overrides: Any) -> "Settings":
        """Return a copy with the given fields replaced."""
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean).validate()

    # -- internals ---------------------------------------------------------- #
    @staticmethod
    def _read_file(path: Path) -> Dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        suffix = path.suffix.lower()
        if suffix == ".json":
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ConfigurationError(f"invalid JSON in {path}: {exc}") from exc
        elif suffix == ".toml":
            try:
                import tomllib  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - py<3.11
                raise ConfigurationError(
                    "TOML config requires Python 3.11+ (tomllib)"
                ) from exc
            try:
                loaded = tomllib.loads(text)
            except Exception as exc:  # tomllib.TOMLDecodeError
                raise ConfigurationError(f"invalid TOML in {path}: {exc}") from exc
        else:
            raise ConfigurationError(f"unsupported config format: {path.suffix}")
        if not isinstance(loaded, dict):
            raise ConfigurationError(f"config root must be an object: {path}")
        return loaded

    @staticmethod
    def _read_env(env: Mapping[str, str]) -> Dict[str, Any]:
        """Map ``AGI_*`` variables onto settings (flat + ``limits``/``retry``)."""
        out: Dict[str, Any] = {}
        scalars = {
            "SANDBOX_DIR": ("sandbox_dir", str),
            "LEDGER_PATH": ("ledger_path", str),
            "METRICS_PATH": ("metrics_path", str),
            "LOG_PATH": ("log_path", str),
            "LOG_LEVEL": ("log_level", str),
            "MAX_RISK": ("max_risk", float),
        }
        flags = {
            "LOG_JSON": ("log_json", _as_bool),
            "ALLOW_DELETE": ("allow_delete", _as_bool),
            "METRICS_ENABLED": ("metrics_enabled", _as_bool),
        }
        nested: Dict[str, Dict[str, Any]] = {"limits": {}, "retry": {}}
        nested_types = {
            "ACTION_TIMEOUT": ("limits", "action_timeout_seconds", float),
            "RUN_TIMEOUT": ("limits", "run_timeout_seconds", float),
            "MAX_ACTIONS": ("limits", "max_actions_per_plan", int),
            "MAX_CONTENT_BYTES": ("limits", "max_content_bytes", int),
            "MAX_SANDBOX_FILES": ("limits", "max_sandbox_files", int),
            "MAX_ATTEMPTS": ("retry", "max_attempts", int),
            "BACKOFF": ("retry", "backoff_seconds", float),
            "RETRY_ON_TIMEOUT": ("retry", "retry_on_timeout", _as_bool),
        }

        for key, value in env.items():
            if not key.startswith(ENV_PREFIX):
                continue
            name = key[len(ENV_PREFIX):]
            if name in scalars:
                field_name, caster = scalars[name]
                out[field_name] = caster(value)
            elif name in flags:
                field_name, caster = flags[name]
                out[field_name] = caster(value)
            elif name in nested_types:
                group, field_name, caster = nested_types[name]
                nested[group][field_name] = caster(value)
            elif name == "ALLOWLIST":
                out["allowlist"] = [c.strip() for c in value.split(",") if c.strip()]
            elif name == "DENYLIST":
                out["denylist"] = [c.strip() for c in value.split(",") if c.strip()]

        for group, values in nested.items():
            if values:
                out[group] = {**(out.get(group) or {}), **values}
        return out


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _opt_path(value: Any) -> Optional[Path]:
    return Path(value).expanduser() if value else None


__all__ = ["Settings", "Limits", "RetryPolicy", "SCHEMA_VERSION", "DEFAULT_CONFIG_FILENAME"]
