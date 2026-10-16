from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from restaurant_app.schemas import TravelMode

logger = logging.getLogger(__name__)

RESTAURANT_LOCATION = (
    float(os.getenv("RESTAURANT_LONGITUDE", "121.4737")),
    float(os.getenv("RESTAURANT_LATITUDE", "31.2304")),
)


class AddressUnresolvable(Exception):
    pass


class MapsServiceUnavailable(Exception):
    pass


@dataclass(frozen=True)
class RouteMetrics:
    distance_m: float
    duration_s: float | None


class DemoMapsProvider:
    """Credential-free local map fixture; set AMAP_MCP_URL for live AMap data."""

    def __init__(self) -> None:
        self.locations = {
            "人民广场": (121.4737, 31.2304),
            "六合大厦": (121.4737, 31.2304),
            "陆家嘴": (121.5080, 31.2397),
            "外滩": (121.4903, 31.2400),
            "南京西路": (121.4593, 31.2290),
            "徐家汇": (121.4360, 31.1950),
            "静安寺": (121.4450, 31.2230),
        }

    async def route(self, address: str, mode: TravelMode) -> RouteMetrics:
        normalized = re.sub(r"\s+", "", address)
        destination = next((point for keyword, point in self.locations.items() if keyword in normalized), None)
        if destination is None:
            raise AddressUnresolvable
        direct_m = _haversine_m(RESTAURANT_LOCATION, destination)
        if mode is TravelMode.STRAIGHT:
            return RouteMetrics(distance_m=direct_m, duration_s=None)
        if mode is TravelMode.RIDING:
            distance_m = direct_m * 1.18
            speed_kmh = 15
        else:
            distance_m = direct_m * 1.34
            speed_kmh = 25
        return RouteMetrics(distance_m=distance_m, duration_s=distance_m / 1000 / speed_kmh * 3600)


