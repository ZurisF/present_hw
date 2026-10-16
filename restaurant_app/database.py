from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.engine import URL, Engine

ROOT_PATH = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_PATH / ".env")

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

metadata = MetaData()
menu_items = Table(
    "menu_items",
    metadata,
    Column("dish_name", String(100), primary_key=True),
    Column("category", String(50), nullable=False),
    Column("price", Numeric(8, 2), nullable=False),
    Column("description", Text, nullable=False),
    Column("flavors_json", Text, nullable=False),
    Column("is_featured", Boolean, nullable=False, server_default="0"),
    Column("is_available", Boolean, nullable=False, server_default="1"),
    Column("allergens", String(500), nullable=False, server_default=""),
    Column("created_at", DateTime, nullable=False, server_default=func.current_timestamp()),
    Column("updated_at", DateTime, nullable=False, server_default=func.current_timestamp()),
    Index("ix_menu_category_available", "category", "is_available"),
    Index("ix_menu_featured_available", "is_featured", "is_available"),
)

agent_operations = Table(
    "agent_operations",
    metadata,
    Column("request_id", String(80), primary_key=True),
    Column("thread_id", String(128), nullable=False),
    Column("operation_type", String(48), nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("status", String(20), nullable=False, server_default="PENDING"),
    Column("reservation_id", String(40)),
    Column("error_code", String(80)),
    Column("created_at", DateTime, nullable=False, server_default=func.current_timestamp()),
    Column("updated_at", DateTime, nullable=False, server_default=func.current_timestamp()),
    Index("ix_agent_operations_thread_status", "thread_id", "status"),
)

reservations = Table(
    "reservations",
    metadata,
    Column("reservation_id", String(40), primary_key=True),
    Column("request_id", String(80), ForeignKey("agent_operations.request_id"), nullable=False, unique=True),
    Column("thread_id", String(128), nullable=False),
    Column("customer_name", String(80), nullable=False),
    Column("phone", String(32), nullable=False),
    Column("num_people", Integer, nullable=False),
    Column("num_children", Integer, nullable=False, server_default="0"),
    Column("arrival_time", DateTime, nullable=False),
    Column("seat_preference", String(128), nullable=False),
    Column("main_dish_preference", String(512), nullable=False),
    Column("other_comments", Text, nullable=False),
    Column("status", String(20), nullable=False, server_default="CONFIRMED"),
    Column("created_at", DateTime, nullable=False, server_default=func.current_timestamp()),
    Index("ix_reservations_phone_created", "phone", "created_at"),
)

agent_operation_events = Table(
    "agent_operation_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("request_id", String(80), ForeignKey("agent_operations.request_id"), nullable=False),
    Column("step", String(48), nullable=False),
    Column("attempt", Integer, nullable=False, server_default="0"),
    Column("state", String(20), nullable=False),
    Column("error_code", String(80)),
    Column("created_at", DateTime, nullable=False, server_default=func.current_timestamp()),
    Index("ix_operation_events_request_time", "request_id", "created_at"),
)

chat_sessions = Table(
    "chat_sessions",
    metadata,
    Column("session_id", String(128), primary_key=True),
    Column("state_json", Text, nullable=False),
    Column("updated_at", DateTime, nullable=False, server_default=func.current_timestamp()),
)


def database_url() -> URL | str:
    configured = os.getenv("MYSQL_URL", "").strip()
    if configured:
        return configured
    return URL.create(
        "mysql+pymysql",
        username=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        database=os.getenv("MYSQL_DATABASE", "menu"),
        query={"charset": os.getenv("MYSQL_CHARSET", "utf8mb4")},
    )


engine: Engine = create_engine(database_url(), pool_pre_ping=True, pool_recycle=1800)


def initialize_database() -> None:
    """Create the course MySQL schema and seed the sample menu when empty."""
    metadata.create_all(engine)
    with engine.begin() as connection:
        count = connection.execute(select(func.count()).select_from(menu_items)).scalar_one()
        if count == 0:
            connection.execute(
                menu_items.insert(),
                [
                    {
                        "dish_name": name,
                        "category": category,
                        "price": price,
                        "description": description,
                        "flavors_json": json.dumps(flavors, ensure_ascii=False),
                        "is_featured": bool(featured),
                        "is_available": bool(available),
                        "allergens": allergens,
                    }
                    for name, category, price, description, flavors, featured, available, allergens in MENU_SEED
                ],
            )


def close_database() -> None:
    engine.dispose()

