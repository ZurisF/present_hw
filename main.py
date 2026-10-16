from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT_PATH = Path(__file__).resolve().parent
load_dotenv(ROOT_PATH / ".env")

from agent.langchain_assistant import RestaurantAssistant  # noqa: E402
from restaurant_app import repository  # noqa: E402
from restaurant_app.database import close_database, initialize_database  # noqa: E402
from restaurant_app.delivery import (  # noqa: E402
    MapsServiceUnavailable,
    check_delivery,
    get_maps_agent_tools,
    get_maps_provider,
)
from restaurant_app.faq import RedisFAQStore  # noqa: E402
from restaurant_app.mock_agent import CANCEL_PATTERN, MockRestaurantAgent  # noqa: E402
from restaurant_app.repository import IdempotencyConflict  # noqa: E402
from restaurant_app.schemas import ChatRequest, ChatResponse, DeliveryRequest, ReservationCreate  # noqa: E402
from restaurant_app.vector_search import MenuVectorIndex  # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("restaurant_app")
FRONTEND_DIST = ROOT_PATH / "frontend" / "dist"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
booking_flow = MockRestaurantAgent()
assistant: RestaurantAssistant | None = None
faq_store: RedisFAQStore | None = None
menu_vector_index: MenuVectorIndex | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global assistant, faq_store, menu_vector_index
    try:
        initialize_database()
        faq_store = RedisFAQStore()
        faq_store.initialize()
        menu_vector_index = MenuVectorIndex.from_environment()
        menu_vector_index.initialize(repository.list_menu())
        booking_flow.menu_index = menu_vector_index
        try:
            recovered = repository.recover_pending_reservations()
            if recovered:
                logger.info("recovered %s pending reservation(s)", len(recovered))
        except Exception:
            logger.exception("pending reservation recovery failed")

        try:
            maps_tools = await get_maps_agent_tools()
        except MapsServiceUnavailable:
            logger.exception("AMap MCP tool discovery failed during startup; delivery requests will retry")
            maps_tools = []
        assistant = RestaurantAssistant(menu_vector_index, faq_store, maps_tools)
        logger.info(
            "course stack initialized",
            extra={
                "agent_provider": os.getenv("AGENT_PROVIDER", "mock"),
                "vector_db": "milvus",
                "embedding_model": os.getenv("EMBEDDING_MODEL_PATH", "models/bge-base-zh"),
                "maps_provider": type(get_maps_provider()).__name__,
            },
        )
        yield
    finally:
        assistant = None
        if menu_vector_index is not None:
            menu_vector_index.close()
            menu_vector_index = None
        if faq_store is not None:
            faq_store.close()
            faq_store = None
        close_database()


app = FastAPI(
    title="六合餐厅点餐与配送服务",
    description="LangChain Agent、MySQL 菜单与预订、Redis FAQ、BGE + Milvus 检索和高德 MCP 配送查询。",
    version="2.0.0",
    docs_url="/doc",
    lifespan=lifespan,
)
if (FRONTEND_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="frontend-assets")


@app.get("/", include_in_schema=False)
def home():
    if (FRONTEND_DIST / "index.html").is_file():
        return FileResponse(FRONTEND_DIST / "index.html")
    return JSONResponse(
        {
            "service": "六合餐厅点餐与配送服务",
            "frontend": "请在 frontend 目录运行 npm run dev（默认 http://localhost:3000）",
            "api_docs": "/doc",
        }
    )


def _require_assistant() -> RestaurantAssistant:
    if assistant is None:
        raise HTTPException(status_code=503, detail="LangChain Agent 尚未就绪")
    return assistant


def _rank_menu(query: str, items: list[dict]) -> list[dict]:
    if menu_vector_index is None:
        raise HTTPException(status_code=503, detail="Milvus 菜单检索尚未就绪")
    if not items:
        return []
    hits = menu_vector_index.search(query, limit=max(len(repository.list_menu()), len(items)))
    ranks = {hit["dish_name"]: position for position, hit in enumerate(hits)}
    return sorted(items, key=lambda item: ranks.get(item["dish_name"], len(ranks)))


