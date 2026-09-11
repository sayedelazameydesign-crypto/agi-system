"""Executor layer: the only component that touches the outside world.

Design rules:
  * handlers never raise -- failures come back as ``Observation(ok=False)``
  * handlers do not check permissions (that is :class:`PolicyEngine`'s job)
  * the sandbox boundary is re-checked as defence in depth
  * writes are atomic (temp file + ``os.replace`` + ``fsync``)
  * every action can be bounded by a timeout (cooperative: the worker thread
    is a daemon, so a hung syscall cannot block the kernel forever)
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from .errors import ExecutionTimeoutError, QuotaExceededError
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

    def __init__(
        self,
        sandbox_dir: os.PathLike | str,
        *,
        default_timeout: Optional[float] = None,
        max_files: Optional[int] = None,
    ) -> None:
        self.sandbox_root = Path(sandbox_dir).resolve()
        self.sandbox_root.mkdir(parents=True, exist_ok=True)
        self.default_timeout = default_timeout
        self.max_files = max_files
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

    def execute(self, action: Action, timeout: Optional[float] = None) -> Observation:
        """Execute one action.  Never raises: errors become ``ok=False``."""
        handler = self.handlers.get(action.capability)
        if handler is None:
            return self._fail(
                action, f"unsupported capability {action.capability!r}", code="CAPABILITY_NOT_FOUND"
            )

        resolved, err = self._resolve(action)
        if err is not None:
            return self._fail(action, err, code="SANDBOX_VIOLATION")

        assert resolved is not None
        if self._quota_exceeded(action, resolved):
            return self._fail(
                action,
                f"sandbox file quota exceeded (max_files={self.max_files})",
                code="QUOTA_EXCEEDED",
            )

        return self._run_guarded(handler, action, resolved, timeout)

    # -- capability handlers ------------------------------------------------ #
    def _write(self, action: Action, path: Path) -> Observation:
        content: Any = action.params.get("content", "")
        if not isinstance(content, (str, bytes)):
            content = str(content)
        data = content.encode("utf-8") if isinstance(content, str) else content

        if action.params.get("append") and path.exists():
            with open(path, "ab") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(path, data)

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
                    data={
                        "path": self._display(path),
                        "content": "",
                        "sha256": sha256_of(b""),
                        "existed": False,
                    },
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
                return self._fail(
                    action,
                    "directory is not empty (recursive=True required)",
                    code="NOT_EMPTY",
                )
            path.rmdir()
        else:
            path.unlink()
        self._fsync_dir(path.parent)
        return Observation(
            capability=action.capability,
            action_id=action.action_id,
            ok=True,
            data={"path": self._display(path), "deleted": True},
        )

    def _mkdir(self, action: Action, path: Path) -> Observation:
        path.mkdir(parents=True, exist_ok=True)
        self._fsync_dir(path.parent)
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
        entries = self._entries(path)
        return Observation(
            capability=action.capability,
            action_id=action.action_id,
            ok=True,
            data={"path": self._display(path), "entries": entries, "count": len(entries)},
        )

    # -- durability helpers -------------------------------------------------- #
    @staticmethod
    def _entries(path: Path) -> list:
        return sorted(
            {f"{p.name}/" for p in path.iterdir() if p.is_dir()}
            | {p.name for p in path.iterdir() if not p.is_dir()}
        )

    def _atomic_write(self, path: Path, data: bytes) -> None:
        """Write via a temp file then rename: readers never see a partial file."""
        tmp = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:  # pragma: no cover - best effort
                    pass
        self._fsync_dir(path.parent)

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """Best effort directory fsync (not supported on every platform)."""
        try:
            fd = os.open(str(directory), os.O_RDONLY)
        except OSError:  # pragma: no cover - platform dependent
            return
        try:
            os.fsync(fd)
        except OSError:  # pragma: no cover - platform dependent
            pass
        finally:
            os.close(fd)

    # -- timeouts / quotas --------------------------------------------------- #
    def _run_guarded(
        self,
        handler: Callable[[Action, Path], Observation],
        action: Action,
        path: Path,
        timeout: Optional[float],
    ) -> Observation:
        limit = timeout if timeout is not None else self.default_timeout
        box: Dict[str, Any] = {}

        def target() -> None:
            try:
                box["value"] = handler(action, path)
            except BaseException as exc:  # noqa: BLE001 - reported, never raised
                box["error"] = exc

        worker = threading.Thread(target=target, name=f"exec-{action.capability}", daemon=True)
        worker.start()
        worker.join(limit)

        if worker.is_alive():
            return self._fail(
                action,
                f"timeout after {limit}s while running {action.capability}",
                code="EXECUTION_TIMEOUT",
                timeout=limit,
            )

        if "error" in box:
            exc = box["error"]
            # KeyboardInterrupt / SystemExit must never be swallowed: they
            # belong to the process, not to the action.
            if not isinstance(exc, Exception):
                raise exc
            return self._map_error(action, exc)
        return box["value"]

    def _map_error(self, action: Action, exc: BaseException) -> Observation:
        code = type(exc).__name__.upper()
        if isinstance(exc, FileNotFoundError):
            code = "NOT_FOUND"
        elif isinstance(exc, NotADirectoryError):
            code = "NOT_A_DIRECTORY"
        elif isinstance(exc, IsADirectoryError):
            code = "IS_A_DIRECTORY"
        elif isinstance(exc, PermissionError):
            code = "PERMISSION_DENIED"
        elif isinstance(exc, OSError):
            code = "OS_ERROR"
        elif isinstance(exc, ExecutionTimeoutError):
            code = "EXECUTION_TIMEOUT"
        elif isinstance(exc, QuotaExceededError):
            code = "QUOTA_EXCEEDED"
        return self._fail(action, f"{type(exc).__name__}: {exc}", code=code)

    def _quota_exceeded(self, action: Action, path: Path) -> bool:
        if self.max_files is None:
            return False
        if action.capability not in ("filesystem.write", "filesystem.mkdir"):
            return False
        if path.exists():
            return False
        return self.count_files() >= self.max_files

    def count_files(self) -> int:
        """Number of entries currently inside the sandbox (bounded walk)."""
        total = 0
        for _root, _dirs, files in os.walk(self.sandbox_root):
            total += len(files)
            if self.max_files is not None and total >= self.max_files:
                return total
        return total

    # -- helpers ------------------------------------------------------------ #
    def _resolve(self, action: Action) -> Tuple[Optional[Path], Optional[str]]:
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
    def _fail(
        action: Action, error: str, code: str = "EXECUTION_FAILED", **extra: Any
    ) -> Observation:
        data: Dict[str, Any] = {"path": str(action.params.get("path", "")), "error_code": code}
        data.update(extra)
        return Observation(
            capability=action.capability,
            action_id=action.action_id,
            ok=False,
            data=data,
            error=error,
        )


__all__ = ["Executor", "sha256_of", "sha256_file"]
