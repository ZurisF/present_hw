from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert

from restaurant_app.database import (
    agent_operation_events,
    agent_operations,
    chat_sessions,
    engine,
    menu_items,
    reservations,
)

FLAVOR_ALIASES = {
    "清淡": ("清淡", "鲜香", "咸鲜"),
    "鲜香": ("鲜香", "咸鲜"),
    "麻辣": ("麻辣", "香辣", "微辣"),
    "香辣": ("香辣", "麻辣", "微辣"),
    "甜口": ("甜口", "微甜", "咸甜"),
    "咸甜": ("咸甜", "微甜", "浓郁"),
    "浓郁": ("浓郁", "咸甜"),
}
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _menu_item(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["price"] = float(item["price"])
    item["flavors"] = json.loads(item.pop("flavors_json"))
    item["is_featured"] = bool(item["is_featured"])
    item["is_available"] = bool(item["is_available"])
    return item


def _reservation_item(row: Any) -> dict[str, Any]:
    item = dict(row)
    for field in ("arrival_time", "created_at"):
        value = item.get(field)
        if isinstance(value, datetime):
            item[field] = value.isoformat(timespec="minutes" if field == "arrival_time" else "seconds")
    return item


def list_menu(
    category: str | None = None,
    flavor: str | None = None,
    keyword: str | None = None,
) -> list[dict[str, Any]]:
    conditions = []
    if category:
        conditions.append(menu_items.c.category == category)
    if keyword and keyword.strip():
        pattern = f"%{keyword.strip()}%"
        conditions.append(
            or_(
                menu_items.c.dish_name.like(pattern),
                menu_items.c.description.like(pattern),
                menu_items.c.category.like(pattern),
            )
        )
    statement = select(menu_items)
    if conditions:
        statement = statement.where(and_(*conditions))
    statement = statement.order_by(menu_items.c.is_featured.desc(), menu_items.c.category, menu_items.c.dish_name)
    with engine.connect() as connection:
        items = [_menu_item(row) for row in connection.execute(statement).mappings().all()]
    if flavor:
        aliases = FLAVOR_ALIASES.get(flavor, (flavor,))
        items = [item for item in items if any(tag in item["flavors"] for tag in aliases)]
    return items


def featured_dishes() -> list[dict[str, Any]]:
    statement = (
        select(menu_items)
        .where(menu_items.c.is_featured.is_(True))
        .order_by(menu_items.c.is_available.desc(), menu_items.c.dish_name)
    )
    with engine.connect() as connection:
        return [_menu_item(row) for row in connection.execute(statement).mappings().all()]


def _normalize_reservation(details: dict[str, Any]) -> tuple[dict[str, Any], datetime]:
    arrival_value = details["arrival_time"]
    arrival = arrival_value if isinstance(arrival_value, datetime) else datetime.fromisoformat(str(arrival_value).replace("Z", "+00:00"))
    if arrival.tzinfo is not None:
        arrival = arrival.astimezone(SHANGHAI_TZ).replace(tzinfo=None)
    normalized = {
        "customer_name": str(details["customer_name"]).strip(),
        "phone": str(details["phone"]).strip(),
        "num_people": int(details["num_people"]),
        "num_children": int(details.get("num_children", 0)),
        "arrival_time": arrival.isoformat(timespec="minutes"),
        "seat_preference": str(details["seat_preference"]).strip(),
        "main_dish_preference": str(details["main_dish_preference"]).strip(),
        "other_comments": str(details.get("other_comments", "")).strip(),
    }
    return normalized, arrival


def create_reservation(
    details: dict[str, Any],
    request_id: str | None,
    thread_id: str,
) -> dict[str, Any]:
    stable_request_id = request_id or str(uuid4())
    normalized, arrival = _normalize_reservation(details)
    payload_json = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    reservation_id = f"LHR-{datetime.now(SHANGHAI_TZ):%Y%m%d}-{uuid4().hex[:10].upper()}"

    with engine.begin() as connection:
        inserted = connection.execute(
            insert(agent_operations)
            .prefix_with("IGNORE")
            .values(
                request_id=stable_request_id,
                thread_id=thread_id,
                operation_type="RESERVATION_CREATE",
                payload_sha256=payload_hash,
                payload_json=payload_json,
                status="PENDING",
            )
        )
        if inserted.rowcount:
            connection.execute(
                insert(agent_operation_events).values(
                    request_id=stable_request_id,
                    step="OPERATION_PREPARED",
                    attempt=0,
                    state="PENDING",
                )
            )
        operation = connection.execute(
            select(agent_operations)
            .where(agent_operations.c.request_id == stable_request_id)
            .with_for_update()
        ).mappings().one()
        if operation["payload_sha256"] != payload_hash or operation["thread_id"] != thread_id:
            raise IdempotencyConflict("该请求编号已用于不同的预订内容")
        if operation["status"] == "SUCCEEDED":
            existing = connection.execute(
                select(reservations).where(reservations.c.request_id == stable_request_id)
            ).mappings().first()
            if existing:
                return _reservation_item(existing)
            raise RuntimeError("预订操作已标记成功，但记录不存在")

    # Commit PENDING first. A process restart can replay the same request_id safely.
    with engine.begin() as connection:
        operation = connection.execute(
            select(agent_operations)
            .where(agent_operations.c.request_id == stable_request_id)
            .with_for_update()
        ).mappings().one()
        if operation["payload_sha256"] != payload_hash or operation["thread_id"] != thread_id:
            raise IdempotencyConflict("该请求编号已用于不同的预订内容")
        existing = connection.execute(
            select(reservations).where(reservations.c.request_id == stable_request_id)
        ).mappings().first()
        if existing:
            connection.execute(
                update(agent_operations)
                .where(agent_operations.c.request_id == stable_request_id)
                .values(status="SUCCEEDED", reservation_id=existing["reservation_id"], error_code=None, updated_at=func.current_timestamp())
            )
            connection.execute(
                insert(agent_operation_events).values(
                    request_id=stable_request_id,
                    step="STATUS_CHECKED",
                    attempt=0,
                    state="SUCCEEDED",
                )
            )
            return _reservation_item(existing)

        connection.execute(
            insert(reservations).values(
                reservation_id=reservation_id,
                request_id=stable_request_id,
                thread_id=thread_id,
                customer_name=normalized["customer_name"],
                phone=normalized["phone"],
                num_people=normalized["num_people"],
                num_children=normalized["num_children"],
                arrival_time=arrival,
                seat_preference=normalized["seat_preference"],
                main_dish_preference=normalized["main_dish_preference"],
                other_comments=normalized["other_comments"],
                status="CONFIRMED",
            )
        )
        connection.execute(
            update(agent_operations)
            .where(agent_operations.c.request_id == stable_request_id)
            .values(status="SUCCEEDED", reservation_id=reservation_id, error_code=None, updated_at=func.current_timestamp())
        )
        connection.execute(
            insert(agent_operation_events).values(
                request_id=stable_request_id,
                step="TOOL_SUCCEEDED",
                attempt=1,
                state="SUCCEEDED",
            )
        )
        row = connection.execute(
            select(reservations).where(reservations.c.reservation_id == reservation_id)
        ).mappings().one()
        return _reservation_item(row)


def recover_pending_reservations() -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(agent_operations.c.request_id, agent_operations.c.thread_id, agent_operations.c.payload_json)
            .where(agent_operations.c.status == "PENDING")
            .order_by(agent_operations.c.created_at)
        ).mappings().all()
    recovered = []
    for operation in rows:
        details = json.loads(operation["payload_json"])
        recovered.append(create_reservation(details, operation["request_id"], operation["thread_id"]))
    return recovered


