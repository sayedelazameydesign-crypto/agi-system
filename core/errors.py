"""Typed error taxonomy for the kernel.

Every error carries a stable ``code`` so operators and CI can branch on it
instead of parsing message strings.
"""

from __future__ import annotations

from typing import Any, Dict


class KernelError(Exception):
    """Base class for every kernel error."""

    code: str = "KERNEL_ERROR"
    http_status: int = 500

    def __init__(self, message: str = "", **context: Any) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.context: Dict[str, Any] = context

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "context": self.context}


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
class ConfigurationError(KernelError):
    code = "CONFIGURATION_ERROR"
    http_status = 400


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
class PlanningError(KernelError):
    """A goal could not be turned into any plan."""

    code = "PLANNING_FAILED"
    http_status = 422


class UnsupportedGoalError(PlanningError):
    code = "UNSUPPORTED_GOAL"


class MissingParameterError(PlanningError):
    code = "MISSING_PARAMETER"


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #
class PolicyViolation(KernelError):
    code = "POLICY_VIOLATION"
    http_status = 403


class CapabilityDeniedError(PolicyViolation):
    code = "CAPABILITY_DENIED"


class SandboxViolationError(PolicyViolation):
    code = "SANDBOX_VIOLATION"


class RiskLimitExceededError(PolicyViolation):
    code = "RISK_LIMIT_EXCEEDED"


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
class ExecutionError(KernelError):
    code = "EXECUTION_FAILED"
    http_status = 500


class CapabilityNotFoundError(ExecutionError):
    code = "CAPABILITY_NOT_FOUND"
    http_status = 501


class ExecutionTimeoutError(ExecutionError):
    code = "EXECUTION_TIMEOUT"
    http_status = 504


class RunTimeoutError(ExecutionError):
    code = "RUN_TIMEOUT"
    http_status = 504


class QuotaExceededError(ExecutionError):
    code = "QUOTA_EXCEEDED"
    http_status = 429


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #
class VerificationError(KernelError):
    code = "VERIFICATION_FAILED"
    http_status = 409


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
class LedgerError(KernelError):
    code = "LEDGER_ERROR"
    http_status = 500


class LedgerCorruptionError(LedgerError):
    code = "LEDGER_CORRUPTION"


class LedgerLockError(LedgerError):
    code = "LEDGER_LOCK_ERROR"
    http_status = 503


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #
class AbortedError(KernelError):
    code = "ABORTED"
    http_status = 499


__all__ = [
    "KernelError",
    "ConfigurationError",
    "PlanningError",
    "UnsupportedGoalError",
    "MissingParameterError",
    "PolicyViolation",
    "CapabilityDeniedError",
    "SandboxViolationError",
    "RiskLimitExceededError",
    "ExecutionError",
    "CapabilityNotFoundError",
    "ExecutionTimeoutError",
    "RunTimeoutError",
    "QuotaExceededError",
    "VerificationError",
    "LedgerError",
    "LedgerCorruptionError",
    "LedgerLockError",
    "AbortedError",
]
