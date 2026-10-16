from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


class TravelMode(str, Enum):
    RIDING = "1"
    DRIVING = "2"
    STRAIGHT = "3"


class DeliveryRequest(BaseModel):
    address: str = Field(min_length=2, max_length=240, description="配送目标地址")
    travel_mode: TravelMode = Field(default=TravelMode.DRIVING, description="1 骑行，2 驾车，3 直线距离")

    @field_validator("address")
    @classmethod
    def normalize_address(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("请输入完整地址")
        return value


class ReservationCreate(BaseModel):
    customer_name: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=5, max_length=32)
    num_people: int = Field(ge=1, le=30)
    num_children: int = Field(ge=0, le=30, default=0)
    arrival_time: datetime
    seat_preference: str = Field(min_length=1, max_length=128)
    main_dish_preference: str = Field(min_length=1, max_length=512)
    other_comments: str = Field(default="", max_length=1000)
    confirmed: bool = Field(default=False, description="用户显式确认后设为 true")
    request_id: str | None = Field(default=None, max_length=80)
    thread_id: str | None = Field(default=None, max_length=128)

    @field_validator("customer_name", "phone", "seat_preference", "main_dish_preference")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("此字段不能为空")
        return value

    @field_validator("phone")
    @classmethod
    def validate_phone_characters(cls, value: str) -> str:
        if not re.fullmatch(r"[+\d\-\s()]{5,32}", value):
            raise ValueError("联系电话格式不正确")
        return value

    @model_validator(mode="after")
    def children_cannot_exceed_party(self) -> ReservationCreate:
        if self.num_children > self.num_people:
            raise ValueError("儿童人数不能大于就餐总人数")
        return self


class ChatRequest(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)

    @field_validator("session_id", "message")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("此字段不能为空")
        return value


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    intent: str
    reservation: dict[str, Any] | None = None
    reservations: list[dict[str, Any]] | None = None
    delivery: dict[str, Any] | None = None

