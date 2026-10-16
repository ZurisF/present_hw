from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ROOT_PATH = Path(__file__).resolve().parent.parent

MENU_SEED = [
    ("小笼包", "点心", 28.0, "皮薄馅鲜，搭配姜丝与香醋。", ["鲜香", "咸鲜"], 1, 1, "含猪肉、含小麦"),
    ("清蒸鲈鱼", "热菜", 88.0, "鲜嫩鲈鱼清蒸，葱姜提鲜。", ["清淡", "咸鲜"], 1, 1, "含鱼类"),
    ("本帮红烧肉", "热菜", 68.0, "浓油赤酱，软糯入味。", ["咸甜", "浓郁"], 1, 1, "含猪肉"),
    ("油爆虾", "热菜", 78.0, "上海本帮做法，外脆里嫩。", ["鲜香", "微甜"], 1, 1, "含虾类"),
    ("葱油拌面", "主食", 22.0, "葱香浓郁，口味清爽。", ["清淡", "咸鲜"], 0, 1, "含小麦"),
    ("荠菜馄饨", "主食", 32.0, "荠菜猪肉馅，清汤鲜美。", ["清淡", "鲜香"], 0, 1, "含猪肉、含小麦"),
    ("上海熏鱼", "凉菜", 48.0, "先炸后浸，咸甜适口。", ["咸甜", "浓郁"], 0, 1, "含鱼类"),
    ("麻婆豆腐", "热菜", 38.0, "豆腐细嫩，麻辣鲜香。", ["麻辣", "香辣"], 0, 1, "含大豆"),
    ("桂花酒酿圆子", "甜品", 26.0, "酒酿清甜，带淡淡桂花香。", ["甜口", "清淡"], 0, 1, "含糯米"),
    ("蟹粉豆腐", "热菜", 58.0, "蟹粉鲜香，豆腐细嫩。", ["鲜香", "咸鲜"], 0, 0, "含蟹类、含大豆"),
]

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS menu_items (
    dish_name TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    price REAL NOT NULL CHECK (price >= 0),
    description TEXT NOT NULL,
    flavors_json TEXT NOT NULL,
    is_featured INTEGER NOT NULL DEFAULT 0 CHECK (is_featured IN (0, 1)),
    is_available INTEGER NOT NULL DEFAULT 1 CHECK (is_available IN (0, 1)),
    allergens TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS agent_operations (
    request_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    operation_type TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    reservation_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_agent_operations_thread_status
    ON agent_operations(thread_id, status);
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    num_people INTEGER NOT NULL CHECK (num_people > 0),
    num_children INTEGER NOT NULL DEFAULT 0 CHECK (num_children >= 0),
    arrival_time TEXT NOT NULL,
    seat_preference TEXT NOT NULL,
    main_dish_preference TEXT NOT NULL,
    other_comments TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'CONFIRMED',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (request_id) REFERENCES agent_operations(request_id)
);
CREATE INDEX IF NOT EXISTS ix_reservations_phone_created
    ON reservations(phone, created_at);
CREATE TABLE IF NOT EXISTS agent_operation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    step TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (request_id) REFERENCES agent_operations(request_id)
);
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def database_path() -> Path:
    configured = os.getenv("DATABASE_PATH", "").strip()
    path = Path(configured).expanduser() if configured else ROOT_PATH / "data" / "restaurant.sqlite3"
    if not path.is_absolute():
        path = ROOT_PATH / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(database_path(), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    with connect() as connection:
        connection.executescript(SCHEMA)
        count = connection.execute("SELECT COUNT(*) AS count FROM menu_items").fetchone()["count"]
        if count == 0:
            connection.executemany(
                """INSERT INTO menu_items
                   (dish_name, category, price, description, flavors_json,
                    is_featured, is_available, allergens)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (name, category, price, description, json.dumps(flavors, ensure_ascii=False), featured, available, allergens)
                    for name, category, price, description, flavors, featured, available, allergens in MENU_SEED
                ],
            )

