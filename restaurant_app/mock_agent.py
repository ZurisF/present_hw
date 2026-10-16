from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from restaurant_app import repository
from restaurant_app.delivery import MapsServiceUnavailable, check_delivery
from restaurant_app.schemas import TravelMode

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
FLAVORS = ("清淡", "鲜香", "麻辣", "香辣", "甜口", "咸甜", "浓郁")
SEAT_OPTIONS = ("靠窗", "大厅", "包间", "安静", "儿童座椅")
CONFIRM_PATTERN = re.compile(r"^(?:好的[，, ]*)?(?:我)?(?:确认|确定|没问题|就这样|提交预约)[。！! ]*$")
CANCEL_PATTERN = re.compile(r"^(?:取消|取消预订|取消预约|取消订位|先不订|不订了|不用预订|停止预约)[。！! ]*$")


class MockRestaurantAgent:
    """Deterministic, model-free dialogue flow for the classroom application."""

    def __init__(self) -> None:
        self.menu_index: Any | None = None

    def rank_menu(self, query: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rank SQL-filtered menu facts by Milvus similarity."""
        if not items or self.menu_index is None:
            return items
        hits = self.menu_index.search(query, limit=max(len(repository.list_menu()), len(items)))
        rank = {hit["dish_name"]: index for index, hit in enumerate(hits)}
        return sorted(items, key=lambda item: rank.get(item["dish_name"], len(rank)))

    async def reply(self, session_id: str, message: str) -> dict[str, Any]:
        text = message.strip()
        state = repository.load_chat_state(session_id)
        if re.search(r"(?:查看|显示|查询|我的).{0,8}(?:预订|预约)|(?:预订|预约)(?:记录|状态)", text):
            return self._show_reservation(session_id, text)
        if re.search(r"能(?:不能)?送到|可以送到|配送到|配送至|送到.+吗|配送范围", text):
            return await self._delivery(text)
        if CANCEL_PATTERN.fullmatch(text):
            return {"answer": "当前没有待确认的预订。", "intent": "reservation_cancelled"}
        pending = state.get("pending_reservation")
        if pending:
            response = self._continue_reservation(session_id, text, state, pending)
            repository.save_chat_state(session_id, state)
            return response

        if re.search(r"预订|预约|订位", text):
            pending = {"request_id": str(uuid4()), "details": {}, "awaiting_confirmation": False}
            self._merge_reservation_details(pending["details"], text)
            state["pending_reservation"] = pending
            response = self._reservation_prompt(session_id, pending)
            repository.save_chat_state(session_id, state)
            return response

        if self._is_flavor_question(text):
            flavor = next((flavor for flavor in FLAVORS if flavor in text), None)
            if flavor:
                candidates = repository.list_menu(flavor=flavor)
                items = [item for item in self.rank_menu(text, candidates) if item["is_available"]]
                return {"answer": _format_menu(items, f"{flavor}口味推荐"), "intent": "flavor_recommendation"}
            return {
                "answer": "你偏好哪种口味？我可以推荐清淡、鲜香、麻辣、香辣、甜口或浓郁风味的菜品。",
                "intent": "flavor_recommendation",
            }

        if re.search(r"特色菜|招牌菜|推荐菜", text):
            items = self.rank_menu(text, repository.featured_dishes())
            return {
                "answer": _format_menu(items, "六合餐厅特色菜"),
                "intent": "featured_recommendation",
            }

        if re.search(r"菜单|有什么菜|菜品|吃什么", text):
            return {"answer": _format_menu(repository.list_menu(), "六合餐厅菜单"), "intent": "menu"}

        dish = next((item for item in repository.list_menu() if item["dish_name"] in text), None)
        if dish:
            availability = "当前可供应" if dish["is_available"] else "暂时售罄"
            return {
                "answer": f"{dish['dish_name']}：¥{dish['price']:.0f}，{availability}。{dish['description']}",
                "intent": "menu_item_lookup",
            }

        if re.search(r"推荐|想吃|想来|来点|来一道|来道|适合我|不太辣|不辣|不油腻", text):
            items = self.rank_menu(text, repository.list_menu())
            items = [item for item in items if item["is_available"]][:4]
            return {
                "answer": _format_menu(items, "按你的描述从菜单中检索到"),
                "intent": "semantic_menu_recommendation",
            }

        return {
            "answer": "你好，我是六合餐厅助手。可以帮你看菜单、按口味推荐、推荐特色菜、查询配送范围或预订座位。",
            "intent": "help",
        }

    def _is_flavor_question(self, text: str) -> bool:
        return "口味" in text or any(flavor in text for flavor in FLAVORS)

    async def _delivery(self, text: str) -> dict[str, Any]:
        address = _extract_delivery_address(text)
        if not address:
            return {
                "answer": "请把完整收货地址发给我，例如：能送到上海市浦东新区陆家嘴吗？",
                "intent": "delivery",
            }
        if "骑" in text:
            mode = TravelMode.RIDING
        elif "直线" in text:
            mode = TravelMode.STRAIGHT
        else:
            mode = TravelMode.DRIVING
        try:
            result = await check_delivery(address, mode)
        except MapsServiceUnavailable:
            return {
                "answer": "地图服务暂时不可用，配送范围还没有查清。请稍后重试。",
                "intent": "delivery",
            }
        if not result["success"]:
            return {"answer": result["message"], "intent": "delivery"}
        duration = f"，预计约 {result['duration_min']} 分钟" if result["duration_min"] is not None else ""
        verdict = "可以配送" if result["in_range"] else "暂不在配送范围内"
        answer = f"{result['address']}按{result['travel_mode']}计算约 {result['distance_km']:.1f} 公里{duration}，{verdict}。"
        return {"answer": answer, "intent": "delivery", "delivery": result}

    def _show_reservation(self, session_id: str, text: str) -> dict[str, Any]:
        reservation_id = re.search(r"LHR-\d{8}-[A-F0-9]{10}", text, re.IGNORECASE)
        phone = _extract_phone(text)
        if reservation_id:
            item = repository.reservation_by_id(reservation_id.group(0))
            records = [item] if item else []
        else:
            records = repository.list_reservations(phone=phone, thread_id=None if phone else session_id)
        if not records:
            return {
                "answer": "暂时没有找到预订记录。你可以提供预订编号，或在本轮对话中完成预订后再来查询。",
                "intent": "reservation_lookup",
            }
        return {"answer": _format_reservations(records), "intent": "reservation_lookup", "reservations": records}

    def _continue_reservation(
        self,
        session_id: str,
        text: str,
        state: dict[str, Any],
        pending: dict[str, Any],
    ) -> dict[str, Any]:
        if CANCEL_PATTERN.fullmatch(text):
            state.pop("pending_reservation", None)
            return {"answer": "好的，已取消这次预订。你之后想订位时再告诉我。", "intent": "reservation_cancelled"}

        if pending.get("awaiting_confirmation") and CONFIRM_PATTERN.fullmatch(text):
            details = dict(pending["details"])
            try:
                record = repository.create_reservation(details, pending["request_id"], session_id)
            except Exception:
                return {
                    "answer": "预订暂时没有提交成功，系统会保留这次请求状态。请稍后重试，或联系餐厅核对。",
                    "intent": "reservation_error",
                }
            state.pop("pending_reservation", None)
            return {
                "answer": f"预订已确认，编号 {record['reservation_id']}。回复“显示预订 {record['reservation_id']}”可以查看详情。",
                "intent": "reservation_created",
                "reservation": record,
            }

        self._merge_reservation_details(pending["details"], text)
        pending["awaiting_confirmation"] = False
        response = self._reservation_prompt(session_id, pending)
        return response

    def _reservation_prompt(self, session_id: str, pending: dict[str, Any]) -> dict[str, Any]:
        details = pending["details"]
        missing = [field for field in _required_reservation_fields() if field not in details]
        if missing:
            prompts = {
                "customer_name": "预订人姓名",
                "phone": "联系电话",
                "num_people": "就餐总人数",
                "num_children": "儿童人数（没有请写 0）",
                "arrival_time": "到店日期和时间（上海时间，例如 2026-10-20 18:30）",
                "seat_preference": "座位偏好（例如靠窗、大厅或包间）",
                "main_dish_preference": "主菜偏好（没有请写不限）",
                "other_comments": "其他备注（没有请写无）",
            }
            labels = "、".join(prompts[field] for field in missing)
            return {
                "answer": f"我先帮你登记意向。还需要：{labels}。可以一条消息一起告诉我。",
                "intent": "reservation_collecting",
            }

        if not 1 <= int(details["num_people"]) <= 30:
            return {"answer": "就餐总人数需要在 1 到 30 人之间，请重新告诉我人数。", "intent": "reservation_collecting"}
        if int(details["num_children"]) > int(details["num_people"]):
            return {"answer": "儿童人数不能大于就餐总人数，请重新告诉我总人数或儿童人数。", "intent": "reservation_collecting"}
        if not re.fullmatch(r"[+\d\-\s()]{5,32}", str(details["phone"])):
            return {"answer": "联系电话格式不正确，请重新提供联系电话。", "intent": "reservation_collecting"}

        pending["awaiting_confirmation"] = True
        detail = details
        summary = (
            f"请核对预订信息：{detail['customer_name']}，电话 {detail['phone']}；"
            f"{detail['arrival_time']} 到店，{detail['num_people']} 人（儿童 {detail['num_children']} 人）；"
            f"座位：{detail['seat_preference']}；主菜偏好：{detail['main_dish_preference']}；"
            f"备注：{detail['other_comments'] or '无'}。确认无误请回复“确认”，也可以继续补充修改或回复“取消”。"
        )
        return {"answer": summary, "intent": "reservation_confirmation"}

    def _merge_reservation_details(self, details: dict[str, Any], text: str) -> None:
        name = re.search(
            r"(?:我叫|姓名[：:]?|名字[：:]?)\s*([A-Za-z\u4e00-\u9fff·'’\- ]{1,80}?)(?=\s*(?:电话|手机|联系电话|手机号|[，,、。；;\n]|$))",
            text,
        )
        if not name:
            name = re.search(r"^([\u4e00-\u9fff]{2,8})(?=$|[，,、\s])", text)
        if not name:
            name = re.search(r"^([A-Za-z][A-Za-z '\-]{0,78})(?=$|[，,、\s])", text)
        if name:
            details["customer_name"] = name.group(1).strip()
        phone = _extract_phone(text)
        if phone:
            details["phone"] = phone

        count = _extract_number(text, r"(?:就餐|用餐|一共|共)?\s*([\d一二两三四五六七八九十]+)\s*(?:位|人)")
        if count is not None:
            details["num_people"] = count
        children = _extract_number(text, r"(?:儿童|小孩|孩子)\s*([\d一二两三四五六七八九十]+)\s*(?:名|位|人)?")
        if children is None:
            children = _extract_number(text, r"([\d一二两三四五六七八九十]+)\s*(?:个|位|名)?(?:儿童|小孩|孩子)")
        if children is not None:
            details["num_children"] = children
        elif re.search(r"(?:没|没有|无|不带)(?:有)?(?:儿童|小孩|孩子)|(?:儿童|小孩|孩子)(?:没有|为零|0名|0人)", text):
            details["num_children"] = 0
        elif count is None:
            bare_count = _extract_number(text, r"\s*([\d一二两三四五六七八九十]+)\s*")
            if bare_count is not None:
                if "num_people" in details and "num_children" not in details:
                    details["num_children"] = bare_count
                elif bare_count == 0 and "num_children" not in details:
                    details["num_children"] = 0
                elif "num_people" not in details:
                    details["num_people"] = bare_count

        arrival = _parse_arrival_time(text)
        if arrival:
            details["arrival_time"] = arrival
        seat = next((option for option in SEAT_OPTIONS if option in text), None)
        if not seat and re.search(r"(?:座位|位置)(?:偏好)?[：:]?\s*(?:不限|都行|随便|无所谓|不挑)", text):
            seat = "不限"
        if seat:
            details["seat_preference"] = seat
        preference = re.search(r"(?:主菜偏好|菜品偏好|主菜)[：:]\s*([^，,。；;\n]+)", text)
        if re.search(r"(?:主菜偏好|主菜|菜品偏好)[：:]?\s*(?:不限|都行|随便|没有)|^(?:都行|随便|不限)$", text):
            details["main_dish_preference"] = "不限"
        elif preference:
            details["main_dish_preference"] = preference.group(1).strip()
        else:
            dish = next((item["dish_name"] for item in repository.list_menu() if item["dish_name"] in text), None)
            if dish:
                details["main_dish_preference"] = dish
        if re.fullmatch(r"\s*(?:无|没有|不需要)\s*", text) or re.search(r"(?:没有|无|不需要)(?:其他)?备注|备注[：:]?\s*(?:无|没有|不需要)", text):
            details["other_comments"] = ""
        else:
            comments = re.search(r"(?:备注|其他要求)[：:]?\s*(.+)$", text)
            if comments:
                details["other_comments"] = comments.group(1).strip()


def _required_reservation_fields() -> tuple[str, ...]:
    return (
        "customer_name",
        "phone",
        "num_people",
        "num_children",
        "arrival_time",
        "seat_preference",
        "main_dish_preference",
        "other_comments",
    )


def _extract_delivery_address(text: str) -> str | None:
    patterns = (
        r"(?:能不能|能|可以)?(?:配送|送)(?:到|至)\s*(.+?)(?:吗|么|[?？。！!]|$)",
        r"(?:配送范围|配送到|送到)\s*[:：]?\s*(.+?)(?:吗|么|[?？。！!]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            address = match.group(1).strip(" ，,。！？!? ")
            if address.startswith("上海市"):
                pass
            elif address.startswith("上海"):
                address = "上海市" + address[2:]
            else:
                address = "上海市" + address
            return address or None
    return None


def _extract_phone(text: str) -> str | None:
    labeled = re.search(r"(?:联系电话|手机号|手机|电话)[：:\s]*([+\d][+\d\-()]{3,30}\d)(?!\d)", text)
    if labeled:
        return labeled.group(1)
    mobile = re.search(r"(?<!\d)1\d{10}(?!\d)", text)
    return mobile.group(0) if mobile else None


def _extract_number(text: str, pattern: str) -> int | None:
    match = re.search(pattern, text)
    if not match:
        return None
    token = match.group(1)
    if token.isdigit():
        return int(token)
    numerals = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if token == "十":
        return 10
    if token.startswith("十"):
        return 10 + numerals.get(token[1:], 0)
    if token.endswith("十"):
        return numerals.get(token[0], 1) * 10
    if "十" in token:
        tens, ones = token.split("十", 1)
        return numerals.get(tens, 1) * 10 + numerals.get(ones, 0)
    return numerals.get(token)


def _parse_arrival_time(text: str) -> str | None:
    now = datetime.now(SHANGHAI_TZ).replace(tzinfo=None)
    date_value = now.date()
    explicit_date = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?", text)
    month_day = re.search(r"(\d{1,2})月(\d{1,2})日", text)
    if explicit_date:
        try:
            date_value = datetime(int(explicit_date.group(1)), int(explicit_date.group(2)), int(explicit_date.group(3))).date()
        except ValueError:
            return None
    elif month_day:
        year = now.year
        try:
            date_value = datetime(year, int(month_day.group(1)), int(month_day.group(2))).date()
            if date_value < now.date():
                date_value = datetime(year + 1, int(month_day.group(1)), int(month_day.group(2))).date()
        except ValueError:
            return None
    elif "后天" in text:
        date_value += timedelta(days=2)
    elif "明天" in text or "明晚" in text:
        date_value += timedelta(days=1)

    period = re.search(r"明晚|今晚|早上|上午|中午|下午|晚上", text)
    clock = re.search(r"(?<!\d)(\d{1,2})\s*[:：点时](?:(\d{1,2})\s*(?:分)?|(半))?", text)
    if not clock:
        chinese_clock = re.search(r"([一二两三四五六七八九十]+)\s*点(?:半|(?:([一二两三四五六七八九十\d]+)\s*分))?", text)
        if chinese_clock:
            hour = _chinese_hour(chinese_clock.group(1))
            minute = 30 if "半" in chinese_clock.group(0) else (_chinese_hour(chinese_clock.group(2)) if chinese_clock.group(2) else 0)
        else:
            return None
    else:
        hour = int(clock.group(1))
        minute = 30 if clock.group(3) else int(clock.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    period_text = period.group(0) if period else ""
    if period_text in ("下午", "晚上", "今晚", "明晚") and 1 <= hour <= 11:
        hour += 12
    elif period_text == "中午" and 1 <= hour <= 10:
        hour += 12
    try:
        result = datetime.combine(date_value, datetime.min.time()).replace(hour=hour, minute=minute)
    except ValueError:
        return None
    if result <= now:
        return None
    return result.isoformat(timespec="minutes")


def _chinese_hour(value: str | None) -> int:
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    numerals = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if value == "十":
        return 10
    if value.startswith("十"):
        return 10 + numerals.get(value[1:], 0)
    if "十" in value:
        tens, ones = value.split("十", 1)
        return numerals.get(tens, 1) * 10 + numerals.get(ones, 0)
    return numerals.get(value, 0)


def _format_menu(items: list[dict[str, Any]], title: str) -> str:
    if not items:
        return f"{title}暂时没有匹配的菜品。"
    lines = [f"{title}："]
    for item in items:
        availability = "可供应" if item["is_available"] else "暂时售罄"
        flavors = "、".join(item["flavors"])
        lines.append(f"• {item['dish_name']} ¥{item['price']:.0f}｜{availability}｜{flavors}。{item['description']}")
    return "\n".join(lines)


def _format_reservations(items: list[dict[str, Any]]) -> str:
    lines = ["查询到以下预订记录："]
    for item in items:
        lines.append(
            f"• {item['reservation_id']}｜{item['arrival_time']}｜{item['num_people']} 人，儿童 {item['num_children']} 人｜"
            f"{item['seat_preference']}｜主菜偏好：{item['main_dish_preference']}｜状态：{item['status']}"
        )
    return "\n".join(lines)

