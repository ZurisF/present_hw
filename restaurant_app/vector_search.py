from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from typing import Any

EMBEDDING_DIMENSION = 384
_TEXT_PATTERN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.IGNORECASE)

# A deterministic embedding substitute avoids requiring model credentials or
# downloads. Vector persistence and nearest-neighbor search still use Milvus.
_CONCEPTS = {
    "light": ("清淡", "清爽", "清口", "清蒸", "少油", "不油腻", "原汁原味"),
    "savory": ("鲜香", "咸鲜", "鲜美", "鲜嫩", "鲜味", "有滋味"),
    "spicy": ("麻辣", "香辣", "微辣", "辛辣", "辣味"),
    "sweet": ("甜口", "甜品", "甜味", "酒酿", "桂花"),
    "fish": ("鱼", "鲈鱼", "熏鱼", "鱼类", "鱼肉"),
    "seafood": ("海鲜", "虾", "蟹", "水产"),
    "pork": ("猪肉", "红烧肉", "馄饨"),
    "soy": ("豆腐", "大豆", "豆制品"),
}


def mock_embedding(text: str) -> list[float]:
    """Create stable dense vectors from Chinese character n-grams and concepts."""
    normalized = text.strip().lower()
    features: Counter[str] = Counter()
    for token in _TEXT_PATTERN.findall(normalized):
        if all("\u4e00" <= char <= "\u9fff" for char in token):
            for size in (1, 2, 3):
                for start in range(max(1, len(token) - size + 1)):
                    gram = token[start : start + size]
                    if len(gram) == size:
                        features[f"zh{size}:{gram}"] += 1.0 if size == 1 else 1.5
        else:
            features[f"word:{token}"] += 2.0

    for concept, aliases in _CONCEPTS.items():
        if any(alias in normalized for alias in aliases):
            features[f"concept:{concept}"] += 3.0

    vector = [0.0] * EMBEDDING_DIMENSION
    for feature, weight in features.items():
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSION
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign * weight

    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        vector = [value / norm for value in vector]
    return vector


def _menu_document(item: dict[str, Any]) -> str:
    parts = [
        item["dish_name"],
        item["category"],
        item["description"],
        *item.get("flavors", []),
        item.get("allergens", ""),
    ]
    if item.get("is_featured"):
        parts.append("特色菜 招牌菜 推荐菜")
    return " ".join(part for part in parts if part)


class MenuVectorIndex:
    """Milvus-backed menu retrieval with SQL kept as the source of menu facts."""

    def __init__(self, client: Any, collection_name: str, uri: str):
        self.client = client
        self.collection_name = collection_name
        self.uri = uri

    @classmethod
    def from_environment(cls) -> "MenuVectorIndex":
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:
            raise RuntimeError(
                "Milvus support is required. Install the project dependencies with `pip install -e .`."
            ) from exc

        uri = os.getenv("MILVUS_URI", "").strip()
        if not uri.startswith(("http://", "https://")):
            raise RuntimeError(
                "Set MILVUS_URI to the remote Milvus endpoint provided for this project."
            )

        token = os.getenv("MILVUS_TOKEN", "").strip()
        client = MilvusClient(uri=uri, token=token) if token else MilvusClient(uri=uri)
        collection_name = os.getenv("MILVUS_COLLECTION", "restaurant_menu").strip() or "restaurant_menu"
        return cls(client=client, collection_name=collection_name, uri=uri)

    @property
    def deployment(self) -> str:
        return "remote"

    def initialize(self, items: list[dict[str, Any]]) -> None:
        if not self.client.has_collection(self.collection_name):
            self.client.create_collection(
                collection_name=self.collection_name,
                dimension=EMBEDDING_DIMENSION,
                primary_field_name="dish_id",
                id_type="string",
                vector_field_name="vector",
                metric_type="COSINE",
                auto_id=False,
                max_length=512,
            )

        if items:
            self.client.upsert(
                collection_name=self.collection_name,
                data=[
                    {
                        "dish_id": item["dish_name"],
                        "vector": mock_embedding(_menu_document(item)),
                    }
                    for item in items
                ],
            )

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        normalized = query.strip()
        if not normalized:
            return []
        groups = self.client.search(
            collection_name=self.collection_name,
            data=[mock_embedding(normalized)],
            anns_field="vector",
            limit=max(1, min(limit, 50)),
            output_fields=[],
        )
        if not groups:
            return []
        return [
            {"dish_name": hit["id"], "score": float(hit.get("distance", 0.0))}
            for hit in groups[0]
        ]
