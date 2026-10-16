"""MySQL-backed, idempotent reservation operations for the course project."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import pymysql
from dotenv import load_dotenv
from pymysql.cursors import DictCursor

from agent.recovery import (
    OperationState,
    RetryPolicy,
    StoredOperation,
    ToolOutcome,
    TransientToolError,
    run_idempotent_tool,
)

TRANSIENT_MYSQL_CODES = {1205, 1213, 2006, 2013}


@dataclass(frozen=True)
class ReservationRequest:
    request_id: str
    thread_id: str
    num_people: int
    num_children: int
    arrival_time: datetime
    seat_preference: str
    main_dish_preference: str
    other_comments: str = ""

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.thread_id.strip():
            raise ValueError("request_id and thread_id are required")
        if self.num_people < 1 or self.num_children < 0:
            raise ValueError("guest counts are invalid")
        if self.num_children > self.num_people:
            raise ValueError("children cannot exceed the total number of guests")
        if not self.seat_preference.strip() or not self.main_dish_preference.strip():
            raise ValueError("seat and main dish preferences are required")

    def payload(self) -> dict[str, Any]:
        values = asdict(self)
        values["arrival_time"] = self.arrival_time.isoformat(timespec="seconds")
        return values


class IdempotencyConflict(ValueError):
    """A request ID was reused for different reservation data."""


class MysqlReservationService:
    """Create or resume reservations while preserving one request ID per action.

    The application must create and persist ``request_id`` after the user's
    explicit confirmation. Never derive it from model-generated text.
    """

    def __init__(
        self,
        *,
        retry_policy: RetryPolicy = RetryPolicy(),
        connect_timeout: int = 5,
    ) -> None:
        load_dotenv()
        self.retry_policy = retry_policy
        self.connect_timeout = connect_timeout

    def _connect(self):
        required = ("HOST", "USER", "PASSWORD", "DATABASE", "PORT", "CHARSET")
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise ValueError("database configuration is incomplete")
        return pymysql.connect(
            host=os.environ["HOST"],
            user=os.environ["USER"],
            password=os.environ["PASSWORD"],
            database=os.environ["DATABASE"],
            port=int(os.environ["PORT"]),
            charset=os.environ["CHARSET"],
            connect_timeout=self.connect_timeout,
            read_timeout=10,
            write_timeout=10,
            autocommit=False,
            cursorclass=DictCursor,
        )

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def prepare(self, request: ReservationRequest) -> StoredOperation:
        """Persist the confirmed payload before starting the side effect."""
        payload = request.payload()
        payload_hash = self._payload_hash(payload)
        connection = self._connect()
        try:
            connection.begin()
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT IGNORE INTO agent_operations
                        (request_id, thread_id, operation_type, payload_sha256,
                         payload_json, status)
                    VALUES (%s, %s, 'RESERVATION', %s, %s, 'PENDING')
                    """,
                    (
                        request.request_id,
                        request.thread_id,
                        payload_hash,
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
                if cursor.rowcount == 1:
                    cursor.execute(
                        """
                        INSERT INTO agent_operation_events
                            (request_id, step, attempt, state)
                        VALUES (%s, 'OPERATION_PREPARED', 0, 'PENDING')
                        """,
                        (request.request_id,),
                    )
                cursor.execute(
                    """
                    SELECT thread_id, payload_sha256, status, reservation_id, error_code
                    FROM agent_operations WHERE request_id = %s FOR UPDATE
                    """,
                    (request.request_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("operation row was not created")
                if row["thread_id"] != request.thread_id or row["payload_sha256"] != payload_hash:
                    raise IdempotencyConflict(
                        "request_id is already associated with a different operation"
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.lookup(request.request_id) or StoredOperation(OperationState.PENDING)

    def lookup(self, operation_id: str) -> StoredOperation | None:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT o.status, o.error_code, r.id AS reservation_id,
                           r.num_people, r.num_children, r.arrival_time,
                           r.seat_preference, r.main_dish_preference,
                           r.other_comments, r.status AS reservation_status
                    FROM agent_operations AS o
                    LEFT JOIN reservations AS r ON r.request_id = o.request_id
                    WHERE o.request_id = %s
                    """,
                    (operation_id,),
                )
                row = cursor.fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        if row["reservation_id"] is not None:
            result = {
                key: row[key]
                for key in (
                    "reservation_id",
                    "num_people",
                    "num_children",
                    "arrival_time",
                    "seat_preference",
                    "main_dish_preference",
                    "other_comments",
                    "reservation_status",
                )
            }
            return StoredOperation(OperationState.SUCCEEDED, result=result)
        return StoredOperation(
            state=OperationState(row["status"]),
            error_code=row["error_code"],
        )

    def append_event(
        self,
        operation_id: str,
        step: str,
        attempt: int,
        state: OperationState,
        error_code: str | None = None,
    ) -> None:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO agent_operation_events
                        (request_id, step, attempt, state, error_code)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (operation_id, step, attempt, state.value, error_code),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_needs_review(self, operation_id: str, error_code: str) -> None:
        self._set_pending_terminal(operation_id, OperationState.NEEDS_REVIEW, error_code)

    def mark_rejected(self, operation_id: str, error_code: str) -> None:
        self._set_pending_terminal(operation_id, OperationState.REJECTED, error_code)

    def mark_failed(self, operation_id: str, error_code: str) -> None:
        self._set_pending_terminal(operation_id, OperationState.FAILED, error_code)

    def _set_pending_terminal(
        self,
        operation_id: str,
        state: OperationState,
        error_code: str,
    ) -> None:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE agent_operations
                    SET status = %s, error_code = %s
                    WHERE request_id = %s AND status = 'PENDING'
                    """,
                    (state.value, error_code, operation_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _execute_reservation(self, operation_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            connection.begin()
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT payload_json, status, reservation_id
                    FROM agent_operations WHERE request_id = %s FOR UPDATE
                    """,
                    (operation_id,),
                )
                operation = cursor.fetchone()
                if operation is None:
                    raise ValueError("operation must be prepared before execution")
                if operation["status"] == OperationState.SUCCEEDED.value:
                    cursor.execute(
                        "SELECT id, num_people, num_children, arrival_time, status "
                        "FROM reservations WHERE request_id = %s",
                        (operation_id,),
                    )
                    existing = cursor.fetchone()
                    connection.commit()
                    if existing is None:
                        raise RuntimeError("succeeded operation has no reservation row")
                    return existing
                if operation["status"] != OperationState.PENDING.value:
                    raise ValueError("operation is not pending")

                payload = operation["payload_json"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                cursor.execute(
                    """
                    INSERT INTO reservations
                        (request_id, thread_id, num_people, num_children,
                         arrival_time, seat_preference, main_dish_preference,
                         other_comments)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        operation_id,
                        payload["thread_id"],
                        payload["num_people"],
                        payload["num_children"],
                        datetime.fromisoformat(payload["arrival_time"]),
                        payload["seat_preference"],
                        payload["main_dish_preference"],
                        payload["other_comments"],
                    ),
                )
                reservation_id = cursor.lastrowid
                cursor.execute(
                    """
                    UPDATE agent_operations
                    SET status = 'SUCCEEDED', reservation_id = %s, error_code = NULL
                    WHERE request_id = %s AND status = 'PENDING'
                    """,
                    (reservation_id, operation_id),
                )
                cursor.execute(
                    """
                    INSERT INTO agent_operation_events
                        (request_id, step, attempt, state)
                    VALUES (%s, 'RESERVATION_COMMITTED', 0, 'SUCCEEDED')
                    """,
                    (operation_id,),
                )
            connection.commit()
            return {
                "reservation_id": reservation_id,
                "request_id": operation_id,
                "status": "CONFIRMED",
            }
        except pymysql.err.OperationalError as exc:
            connection.rollback()
            error_number = exc.args[0] if exc.args else None
            if error_number in TRANSIENT_MYSQL_CODES:
                raise TransientToolError("DATABASE_TRANSIENT_ERROR") from None
            raise RuntimeError("database operation failed") from None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_or_resume(self, request: ReservationRequest) -> ToolOutcome:
        """Persist confirmed input, reconcile current state, then run safely."""
        try:
            stored = self.prepare(request)
        except IdempotencyConflict:
            return ToolOutcome(
                state=OperationState.FAILED,
                message="This request ID belongs to a different reservation.",
                error_code="IDEMPOTENCY_CONFLICT",
                operation_id=request.request_id,
            )
        except Exception:
            # Preparation itself is a write. If its response was lost, inspect the
            # same ID before asking the caller to start a different operation.
            try:
                stored = self.lookup(request.request_id)
            except Exception:
                stored = None
            if stored is None:
                return ToolOutcome(
                    state=OperationState.NEEDS_REVIEW,
                    message="The operation record could not be verified; check before retrying.",
                    error_code="PREPARE_STATUS_UNKNOWN",
                    operation_id=request.request_id,
                )

        if stored.state is OperationState.SUCCEEDED:
            return ToolOutcome(
                state=stored.state,
                result=stored.result,
                message="The reservation already exists.",
                operation_id=request.request_id,
            )
        if stored.state in {OperationState.REJECTED, OperationState.FAILED}:
            return ToolOutcome(
                state=stored.state,
                message="The stored operation is terminal and was not repeated.",
                error_code=stored.error_code,
                operation_id=request.request_id,
            )
        if stored.state is OperationState.NEEDS_REVIEW:
            try:
                reconciled = self.reconcile(request.request_id)
            except Exception:
                return ToolOutcome(
                    state=OperationState.NEEDS_REVIEW,
                    message="The database status is still unavailable; leave this operation pending verification.",
                    error_code="RECONCILE_FAILED",
                    operation_id=request.request_id,
                )
            if reconciled.state is not OperationState.PENDING:
                return ToolOutcome(
                    state=reconciled.state,
                    result=reconciled.result,
                    message="The previous operation was checked against the database.",
                    error_code=reconciled.error_code,
                    operation_id=request.request_id,
                )

        return run_idempotent_tool(
            operation_id=request.request_id,
            execute=self._execute_reservation,
            journal=self,
            retry_policy=self.retry_policy,
        )

    def reconcile(self, operation_id: str) -> StoredOperation:
        """Check the side-effect row before reopening a pending review item."""
        connection = self._connect()
        try:
            connection.begin()
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT status, error_code FROM agent_operations
                    WHERE request_id = %s FOR UPDATE
                    """,
                    (operation_id,),
                )
                operation = cursor.fetchone()
                if operation is None:
                    raise ValueError("operation does not exist")
                cursor.execute(
                    "SELECT id FROM reservations WHERE request_id = %s",
                    (operation_id,),
                )
                reservation = cursor.fetchone()
                if reservation is not None:
                    cursor.execute(
                        """
                        UPDATE agent_operations
                        SET status = 'SUCCEEDED', reservation_id = %s, error_code = NULL
                        WHERE request_id = %s AND status IN ('PENDING', 'NEEDS_REVIEW')
                        """,
                        (reservation["id"], operation_id),
                    )
                    connection.commit()
                    return self.lookup(operation_id) or StoredOperation(
                        OperationState.NEEDS_REVIEW,
                        error_code="RECONCILE_READ_FAILED",
                    )
                if operation["status"] in {
                    OperationState.PENDING.value,
                    OperationState.NEEDS_REVIEW.value,
                }:
                    cursor.execute(
                        """
                        UPDATE agent_operations SET status = 'PENDING', error_code = NULL
                        WHERE request_id = %s AND status IN ('PENDING', 'NEEDS_REVIEW')
                        """,
                        (operation_id,),
                    )
                    connection.commit()
                    return StoredOperation(OperationState.PENDING)
                connection.commit()
                return StoredOperation(
                    OperationState(operation["status"]),
                    error_code=operation["error_code"],
                )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recover_thread(self, thread_id: str) -> list[ToolOutcome]:
        """Find and reconcile unfinished writes after a worker/thread restart.

        The caller must use the same stable thread_id as the original run. Each
        pending operation is checked against reservations before the same key
        is allowed to execute again.
        """
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT request_id FROM agent_operations
                    WHERE thread_id = %s AND status IN ('PENDING', 'NEEDS_REVIEW')
                    ORDER BY created_at
                    """,
                    (thread_id,),
                )
                operation_ids = [row["request_id"] for row in cursor.fetchall()]
        finally:
            connection.close()

        outcomes: list[ToolOutcome] = []
        for operation_id in operation_ids:
            try:
                reconciled = self.reconcile(operation_id)
            except Exception:
                outcomes.append(
                    ToolOutcome(
                        state=OperationState.NEEDS_REVIEW,
                        message="The database status is still unavailable; verify before retrying.",
                        error_code="RECONCILE_FAILED",
                        operation_id=operation_id,
                    )
                )
                continue
            if reconciled.state is not OperationState.PENDING:
                outcomes.append(
                    ToolOutcome(
                        state=reconciled.state,
                        result=reconciled.result,
                        message="Recovered operation was checked against the database.",
                        error_code=reconciled.error_code,
                        operation_id=operation_id,
                    )
                )
                continue
            outcomes.append(
                run_idempotent_tool(
                    operation_id=operation_id,
                    execute=self._execute_reservation,
                    journal=self,
                    retry_policy=self.retry_policy,
                )
            )
        return outcomes
