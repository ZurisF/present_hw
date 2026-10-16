from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT_PATH = Path(__file__).resolve().parent
load_dotenv(ROOT_PATH / ".env")

from restaurant_app import repository  # noqa: E402
from restaurant_app.database import initialize_database  # noqa: E402
from restaurant_app.delivery import MapsServiceUnavailable, check_delivery  # noqa: E402
from restaurant_app.mock_agent import MockRestaurantAgent  # noqa: E402
from restaurant_app.repository import IdempotencyConflict  # noqa: E402
from restaurant_app.schemas import ChatRequest, ChatResponse, DeliveryRequest, ReservationCreate  # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("restaurant_app")
STATIC_PATH = ROOT_PATH / "static"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
agent = MockRestaurantAgent()


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    try:
        recovered = repository.recover_pending_reservations()
        if recovered:
            logger.info("recovered %s pending reservation(s)", len(recovered))
    except Exception:
        logger.exception("pending reservation recovery failed")
    yield


app = FastAPI(
    title="六合餐厅点餐与配送服务",
    description="菜单展示、口味与特色菜推荐、预订、预订查询和高德地图配送范围查询。",
    version="1.0.0",
    docs_url="/doc",
    redoc_url="/redoc",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_PATH), name="static")


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(STATIC_PATH / "index.html")


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
    return {"status": "ok", "agent_backend": "mock", "maps_provider": maps_provider}


@app.get("/api/menu", tags=["菜单"])
def menu(
    category: str | None = Query(default=None, max_length=40),
    flavor: str | None = Query(default=None, max_length=40),
    keyword: str | None = Query(default=None, max_length=80),
) -> dict:
    return {"success": True, "items": repository.list_menu(category=category, flavor=flavor, keyword=keyword)}


@app.get("/api/recommendations/featured", tags=["推荐"])
def featured_recommendations() -> dict:
    return {"success": True, "items": repository.featured_dishes()}


@app.get("/api/recommendations/flavor", tags=["推荐"])
def flavor_recommendations(flavor: str = Query(min_length=1, max_length=40)) -> dict:
    normalized = flavor.strip()
    items = [item for item in repository.list_menu(flavor=normalized) if item["is_available"]]
    if not items:
        known = ("清淡", "鲜香", "麻辣", "香辣", "甜口", "咸甜", "浓郁")
        if normalized not in known:
            raise HTTPException(status_code=422, detail={"message": "口味选项不支持", "available_flavors": known})
    return {"success": True, "flavor": normalized, "items": items}


@app.post("/api/delivery", tags=["配送"])
async def delivery(request: DeliveryRequest) -> dict:
    """查询目标地址是否在六合餐厅 5 公里配送半径内。"""
    try:
        return await check_delivery(request.address, request.travel_mode)
    except MapsServiceUnavailable:
        logger.warning("delivery lookup failed because the maps provider is unavailable")
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "message": "地图服务暂时不可用，请稍后重试",
                "error_code": "MAPS_SERVICE_UNAVAILABLE",
            },
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


@app.post("/api/chat", response_model=ChatResponse, tags=["智能体"])
async def chat(request: ChatRequest) -> dict:
    return {"session_id": request.session_id, **(await agent.reply(request.session_id, request.message))}

