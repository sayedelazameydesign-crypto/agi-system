"""Executor layer: the only component that touches the outside world.

Design rules:
  * handlers never raise -- failures come back as ``Observation(ok=False)``
  * handlers do not check permissions (that is :class:`PolicyEngine`'s job)
  * the sandbox boundary is re-checked as defence in depth
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .models import Action, Observation


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: os.PathLike | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Executor:
    """Runs allowlisted capabilities and reports what it *claims* happened."""

    def __init__(self, sandbox_dir: os.PathLike | str) -> None:
        self.sandbox_root = Path(sandbox_dir).resolve()
        self.sandbox_root.mkdir(parents=True, exist_ok=True)
        self.handlers: Dict[str, Callable[[Action, Path], Observation]] = {
            "filesystem.write": self._write,
            "filesystem.read": self._read,
            "filesystem.delete": self._delete,
            "filesystem.mkdir": self._mkdir,
            "filesystem.list": self._list,
        }

    # -- public API --------------------------------------------------------- #
    def supports(self, capability: str) -> bool:
        return capability in self.handlers

    def execute(self, action: Action) -> Observation:
        """Execute one action.  Never raises: errors become ``ok=False``."""
        handler = self.handlers.get(action.capability)
        if handler is None:
            return self._fail(action, f"unsupported capability {action.capability!r}")

        resolved, err = self._resolve(action)
        if err is not None:
            return self._fail(action, err)

        assert resolved is not None
        try:
            return handler(action, resolved)
        except FileNotFoundError as exc:
            return self._fail(action, f"not found: {exc}")
        except NotADirectoryError as exc:
            return self._fail(action, f"not a directory: {exc}")
        except IsADirectoryError as exc:
            return self._fail(action, f"is a directory: {exc}")
        except PermissionError as exc:
            return self._fail(action, f"permission denied: {exc}")
        except OSError as exc:
            return self._fail(action, f"os error: {exc}")
        except Exception as exc:  # pragma: no cover - last-resort guard
            return self._fail(action, f"unexpected error: {type(exc).__name__}: {exc}")

    # -- capability handlers ------------------------------------------------ #
    def _write(self, action: Action, path: Path) -> Observation:
        content: Any = action.params.get("content", "")
        if not isinstance(content, (str, bytes)):
            content = str(content)
        data = content.encode("utf-8") if isinstance(content, str) else content

        if action.params.get("append") and path.exists():
            with open(path, "ab") as handle:
                handle.write(data)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(data)

        return Observation(
            capability=action.capability,
            action_id=action.action_id,
            ok=True,
            data={
                "path": self._display(path),
                "bytes": len(data),
                "sha256": sha256_file(path),
                "content": content.decode("utf-8", "replace")
                if isinstance(content, bytes)
                else content,
            },
        )

    def _read(self, action: Action, path: Path) -> Observation:
        if not path.exists():
            if action.params.get("missing_ok"):
                return Observation(
                    capability=action.capability,
                    action_id=action.action_id,
                    ok=True,
                    data={"path": self._display(path), "content": "", "sha256": sha256_of(b""),
                          "existed": False},
                )
            raise FileNotFoundError(str(path))
        raw = path.read_bytes()
        return Observation(
            capability=action.capability,
            action_id=action.action_id,
            ok=True,
            data={
                "path": self._display(path),
                "content": raw.decode("utf-8", "replace"),
                "sha256": sha256_of(raw),
                "bytes": len(raw),
                "existed": True,
            },
        )

    def _delete(self, action: Action, path: Path) -> Observation:
        if not path.exists():
            if action.params.get("missing_ok"):
                return Observation(
                    capability=action.capability,
                    action_id=action.action_id,
                    ok=True,
                    data={"path": self._display(path), "deleted": False},
                )
            raise FileNotFoundError(str(path))
        if path.is_dir() and not path.is_symlink():
            recursive = bool(action.params.get("recursive"))
            if any(path.iterdir()) and not recursive:
                return self._fail(action, "directory is not empty (recursive=True required)")
            path.rmdir()
        else:
            path.unlink()
        return Observation(
            capability=action.capability,
            action_id=action.action_id,
            ok=True,
            data={"path": self._display(path), "deleted": True},
        )

    def _mkdir(self, action: Action, path: Path) -> Observation:
        path.mkdir(parents=True, exist_ok=True)
        return Observation(
            capability=action.capability,
            action_id=action.action_id,
            ok=True,
            data={"path": self._display(path), "created": True},
        )

    def _list(self, action: Action, path: Path) -> Observation:
        if not path.exists():
            raise FileNotFoundError(str(path))
        if not path.is_dir():
            raise NotADirectoryError(str(path))
        entries = sorted(
            {f"{p.name}/" for p in path.iterdir() if p.is_dir()}
            | {p.name for p in path.iterdir() if not p.is_dir()}
        )
        return Observation(
            capability=action.capability,
            action_id=action.action_id,
            ok=True,
            data={"path": self._display(path), "entries": entries, "count": len(entries)},
        )

    # -- helpers ------------------------------------------------------------ #
    def _resolve(self, action: Action) -> tuple[Optional[Path], Optional[str]]:
        raw = action.params.get("path")
        if raw is None:
            return None, "action is missing the required 'path' param"
        try:
            candidate = Path(raw)
            resolved = (
                candidate.resolve()
                if candidate.is_absolute()
                else (self.sandbox_root / candidate).resolve()
            )
        except (OSError, ValueError) as exc:
            return None, f"path {str(raw)!r} is not resolvable: {exc}"
        try:
            resolved.relative_to(self.sandbox_root)
        except ValueError:
            return None, f"path escapes the sandbox: {str(raw)!r}"
        return resolved, None

    def _display(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.sandbox_root))
        except ValueError:
            return str(path)

    @staticmethod
    def _fail(action: Action, error: str) -> Observation:
        return Observation(
            capability=action.capability,
            action_id=action.action_id,
            ok=False,
            data={"path": str(action.params.get("path", ""))},
            error=error,
        )


__all__ = ["Executor", "sha256_of", "sha256_file"]