@app.get("/api/health", tags=["系统"])
def health() -> dict[str, str]:
    configured_provider = os.getenv("MAPS_PROVIDER", "auto").strip().lower()
    has_amap_url = bool(os.getenv("AMAP_MCP_URL", "").strip())
    if configured_provider == "demo":
        maps_provider = "demo"
    elif configured_provider == "amap":
        maps_provider = "amap" if has_amap_url else "amap-unconfigured"
    else:
        maps_provider = "amap" if has_amap_url else "demo"
    return {
        "status": "ok",
        "agent_backend": f"langchain-{os.getenv('AGENT_PROVIDER', 'mock').strip().lower()}",
        "maps_provider": maps_provider,
        "database": "mysql",
        "faq_store": "redis" if faq_store else "unavailable",
        "vector_db": "milvus" if menu_vector_index else "unavailable",
        "milvus_deployment": menu_vector_index.deployment if menu_vector_index else "unavailable",
        "embedding_backend": "huggingface-bge-base-zh" if menu_vector_index else "unavailable",
    }


@app.get("/api/menu", tags=["菜单"])
def menu(
    category: str | None = Query(default=None, max_length=40),
    flavor: str | None = Query(default=None, max_length=40),
    keyword: str | None = Query(default=None, max_length=80),
) -> dict:
    return {"success": True, "items": repository.list_menu(category=category, flavor=flavor, keyword=keyword)}


@app.get("/api/recommendations/featured", tags=["推荐"])
def featured_recommendations() -> dict:
    try:
        items = _rank_menu("特色菜 招牌菜", repository.featured_dishes())
    except Exception as exc:
        logger.exception("Milvus featured-dish search failed")
        raise HTTPException(status_code=503, detail="Milvus 菜单检索暂时不可用") from exc
    return {"success": True, "retrieval_backend": "milvus", "items": items}


@app.get("/api/recommendations/flavor", tags=["推荐"])
def flavor_recommendations(flavor: str = Query(min_length=1, max_length=40)) -> dict:
    normalized = flavor.strip()
    candidates = repository.list_menu(flavor=normalized)
    try:
        items = [item for item in _rank_menu(normalized, candidates) if item["is_available"]]
    except Exception as exc:
        logger.exception("Milvus flavor recommendation search failed")
        raise HTTPException(status_code=503, detail="Milvus 菜单检索暂时不可用") from exc
    if not items and normalized not in ("清淡", "鲜香", "麻辣", "香辣", "甜口", "咸甜", "浓郁"):
        raise HTTPException(status_code=422, detail={"message": "口味选项不支持", "available_flavors": ["清淡", "鲜香", "麻辣", "香辣", "甜口", "咸甜", "浓郁"]})
    return {"success": True, "flavor": normalized, "retrieval_backend": "milvus", "items": items}


@app.get("/api/search/menu", tags=["菜单"])
def semantic_menu_search(
    query: str = Query(min_length=1, max_length=160),
    limit: int = Query(default=5, ge=1, le=20),
) -> dict:
    if menu_vector_index is None:
        raise HTTPException(status_code=503, detail="Milvus 菜单检索尚未就绪")
    try:
        hits = menu_vector_index.search(query, limit=limit)
    except Exception as exc:
        logger.exception("Milvus semantic menu search failed")
        raise HTTPException(status_code=503, detail="Milvus 菜单检索暂时不可用") from exc
    menu_by_name = {item["dish_name"]: item for item in repository.list_menu()}
    items = [
        {**menu_by_name[hit["dish_name"]], "similarity": hit["score"]}
        for hit in hits
        if hit["dish_name"] in menu_by_name
    ]
    return {"success": True, "query": query, "retrieval_backend": "milvus", "items": items}