class IdempotencyConflict(ValueError):
    pass


def reservation_by_id(reservation_id: str) -> dict[str, Any] | None:
    with engine.connect() as connection:
        row = connection.execute(
            select(reservations).where(reservations.c.reservation_id == reservation_id.strip().upper())
        ).mappings().first()
    return _reservation_item(row) if row else None


def reservation_by_request_id(request_id: str) -> dict[str, Any] | None:
    with engine.connect() as connection:
        row = connection.execute(
            select(reservations).where(reservations.c.request_id == request_id)
        ).mappings().first()
    return _reservation_item(row) if row else None


def list_reservations(
    thread_id: str | None = None,
    phone: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    conditions = []
    if thread_id:
        conditions.append(reservations.c.thread_id == thread_id)
    if phone:
        conditions.append(reservations.c.phone == phone.strip())
    if not conditions:
        return []
    statement = (
        select(reservations)
        .where(and_(*conditions))
        .order_by(reservations.c.created_at.desc())
        .limit(max(1, min(limit, 100)))
    )
    with engine.connect() as connection:
        return [_reservation_item(row) for row in connection.execute(statement).mappings().all()]


def load_chat_state(session_id: str) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            select(chat_sessions.c.state_json).where(chat_sessions.c.session_id == session_id)
        ).first()
    return json.loads(row.state_json) if row else {}


def save_chat_state(session_id: str, state: dict[str, Any]) -> None:
    payload = json.dumps(state, ensure_ascii=False)
    statement = mysql_insert(chat_sessions).values(session_id=session_id, state_json=payload)
    statement = statement.on_duplicate_key_update(
        state_json=payload,
        updated_at=func.current_timestamp(),
    )
    with engine.begin() as connection:
        connection.execute(statement)