class AmapMcpProvider:
    def __init__(self, url: str) -> None:
        self.url = url

    async def route(self, address: str, mode: TravelMode) -> RouteMetrics:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(
                self.url,
                timeout=timedelta(seconds=20),
                sse_read_timeout=timedelta(seconds=45),
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tool_list = await session.list_tools()
                    available = {tool.name: tool for tool in tool_list.tools}
                    # 六合大厦 is a course-project restaurant landmark. Keep its verified
                    # service origin configurable instead of depending on geocoding a fictional POI.
                    origin = f"{RESTAURANT_LOCATION[0]:.6f},{RESTAURANT_LOCATION[1]:.6f}"
                    destination = await self._geocode(session, available, address)
                    metrics = await self._route(session, available, origin, destination, mode)
                    return metrics
        except AddressUnresolvable:
            raise
        except MapsServiceUnavailable:
            raise
        except Exception as exc:
            logger.warning("AMap MCP request failed: %s", type(exc).__name__)
            raise MapsServiceUnavailable from exc

    async def _geocode(self, session: Any, available: dict[str, Any], address: str) -> str:
        if "maps_geo" not in available:
            raise MapsServiceUnavailable("AMap MCP server does not expose maps_geo")
        result = await session.call_tool("maps_geo", {"address": address, "city": "上海"})
        payload = _tool_payload(result)
        if getattr(result, "isError", False):
            raise MapsServiceUnavailable("AMap geocoding failed")
        location = _find_location(payload)
        if not location:
            raise AddressUnresolvable
        return location

    async def _route(
        self,
        session: Any,
        available: dict[str, Any],
        origin: str,
        destination: str,
        mode: TravelMode,
    ) -> RouteMetrics:
        if mode is TravelMode.STRAIGHT:
            tool_name = "maps_distance"
            if tool_name not in available:
                raise MapsServiceUnavailable("AMap MCP server does not expose maps_distance")
            schema = _tool_schema(available[tool_name])
            properties = schema.get("properties", {})
            arguments: dict[str, Any] = {"origins": origin, "destination": destination}
            if "type" in properties:
                expected = properties["type"].get("type")
                arguments["type"] = 0 if expected in {"integer", "number"} else "0"
        else:
            tool_name = "maps_direction_bicycling" if mode is TravelMode.RIDING else "maps_direction_driving"
            if tool_name not in available:
                raise MapsServiceUnavailable(f"AMap MCP server does not expose {tool_name}")
            arguments = {"origin": origin, "destination": destination}

        result = await session.call_tool(tool_name, arguments)
        if getattr(result, "isError", False):
            raise MapsServiceUnavailable("AMap route calculation failed")
        payload = _tool_payload(result)
        distance = _find_number(payload, {"distance", "distance_m", "route_distance"})
        duration = _find_number(payload, {"duration", "duration_s"})
        if distance is None:
            raise MapsServiceUnavailable("AMap route response did not include a distance")
        return RouteMetrics(distance_m=distance, duration_s=duration)


def get_maps_provider() -> DemoMapsProvider | AmapMcpProvider:
    provider = os.getenv("MAPS_PROVIDER", "auto").strip().lower()
    url = os.getenv("AMAP_MCP_URL", "").strip()
    if provider == "demo":
        return DemoMapsProvider()
    if provider == "amap":
        if not url:
            raise MapsServiceUnavailable("AMAP_MCP_URL is required when MAPS_PROVIDER=amap")
        return AmapMcpProvider(url)
    if url:
        return AmapMcpProvider(url)
    return DemoMapsProvider()


async def check_delivery(address: str, mode: TravelMode) -> dict[str, Any]:
    try:
        route = await get_maps_provider().route(address, mode)
    except AddressUnresolvable:
        return {
            "success": False,
            "address": address,
            "distance_km": None,
            "duration_min": None,
            "in_range": False,
            "travel_mode": mode_label(mode),
            "message": "地址无法解析，请重新输入",
        }
    except MapsServiceUnavailable as exc:
        raise MapsServiceUnavailable from exc

    distance_km = round(route.distance_m / 1000, 1)
    duration_min = math.ceil(route.duration_s / 60) if route.duration_s is not None else None
    radius_km = float(os.getenv("DELIVERY_RADIUS_KM", "5"))
    in_range = route.distance_m <= radius_km * 1000
    return {
        "success": True,
        "address": address,
        "distance_km": distance_km,
        "duration_min": duration_min,
        "in_range": in_range,
        "travel_mode": mode_label(mode),
        "message": "在配送范围内" if in_range else f"超出配送范围（{radius_km:g} 公里）",
    }


def mode_label(mode: TravelMode) -> str:
    return {
        TravelMode.RIDING: "骑行",
        TravelMode.DRIVING: "驾车",
        TravelMode.STRAIGHT: "直线距离",
    }[mode]


def _tool_schema(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
    if hasattr(schema, "model_dump"):
        return schema.model_dump(by_alias=True)
    return schema if isinstance(schema, dict) else {}


def _tool_payload(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    parts = []
    for content in getattr(result, "content", []) or []:
        text = getattr(content, "text", None)
        if text:
            try:
                parts.append(json.loads(text))
            except (json.JSONDecodeError, TypeError):
                parts.append(text)
    return parts[0] if len(parts) == 1 else parts


def _find_location(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("location", "坐标", "经纬度"):
            candidate = value.get(key)
            if isinstance(candidate, str) and _valid_location(candidate):
                return candidate.strip()
        for nested in value.values():
            found = _find_location(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_location(nested)
            if found:
                return found
    elif isinstance(value, str):
        match = re.search(r"(?<!\d)(\d{2,3}\.\d+)\s*,\s*(\d{1,2}\.\d+)", value)
        if match:
            candidate = f"{match.group(1)},{match.group(2)}"
            if _valid_location(candidate):
                return candidate
    return None


def _valid_location(value: str) -> bool:
    match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", value)
    if not match:
        return False
    longitude, latitude = map(float, match.groups())
    return -180 <= longitude <= 180 and -90 <= latitude <= 90


def _find_number(value: Any, accepted_keys: set[str]) -> float | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in accepted_keys:
                try:
                    return float(item)
                except (TypeError, ValueError):
                    continue
        for item in value.values():
            number = _find_number(item, accepted_keys)
            if number is not None:
                return number
    elif isinstance(value, list):
        for item in value:
            number = _find_number(item, accepted_keys)
            if number is not None:
                return number
    elif isinstance(value, str):
        for key in accepted_keys:
            match = re.search(rf"{re.escape(key)}\s*[:：=]\s*([\d.]+)", value, re.IGNORECASE)
            if match:
                return float(match.group(1))
    return None


def _haversine_m(origin: tuple[float, float], destination: tuple[float, float]) -> float:
    longitude1, latitude1 = map(math.radians, origin)
    longitude2, latitude2 = map(math.radians, destination)
    delta_longitude = longitude2 - longitude1
    delta_latitude = latitude2 - latitude1
    a = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(latitude1) * math.cos(latitude2) * math.sin(delta_longitude / 2) ** 2
    )
    return 6_371_000 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

