from __future__ import annotations

import json
import re
from typing import Any, Sequence
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

FLAVORS = ("清淡", "鲜香", "麻辣", "香辣", "甜口", "咸甜", "浓郁")
KNOWN_DISHES = (
    "小笼包", "清蒸鲈鱼", "本帮红烧肉", "油爆虾", "葱油拌面", "荠菜馄饨",
    "上海熏鱼", "麻婆豆腐", "桂花酒酿圆子", "蟹粉豆腐",
)


def _message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content)


def _parse_tool_result(content: Any) -> Any:
    if isinstance(content, (dict, list)):
        return content
    if not isinstance(content, str):
        return content
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content


def _menu_answer(payload: dict[str, Any]) -> str:
    items = payload.get("items") or []
    if not items:
        return "菜单检索没有找到符合条件的菜品，可以换个口味或描述再试试。"
    lines = []
    for item in items[:6]:
        state = "可供应" if item.get("is_available") else "暂时售罄"
        flavors = "、".join(item.get("flavors") or [])
        lines.append(f"{item.get('dish_name')} ¥{float(item.get('price', 0)):.0f} · {state} · {flavors}。{item.get('description', '')}")
    return "我从菜单里为你找到：\n" + "\n".join(lines)


def _format_tool_answer(name: str | None, payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return "工具已返回结果，但暂时无法整理成可展示的信息。"

    status = payload.get("status")
    if status in {"tool_error", "error", "failed"}:
        return "相关服务暂时不可用，我没有把故障当作查询无结果。请稍后重试。"
    if name == "search_menu":
        return _menu_answer(payload)
    if name == "check_menu_item":
        item = payload.get("item")
        if not item:
            return "菜单中没有找到这道菜。"
        available = "当前可供应" if item.get("is_available") else "暂时售罄"
        return f"{item['dish_name']}：¥{float(item['price']):.0f}，{available}。"
    if name == "faq_search":
        items = payload.get("items") or []
        if not items:
            return "我暂时没有找到这条餐厅信息，可以换个问法，或直接联系餐厅确认。"
        return items[0].get("answer", "我找到了相关信息，但答案暂不可用。")
    if name == "check_delivery":
        if not payload.get("success"):
            return payload.get("message", "配送地址暂时无法确认。")
        duration = f"，预计约 {payload['duration_min']} 分钟" if payload.get("duration_min") is not None else ""
        verdict = "在配送范围内" if payload.get("in_range") else "超出配送范围"
        return f"{payload['address']}按{payload['travel_mode']}计算约 {payload['distance_km']:.1f} 公里{duration}，{verdict}。"
    if name == "reservation_lookup":
        records = payload.get("items") or []
        if not records:
            return "暂时没有找到预订记录。"
        return "查询到以下预订：\n" + "\n".join(
            f"{item['reservation_id']}｜{item['arrival_time']}｜{item['num_people']} 人｜{item['status']}"
            for item in records
        )
    return str(payload.get("message") or payload)


class MockRestaurantChatModel(BaseChatModel):
    """Deterministic LangChain chat model that exercises the real tool graph."""

    @property
    def _llm_type(self) -> str:
        return "restaurant-deterministic-mock"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": "restaurant-mock"}

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Any = None, **kwargs: Any) -> BaseChatModel:
        # LangGraph still executes the tools returned in AIMessage.tool_calls.
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        last_tool_index = max(
            (index for index, message in enumerate(messages) if isinstance(message, ToolMessage)),
            default=-1,
        )
        last_human_index = max(
            (index for index, message in enumerate(messages) if isinstance(message, HumanMessage)),
            default=-1,
        )
        if last_tool_index > last_human_index:
            last_tool = messages[last_tool_index]
            answer = _format_tool_answer(last_tool.name, _parse_tool_result(last_tool.content))
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=answer))])

        user_message = next((message for message in reversed(messages) if isinstance(message, HumanMessage)), None)
        text = _message_text(user_message).strip() if user_message else ""
        tool_call = self._choose_tool(text)
        if tool_call is not None:
            name, arguments = tool_call
            message = AIMessage(
                content="",
                tool_calls=[{"name": name, "args": arguments, "id": uuid4().hex, "type": "tool_call"}],
            )
        else:
            answer = "你好，我是六合餐厅助手。可以帮你看菜单、按口味推荐、查询配送范围、查看预订或了解餐厅信息。"
            if re.search(r"配送|送到", text):
                answer = "请把完整收货地址发给我，例如：能送到上海市浦东新区陆家嘴吗？"
            if not text:
                answer = "请告诉我你想了解菜单、餐厅信息、配送范围或预订中的哪一项。"
            message = AIMessage(content=answer)
        return ChatResult(generations=[ChatGeneration(message=message)])

    @staticmethod
    def _choose_tool(text: str) -> tuple[str, dict[str, Any]] | None:
        if re.search(r"(?:能|可以|可不可以).*(?:送到|配送)|配送范围|送到.+吗", text):
            match = re.search(r"(?:送到|配送到|配送至|送至)\s*(.+?)(?:吗|么|[?？。！!]|$)", text)
            if not match:
                if re.search(r"配送范围|配送半径", text):
                    return "faq_search", {"query": text, "limit": 3}
                return None
            address = match.group(1).strip(" ，,。！？!? ")
            if not address.startswith("上海"):
                address = "上海市" + address
            mode = "1" if "骑" in text else "3" if "直线" in text else "2"
            return "check_delivery", {"address": address, "travel_mode": mode}

        reservation_lookup = re.search(r"(?:查看|显示|查询|我的).{0,8}(?:预订|预约)|(?:预订|预约)(?:记录|状态)", text)
        if reservation_lookup:
            phone = re.search(r"(?<!\d)1\d{10}(?!\d)", text)
            reservation_id = re.search(r"LHR-\d{8}-[A-F0-9]{10}", text, re.IGNORECASE)
            args = {"reservation_id": reservation_id.group(0) if reservation_id else ""}
            args["phone"] = phone.group(0) if phone else ""
            return "reservation_lookup", args

        dish = next((name for name in KNOWN_DISHES if name in text), None)
        if dish and re.search(r"(?:有|供应|卖|价格|多少钱|多少|售罄|能点)", text):
            return "check_menu_item", {"dish_name": dish}

        flavor = next((value for value in FLAVORS if value in text), None)
        if flavor or re.search(r"特色菜|招牌菜|推荐菜|菜单|有什么菜|菜品|吃什么|推荐|想吃", text):
            return "search_menu", {
                "query": text or "六合餐厅菜单",
                "flavor": flavor or "",
                "featured_only": bool(re.search(r"特色菜|招牌菜|推荐菜", text)),
                "limit": 6,
            }

        if re.search(r"地址|电话|营业|几点|营业时间|什么时候开|配送半径|配送范围|营业到", text):
            return "faq_search", {"query": text, "limit": 3}
        return None
