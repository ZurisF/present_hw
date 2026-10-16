"""Idempotent tool execution and recovery decisions for side-effecting tools.

The repository passed to this module must enforce a unique, durable operation_id.
For a reservation, that key must be stored in the same transaction as the
reservation row and its SUCCEEDED state.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Protocol, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")


class OperationState(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass(frozen=True)
class StoredOperation:
    state: OperationState
    result: Any = None
    error_code: str | None = None


@dataclass(frozen=True)
class ToolOutcome:
    state: OperationState
    result: Any = None
    message: str = ""
    error_code: str | None = None
    attempts: int = 0
    operation_id: str | None = None


class OperationJournal(Protocol):
    """Durable operation ledger and append-only, sanitized event journal.

    ``lookup`` must check both the operation row and its side-effect row using
    the same idempotency key. State updates must be conditional so a late
    NEEDS_REVIEW update cannot overwrite a terminal SUCCEEDED result.
    """

    def lookup(self, operation_id: str) -> StoredOperation | None: ...

    def append_event(
        self,
        operation_id: str,
        step: str,
        attempt: int,
        state: OperationState,
        error_code: str | None = None,
    ) -> None: ...

    def mark_needs_review(self, operation_id: str, error_code: str) -> None: ...

    def mark_rejected(self, operation_id: str, error_code: str) -> None: ...

    def mark_failed(self, operation_id: str, error_code: str) -> None: ...


class BusinessRuleViolation(Exception):
    """A final business outcome such as a missing/unavailable dish."""

    def __init__(self, error_code: str, user_message: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.user_message = user_message


class TransientToolError(Exception):
    """A known transient error safe to retry with the same operation ID."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class PermanentToolError(Exception):
    """A non-retryable technical failure with a stable, sanitized code."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0
    jitter_fraction: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.initial_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays cannot be negative")
        if not 0 <= self.jitter_fraction <= 1:
            raise ValueError("jitter_fraction must be between 0 and 1")

    def delay_for(self, failed_attempt: int) -> float:
        base = min(
            self.initial_delay_seconds * (2 ** max(0, failed_attempt - 1)),
            self.max_delay_seconds,
        )
        return base * (
            1 + random.uniform(-self.jitter_fraction, self.jitter_fraction)
        )


def _record(
    journal: OperationJournal,
    operation_id: str,
    step: str,
    attempt: int,
    state: OperationState,
    error_code: str | None = None,
) -> None:
    try:
        journal.append_event(operation_id, step, attempt, state, error_code)
    except Exception:
        # A journal outage must not cause a second execution of a completed write.
        logger.error(
            "operation event could not be persisted",
            extra={
                "operation_id": operation_id,
                "step": step,
                "attempt": attempt,
                "state": state.value,
                "error_code": "JOURNAL_WRITE_FAILED",
            },
        )


def _outcome_from_stored(
    stored: StoredOperation,
    attempts: int,
    operation_id: str | None = None,
) -> ToolOutcome:
    if stored.state is OperationState.SUCCEEDED:
        return ToolOutcome(
            state=stored.state,
            result=stored.result,
            message="The operation already completed; returning its stored result.",
            attempts=attempts,
            operation_id=operation_id,
        )
    if stored.state is OperationState.REJECTED:
        return ToolOutcome(
            state=stored.state,
            result=stored.result,
            message="The operation was rejected by a business rule.",
            error_code=stored.error_code,
            attempts=attempts,
            operation_id=operation_id,
        )
    if stored.state is OperationState.FAILED:
        return ToolOutcome(
            state=stored.state,
            message="The operation failed and was not completed.",
            error_code=stored.error_code,
            attempts=attempts,
            operation_id=operation_id,
        )
    return ToolOutcome(
        state=OperationState.NEEDS_REVIEW,
        result=stored.result,
        message="The operation is still pending verification.",
        error_code=stored.error_code or "OPERATION_PENDING",
        attempts=attempts,
        operation_id=operation_id,
    )


def run_idempotent_tool(
    *,
    operation_id: str,
    execute: Callable[[str], T],
    journal: OperationJournal,
    retry_policy: RetryPolicy = RetryPolicy(),
    sleep: Callable[[float], None] = time.sleep,
) -> ToolOutcome:
    """Run or resume a side effect using one stable idempotency key.

    ``execute`` must be safe to call more than once with the same operation ID.
    Unknown exceptions and unavailable status checks are never blindly retried.
    """
    if not operation_id.strip():
        raise ValueError("operation_id must be non-empty and stable across retries")

    try:
        stored = journal.lookup(operation_id)
    except Exception:
        return ToolOutcome(
            state=OperationState.NEEDS_REVIEW,
            message="The saved operation status cannot be read; verify before retrying.",
            error_code="STATUS_LOOKUP_FAILED",
            operation_id=operation_id,
        )

    if stored is not None and stored.state is not OperationState.PENDING:
        return _outcome_from_stored(stored, attempts=0, operation_id=operation_id)

    for attempt in range(1, retry_policy.max_attempts + 1):
        _record(
            journal,
            operation_id,
            "TOOL_STARTED",
            attempt,
            OperationState.PENDING,
        )
        try:
            result = execute(operation_id)
        except BusinessRuleViolation as exc:
            try:
                journal.mark_rejected(operation_id, exc.error_code)
            except Exception:
                return _needs_review(
                    journal,
                    operation_id,
                    attempt,
                    "STATUS_UPDATE_FAILED",
                )
            _record(
                journal,
                operation_id,
                "BUSINESS_REJECTED",
                attempt,
                OperationState.REJECTED,
                exc.error_code,
            )
            return ToolOutcome(
                state=OperationState.REJECTED,
                message=exc.user_message,
                error_code=exc.error_code,
                attempts=attempt,
                operation_id=operation_id,
            )
        except PermanentToolError as exc:
            try:
                journal.mark_failed(operation_id, exc.error_code)
            except Exception:
                return _needs_review(
                    journal,
                    operation_id,
                    attempt,
                    "STATUS_UPDATE_FAILED",
                )
            _record(
                journal,
                operation_id,
                "TOOL_FAILED",
                attempt,
                OperationState.FAILED,
                exc.error_code,
            )
            return ToolOutcome(
                state=OperationState.FAILED,
                message="The operation failed before completion.",
                error_code=exc.error_code,
                attempts=attempt,
                operation_id=operation_id,
            )
        except TransientToolError as exc:
            _record(
                journal,
                operation_id,
                "TOOL_TIMEOUT_OR_TRANSIENT_ERROR",
                attempt,
                OperationState.PENDING,
                exc.error_code,
            )
            try:
                stored = journal.lookup(operation_id)
            except Exception:
                return _needs_review(
                    journal,
                    operation_id,
                    attempt,
                    "STATUS_LOOKUP_FAILED",
                )

            if stored is not None and stored.state is not OperationState.PENDING:
                return _outcome_from_stored(
                    stored,
                    attempts=attempt,
                    operation_id=operation_id,
                )
            if attempt == retry_policy.max_attempts:
                return _needs_review(
                    journal,
                    operation_id,
                    attempt,
                    exc.error_code,
                )
            _record(
                journal,
                operation_id,
                "TOOL_RETRYING",
                attempt,
                OperationState.PENDING,
                exc.error_code,
            )
            sleep(retry_policy.delay_for(attempt))
            continue
        except Exception:
            # Do not expose raw exception text or assume an unknown failure is safe to retry.
            _record(
                journal,
                operation_id,
                "UNCLASSIFIED_TOOL_ERROR",
                attempt,
                OperationState.NEEDS_REVIEW,
                "UNCLASSIFIED_TOOL_ERROR",
            )
            try:
                stored = journal.lookup(operation_id)
            except Exception:
                return _needs_review(
                    journal,
                    operation_id,
                    attempt,
                    "STATUS_LOOKUP_FAILED",
                )
            if stored is not None and stored.state is not OperationState.PENDING:
                return _outcome_from_stored(
                    stored,
                    attempts=attempt,
                    operation_id=operation_id,
                )
            return _needs_review(
                journal,
                operation_id,
                attempt,
                "UNCLASSIFIED_TOOL_ERROR",
            )

        _record(
            journal,
            operation_id,
            "TOOL_SUCCEEDED",
            attempt,
            OperationState.SUCCEEDED,
        )
        return ToolOutcome(
            state=OperationState.SUCCEEDED,
            result=result,
            message="The operation completed successfully.",
            attempts=attempt,
            operation_id=operation_id,
        )

    return _needs_review(
        journal,
        operation_id,
        retry_policy.max_attempts,
        "RETRY_LIMIT_REACHED",
    )


def _needs_review(
    journal: OperationJournal,
    operation_id: str,
    attempts: int,
    error_code: str,
) -> ToolOutcome:
    try:
        journal.mark_needs_review(operation_id, error_code)
    except Exception:
        logger.error(
            "operation could not be marked for review",
            extra={
                "operation_id": operation_id,
                "error_code": "STATUS_UPDATE_FAILED",
            },
        )
    _record(
        journal,
        operation_id,
        "NEEDS_REVIEW",
        attempts,
        OperationState.NEEDS_REVIEW,
        error_code,
    )
    return ToolOutcome(
        state=OperationState.NEEDS_REVIEW,
        message="The final result is uncertain; check the database before any new attempt.",
        error_code=error_code,
        attempts=attempts,
        operation_id=operation_id,
    )