@app.post("/api/delivery", tags=["配送"])
async def delivery(request: DeliveryRequest) -> dict:
    try:
        return await check_delivery(request.address, request.travel_mode)
    except MapsServiceUnavailable as exc:
        logger.warning("delivery lookup failed because the maps provider is unavailable")
        return JSONResponse(
            status_code=503,
            content={"success": False, "message": "地图服务暂时不可用，请稍后重试", "error_code": "MAPS_SERVICE_UNAVAILABLE"},
        )


@app.post("/api/reservations", tags=["预订"])
def create_reservation(request: ReservationCreate) -> dict:
    if not request.confirmed:
        raise HTTPException(status_code=400, detail="请先核对预订信息并显式确认")
    arrival_time = request.arrival_time
    if arrival_time.tzinfo is None:
        arrival_time = arrival_time.replace(tzinfo=SHANGHAI_TZ)
    else:
        arrival_time = arrival_time.astimezone(SHANGHAI_TZ)
    if arrival_time <= datetime.now(timezone.utc).astimezone(SHANGHAI_TZ):
        raise HTTPException(status_code=422, detail="到店时间必须晚于当前上海时间")
    details = request.model_dump(exclude={"confirmed", "request_id", "thread_id"})
    details["arrival_time"] = arrival_time.isoformat(timespec="minutes")
    thread_id = request.thread_id or f"web:{request.phone}"
    try:
        record = repository.create_reservation(details, request.request_id, thread_id)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("reservation persistence failed")
        raise HTTPException(status_code=503, detail="预订暂时无法保存，请稍后重试") from exc
    return {"success": True, "reservation": record, "message": "预订已确认"}


@app.get("/api/reservations", tags=["预订"])
def reservations(
    phone: str | None = Query(default=None, min_length=5, max_length=32),
    thread_id: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    if not phone and not thread_id:
        raise HTTPException(status_code=422, detail="请提供手机号或会话编号查询预订")
    return {"success": True, "items": repository.list_reservations(thread_id=thread_id, phone=phone, limit=limit)}


@app.get("/api/reservations/{reservation_id}", tags=["预订"])
def reservation_detail(reservation_id: str) -> dict:
    record = repository.reservation_by_id(reservation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="没有找到这条预订记录")
    return {"success": True, "reservation": record}


def _is_booking_turn(session_id: str, message: str) -> bool:
    if repository.load_chat_state(session_id).get("pending_reservation"):
        return True
    is_lookup = bool(re.search(r"(?:查看|显示|查询|我的).{0,8}(?:预订|预约)|(?:预订|预约)(?:记录|状态)", message))
    is_cancel_only = bool(CANCEL_PATTERN.fullmatch(message.strip()))
    return (bool(re.search(r"预订|预约|订位|订座", message)) and not is_lookup) or is_cancel_only


async def _reply(session_id: str, message: str) -> dict:
    if _is_booking_turn(session_id, message):
        return await booking_flow.reply(session_id, message)
    return await _require_assistant().reply(session_id, message)


@app.post("/api/chat", response_model=ChatResponse, tags=["智能体"])
async def chat(request: ChatRequest) -> dict:
    try:
        return {"session_id": request.session_id, **(await _reply(request.session_id, request.message))}
    except Exception as exc:
        logger.exception("chat request failed")
        raise HTTPException(status_code=503, detail="Agent 或相关数据服务暂时不可用，请稍后重试") from exc


@app.post("/api/chat/stream", tags=["智能体"])
async def stream_chat(request: ChatRequest) -> StreamingResponse:
    async def events():
        try:
            if _is_booking_turn(request.session_id, request.message):
                result = await booking_flow.reply(request.session_id, request.message)
                yield f"data: {json.dumps({'token': result['answer']}, ensure_ascii=False)}\n\n"
            else:
                service = _require_assistant()
                async for token in service.stream(request.session_id, request.message):
                    yield f"data: {json.dumps({'token': token}, ensure_ascii=False)}\n\n"
            yield "event: done\ndata: {}\n\n"
        except Exception:
            logger.exception("streaming chat request failed")
            yield f"event: error\ndata: {json.dumps({'message': 'Agent 或相关数据服务暂时不可用，请稍后重试'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
