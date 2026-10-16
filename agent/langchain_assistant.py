from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime, tool

from restaurant_app import repository
from restaurant_app.delivery import check_delivery as query_delivery
from restaurant_app.faq import RedisFAQStore
from restaurant_app.schemas import TravelMode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssistantContext:
    session_id: str
    confirmed_reservation: dict[str, Any] | None = None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content)


def _intent_for(text: str) -> str:
    if re.search(r"配送|送到", text):
        return "delivery"
    if re.search(r"地址|电话|营业|几点", text):
        return "faq"
    if re.search(r"特色菜|招牌菜|推荐菜", text):
        return "featured_recommendation"
    if re.search(r"清淡|鲜香|麻辣|香辣|甜口|咸甜|浓郁|口味", text):
        return "flavor_recommendation"
    if re.search(r"菜单|菜品|吃什么|推荐|想吃", text):
        return "menu_search"
    if re.search(r"预订|预约|订位", text):
        return "reservation_lookup"
    return "assistant"


class RestaurantAssistant:
    """LangChain create_agent with the course stack and application-owned tools."""

    def __init__(self, menu_index: Any, faq_store: RedisFAQStore, mcp_tools: list[Any] | None = None) -> None:
        from langchain.agents import create_agent
        from langgraph.checkpoint.memory import InMemorySaver

        self.menu_index = menu_index
        self.faq_store = faq_store
        self._reservation_tool = self._build_reservation_tool()
        tools = [
            self._build_search_menu_tool(),
            self._build_check_menu_tool(),
            self._build_faq_tool(),
            self._build_delivery_tool(),
            self._build_reservation_lookup_tool(),
            self._reservation_tool,
            *(mcp_tools or []),
        ]
        model = self._build_model()
        prompt_path = Path(__file__).resolve().parent / "prompt" / "system_prompt.txt"
        system_prompt = prompt_path.read_text(encoding="utf-8")
        self.graph = create_agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            checkpointer=InMemorySaver(),
            context_schema=AssistantContext,
        )

    @staticmethod
    def _build_model() -> Any:
        provider = os.getenv("AGENT_PROVIDER", "mock").strip().lower()
        if provider == "mock":
            from agent.mock_chat_model import MockRestaurantChatModel

            return MockRestaurantChatModel()
        if provider == "openai":
            from langchain_openai import ChatOpenAI

            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY is required when AGENT_PROVIDER=openai")
            return ChatOpenAI(
                model=os.getenv("OPENAI_MODEL", "gpt-4o"),
                api_key=api_key,
                temperature=0,
                timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30")),
                max_retries=2,
            )
        raise RuntimeError("AGENT_PROVIDER must be 'mock' or 'openai'")

    def _build_search_menu_tool(self) -> Any:
        @tool
        def search_menu(query: str, flavor: str = "", featured_only: bool = False, limit: int = 6) -> dict[str, Any]:
            """通过 Milvus 语义检索餐厅菜单，并从 MySQL 核对价格和供应状态。"""
            candidates = repository.featured_dishes() if featured_only else repository.list_menu(flavor=flavor or None)
            hits = self.menu_index.search(query, limit=max(1, min(len(repository.list_menu()), 30)))
            rank = {hit["dish_name"]: position for position, hit in enumerate(hits)}
            ranked = sorted(candidates, key=lambda item: rank.get(item["dish_name"], len(rank)))
            return {"status": "ok" if ranked else "not_found", "items": ranked[: max(1, min(limit, 10))]}

        return search_menu

    @staticmethod
    def _build_check_menu_tool() -> Any:
        @tool
        def check_menu_item(dish_name: str) -> dict[str, Any]:
            """从 MySQL 查询一道菜是否存在、价格和当前供应状态。"""
            item = next((record for record in repository.list_menu() if record["dish_name"] == dish_name.strip()), None)
            return {"status": "not_found", "item": None} if item is None else {"status": "ok", "item": item}

        return check_menu_item

    def _build_faq_tool(self) -> Any:
        @tool
        def faq_search(query: str, limit: int = 3) -> dict[str, Any]:
            """在 Redis FAQ 中按字符串和 Jaccard 相似度检索餐厅常见问题。"""
            return self.faq_store.search(query, limit=limit)

        return faq_search

    @staticmethod
    def _build_delivery_tool() -> Any:
        @tool
        async def check_delivery(address: str, travel_mode: str = "2") -> dict[str, Any]:
            """使用高德 MCP 查询地址并计算配送距离；travel_mode 为 1 骑行、2 驾车、3 直线。"""
            try:
                mode = TravelMode(travel_mode)
            except ValueError:
                return {"success": False, "message": "出行方式无效，请选择骑行、驾车或直线距离。"}
            return await query_delivery(address, mode)

        return check_delivery

    @staticmethod
    def _build_reservation_lookup_tool() -> Any:
        @tool
        def reservation_lookup(
            runtime: ToolRuntime[AssistantContext],
            phone: str = "",
            reservation_id: str = "",
        ) -> dict[str, Any]:
            """按预订编号、手机号或当前会话查询已登记的预订。"""
            context = runtime.context
            if context is None:
                return {"status": "invalid_context", "items": []}
            if reservation_id.strip():
                record = repository.reservation_by_id(reservation_id.strip())
                records = [record] if record else []
            else:
                records = repository.list_reservations(
                    phone=phone.strip() or None,
                    thread_id=None if phone.strip() else context.session_id,
                    limit=20,
                )
            return {"status": "ok" if records else "not_found", "items": records}

        return reservation_lookup

    @staticmethod
    def _build_reservation_tool() -> Any:
        @tool
        def create_reservation(runtime: ToolRuntime[AssistantContext]) -> dict[str, Any]:
            """提交由应用二次确认后写入 context 的预订；模型不能自行提供确认状态。"""
            context = runtime.context
            details = context.confirmed_reservation if context is not None else None
            if not details:
                return {"status": "confirmation_required", "message": "请先复述预订资料并等待用户明确确认。"}
            try:
                record = repository.create_reservation(
                    details=details,
                    request_id=str(details["request_id"]),
                    thread_id=context.session_id,
                )
            except Exception:
                logger.exception("confirmed reservation tool failed")
                return {"status": "tool_error", "error_code": "RESERVATION_WRITE_FAILED"}
            return {"status": "SUCCEEDED", "reservation": record}

        return create_reservation

    async def reply(self, session_id: str, message: str) -> dict[str, Any]:
        result = await self.graph.ainvoke(
            {"messages": [{"role": "user", "content": message}]},
            config={"configurable": {"thread_id": session_id}},
            context=AssistantContext(session_id=session_id),
        )
        final = next(
            (
                item for item in reversed(result.get("messages", []))
                if getattr(item, "type", None) == "ai" and not getattr(item, "tool_calls", None)
            ),
            None,
        )
        if final is None:
            raise RuntimeError("LangChain agent completed without a final assistant message")
        return {"answer": _content_text(final.content), "intent": _intent_for(message)}

    async def stream(self, session_id: str, message: str):
        async for event in self.graph.astream(
            {"messages": [{"role": "user", "content": message}]},
            config={"configurable": {"thread_id": session_id}},
            context=AssistantContext(session_id=session_id),
            stream_mode="messages",
        ):
            if not isinstance(event, tuple) or not event:
                continue
            chunk = event[0]
            content = getattr(chunk, "content", "")
            text = _content_text(content)
            if text:
                yield text
