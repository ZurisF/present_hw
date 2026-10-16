from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from typing import Any

FAQ_SEED = {
    "address": {
        "question": "餐厅地址在哪里？",
        "aliases": "餐厅在哪|你们在哪里|六合餐厅怎么走|餐厅位置|地址是什么",
        "answer": "六合餐厅位于上海黄浦区人民广场六合大厦。",
    },
    "phone": {
        "question": "餐厅电话是多少？",
        "aliases": "联系电话|怎么联系餐厅|大堂电话|电话号码是多少",
        "answer": "大堂电话：021-12345678。",
    },
    "hours": {
        "question": "餐厅什么时候营业？",
        "aliases": "几点开门|几点关门|几点营业|营业到几点|什么时候停止营业",
        "answer": "周日至周四 10:00–21:00；周五、周六 10:00–23:00。",
    },
    "delivery": {
        "question": "餐厅配送范围有多远？",
        "aliases": "可以送多远|最多送几公里|外卖配送半径|能送到多远",
        "answer": "默认配送半径为 5 公里，具体距离可按骑行、驾车或直线距离查询。",
    },
}


def _normalize(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower(), flags=re.UNICODE)


def _jaccard(left: str, right: str) -> float:
    left_chars, right_chars = set(left), set(right)
    union = left_chars | right_chars
    return len(left_chars & right_chars) / len(union) if union else 0.0


def _similarity(left: str, right: str) -> float:
    a, b = _normalize(left), _normalize(right)
    if not a or not b:
        return 0.0
    sequence = SequenceMatcher(None, a, b).ratio()
    return 0.65 * sequence + 0.35 * _jaccard(a, b)


class RedisFAQStore:
    """Redis-backed FAQ examples with course-style fuzzy matching."""

    def __init__(self, url: str | None = None, prefix: str | None = None) -> None:
        self.url = url or os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        self.prefix = prefix or os.getenv("REDIS_FAQ_PREFIX", "restaurant:faq:")
        self.client: Any | None = None

    def initialize(self) -> None:
        try:
            from redis import Redis
        except ImportError as exc:
            raise RuntimeError("Install the declared Redis client dependency before starting the app.") from exc
        self.client = Redis.from_url(self.url, decode_responses=True, socket_connect_timeout=5)
        self.client.ping()
        for faq_id, record in FAQ_SEED.items():
            key = f"{self.prefix}{faq_id}"
            if not self.client.exists(key):
                self.client.hset(key, mapping=record)

    def search(self, query: str, limit: int = 3, min_score: float = 0.28) -> dict[str, Any]:
        if self.client is None:
            raise RuntimeError("Redis FAQ store has not been initialized")
        normalized_query = query.strip()
        matches = []
        for key in self.client.scan_iter(match=f"{self.prefix}*", count=100):
            record = self.client.hgetall(key)
            question = record.get("question", "")
            examples = [question, *record.get("aliases", "").split("|")]
            score = max((_similarity(normalized_query, example) for example in examples if example), default=0.0)
            if score >= min_score:
                matches.append(
                    {
                        "question": question,
                        "answer": record.get("answer", ""),
                        "score": round(score, 4),
                    }
                )
        matches.sort(key=lambda item: item["score"], reverse=True)
        if not matches:
            return {"status": "not_found", "items": []}
        return {"status": "matched", "items": matches[: max(1, min(limit, 10))]}

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
