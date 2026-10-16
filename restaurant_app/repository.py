from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from restaurant_app.database import connect

FLAVOR_ALIASES = {
    "清淡": ("清淡", "鲜香", "咸鲜"),
    "鲜香": ("鲜香", "咸鲜"),
    "麻辣": ("麻辣", "香辣", "微辣"),
    "香辣": ("香辣", "麻辣", "微辣"),
    "甜口": ("甜口", "微甜", "咸甜"),
    "咸甜": ("咸甜", "微甜", "浓郁"),
    "浓郁": ("浓郁", "咸甜"),
}


def _menu_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "dish_name": row["dish_name"],
        "category": row["category"],
        "price": row["price"],
        "description": row["description"],
        "flavors": json.loads(row["flavors_json"]),
        "is_featured": bool(row["is_featured"]),
        "is_available": bool(row["is_available"]),
        "allergens": row["allergens"],
    }


def list_menu(category: str | None = None, flavor: str | None = None, keyword: str | None = None) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if keyword:
        clauses.append("(dish_name LIKE ? OR description LIKE ? OR category LIKE ?)")
        value = f"%{keyword.strip()}%"
        params.extend((value, value, value))
    query = "SELECT * FROM menu_items"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY is_featured DESC, category, dish_name"
    with connect() as connection:
        rows = connection.execute(query, params).fetchall()
    items = [_menu_item(row) for row in rows]
    if flavor:
        aliases = FLAVOR_ALIASES.get(flavor, (flavor,))
        items = [item for item in items if any(tag in item["flavors"] for tag in aliases)]
    return items


def featured_dishes() -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM menu_items WHERE is_featured = 1 ORDER BY is_available DESC, dish_name"
        ).fetchall()
    return [_menu_item(row) for row in rows]


def create_reservation(details: dict[str, Any], request_id: str | None, thread_id: str) -> dict[str, Any]:
    stable_request_id = request_id or str(uuid4())
    reservation_id = f"LHR-{datetime.now(ZoneInfo('Asia/Shanghai')):%Y%m%d}-{uuid4().hex[:10].upper()}"
    normalized = {
        "customer_name": str(details["customer_name"]).strip(),
        "phone": str(details["phone"]).strip(),
        "num_people": int(details["num_people"]),
        "num_children": int(details["num_children"]),
        "arrival_time": str(details["arrival_time"]).strip(),
        "seat_preference": str(details["seat_preference"]).strip(),
        "main_dish_preference": str(details["main_dish_preference"]).strip(),
        "other_comments": str(details.get("other_comments", "")).strip(),
    }
    payload_json = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

    with connect() as connection:
        inserted = connection.execute(
            """INSERT OR IGNORE INTO agent_operations
               (request_id, thread_id, operation_type, payload_sha256, payload_json, status)
               VALUES (?, ?, 'RESERVATION_CREATE', ?, ?, 'PENDING')""",
            (stable_request_id, thread_id, payload_hash, payload_json),
        )
        if inserted.rowcount == 1:
            connection.execute(
                "INSERT INTO agent_operation_events (request_id, step, attempt, state) VALUES (?, 'OPERATION_PREPARED', 0, 'PENDING')",
                (stable_request_id,),
            )
        operation = connection.execute(
            "SELECT * FROM agent_operations WHERE request_id = ?", (stable_request_id,)
        ).fetchone()
        if operation["payload_sha256"] != payload_hash:
            raise IdempotencyConflict("该请求编号已用于不同的预订内容")
        if operation["status"] == "SUCCEEDED":
            existing = connection.execute(
                "SELECT * FROM reservations WHERE request_id = ?", (stable_request_id,)
            ).fetchone()
            if existing:
                return dict(existing)

    # The PENDING record is durable before the reservation transaction begins.
    # Startup recovery can safely resume it with the same request_id after a crash.
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        operation = connection.execute(
            "SELECT * FROM agent_operations WHERE request_id = ?", (stable_request_id,)
        ).fetchone()
        if operation["status"] == "SUCCEEDED":
            existing = connection.execute(
                "SELECT * FROM reservations WHERE request_id = ?", (stable_request_id,)
            ).fetchone()
            if existing:
                return dict(existing)
        existing = connection.execute(
            "SELECT * FROM reservations WHERE request_id = ?", (stable_request_id,)
        ).fetchone()
        if existing:
            connection.execute(
                "UPDATE agent_operations SET status = 'SUCCEEDED', reservation_id = ?, error_code = NULL, updated_at = CURRENT_TIMESTAMP WHERE request_id = ?",
                (existing["reservation_id"], stable_request_id),
            )
            connection.execute(
                "INSERT INTO agent_operation_events (request_id, step, attempt, state) VALUES (?, 'STATUS_CHECKED', 0, 'SUCCEEDED')",
                (stable_request_id,),
            )
            return dict(existing)
        connection.execute(
            """INSERT INTO reservations
               (reservation_id, request_id, thread_id, customer_name, phone,
                num_people, num_children, arrival_time, seat_preference,
                main_dish_preference, other_comments, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CONFIRMED')""",
            (
                reservation_id,
                stable_request_id,
                thread_id,
                normalized["customer_name"],
                normalized["phone"],
                normalized["num_people"],
                normalized["num_children"],
                normalized["arrival_time"],
                normalized["seat_preference"],
                normalized["main_dish_preference"],
                normalized["other_comments"],
            ),
        )
        connection.execute(
            "UPDATE agent_operations SET status = 'SUCCEEDED', reservation_id = ?, error_code = NULL, updated_at = CURRENT_TIMESTAMP WHERE request_id = ?",
            (reservation_id, stable_request_id),
        )
        connection.execute(
            "INSERT INTO agent_operation_events (request_id, step, attempt, state) VALUES (?, 'TOOL_SUCCEEDED', 1, 'SUCCEEDED')",
            (stable_request_id,),
        )
        row = connection.execute(
            "SELECT * FROM reservations WHERE reservation_id = ?", (reservation_id,)
        ).fetchone()
        return dict(row)


def recover_pending_reservations() -> list[dict[str, Any]]:
    with connect() as connection:
        operations = connection.execute(
            "SELECT request_id, thread_id, payload_json FROM agent_operations WHERE status = 'PENDING' ORDER BY created_at"
        ).fetchall()
    recovered: list[dict[str, Any]] = []
    for operation in operations:
        details = json.loads(operation["payload_json"])
        recovered.append(create_reservation(details, operation["request_id"], operation["thread_id"]))
    return recovered


class IdempotencyConflict(ValueError):
    pass


def reservation_by_id(reservation_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM reservations WHERE reservation_id = ?", (reservation_id.strip().upper(),)
        ).fetchone()
    return dict(row) if row else None


def list_reservations(
    thread_id: str | None = None,
    phone: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if thread_id:
        clauses.append("thread_id = ?")
        params.append(thread_id)
    if phone:
        clauses.append("phone = ?")
        params.append(phone.strip())
    query = "SELECT * FROM reservations"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 100)))
    with connect() as connection:
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def load_chat_state(session_id: str) -> dict[str, Any]:
    with connect() as connection:
        row = connection.execute(
            "SELECT state_json FROM chat_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    return json.loads(row["state_json"]) if row else {}


def save_chat_state(session_id: str, state: dict[str, Any]) -> None:
    payload = json.dumps(state, ensure_ascii=False)
    with connect() as connection:
        connection.execute(
            """INSERT INTO chat_sessions(session_id, state_json, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(session_id) DO UPDATE SET
               state_json = excluded.state_json, updated_at = CURRENT_TIMESTAMP""",
            (session_id, payload),
        )

