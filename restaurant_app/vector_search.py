from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT_PATH = Path(__file__).resolve().parent.parent
EMBEDDING_DIMENSION = 768


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
    return " ".join(str(part) for part in parts if part)


class MenuVectorIndex:
    """BGE-base-zh embeddings with a remote Milvus collection."""

    def __init__(self, client: Any, embeddings: Any, collection_name: str, uri: str):
        self.client = client
        self.embeddings = embeddings
        self.collection_name = collection_name
        self.uri = uri

    @classmethod
    def from_environment(cls) -> "MenuVectorIndex":
        try:
            from langchain_huggingface import HuggingFaceEmbeddings
            from pymilvus import MilvusClient
        except ImportError as exc:
            raise RuntimeError(
                "Install the declared LangChain Hugging Face and PyMilvus dependencies before starting the app."
            ) from exc

        uri = os.getenv("MILVUS_URI", "").strip()
        if not uri.startswith(("http://", "https://")):
            raise RuntimeError("Set MILVUS_URI to the remote Milvus endpoint; local Milvus Lite is not configured.")

        configured_model = os.getenv("EMBEDDING_MODEL_PATH", "models/bge-base-zh").strip()
        model_path = Path(configured_model).expanduser()
        if not model_path.is_absolute():
            model_path = ROOT_PATH / model_path
        if not model_path.is_dir():
            raise RuntimeError(
                f"BGE model files were not found at {model_path}. Place the course model there; the app will not download it."
            )

        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        embeddings = HuggingFaceEmbeddings(
            model_name=str(model_path),
            model_kwargs={
                "device": os.getenv("EMBEDDING_DEVICE", "cpu"),
                "trust_remote_code": True,
            },
            encode_kwargs={
                "normalize_embeddings": True,
                "batch_size": int(os.getenv("EMBEDDING_BATCH_SIZE", "32")),
            },
        )
        dimension = len(embeddings.embed_query("六合餐厅向量维度检查"))
        if dimension != EMBEDDING_DIMENSION:
            raise RuntimeError(
                f"Expected a {EMBEDDING_DIMENSION}-dimensional BGE-base-zh embedding, received {dimension}."
            )

        token = os.getenv("MILVUS_TOKEN", "").strip()
        client = MilvusClient(uri=uri, token=token) if token else MilvusClient(uri=uri)
        collection_name = os.getenv("MILVUS_COLLECTION", "menu_items").strip() or "menu_items"
        return cls(client=client, embeddings=embeddings, collection_name=collection_name, uri=uri)

    @property
    def deployment(self) -> str:
        return "remote"

    def initialize(self, items: list[dict[str, Any]]) -> None:
        from pymilvus import DataType, MilvusClient

        if not self.client.has_collection(self.collection_name):
            schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field(
                field_name="dish_name",
                datatype=DataType.VARCHAR,
                is_primary=True,
                max_length=100,
            )
            schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=EMBEDDING_DIMENSION)
            schema.add_field(field_name="text", datatype=DataType.VARCHAR, max_length=4000)

            index_params = MilvusClient.prepare_index_params()
            index_params.add_index(
                field_name="vector",
                index_type="HNSW",
                metric_type="COSINE",
            )
            self.client.create_collection(
                collection_name=self.collection_name,
                schema=schema,
                index_params=index_params,
            )

        if not items:
            return
        documents = [_menu_document(item) for item in items]
        vectors = self.embeddings.embed_documents(documents)
        self.client.upsert(
            collection_name=self.collection_name,
            data=[
                {
                    "dish_name": item["dish_name"],
                    "vector": vector,
                    "text": document[:4000],
                }
                for item, vector, document in zip(items, vectors, documents, strict=True)
            ],
        )

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        normalized = query.strip()
        if not normalized:
            return []
        groups = self.client.search(
            collection_name=self.collection_name,
            data=[self.embeddings.embed_query(normalized)],
            anns_field="vector",
            limit=max(1, min(limit, 50)),
            output_fields=["dish_name", "text"],
            search_params={"metric_type": "COSINE", "params": {"ef": 64}},
        )
        if not groups:
            return []
        hits = []
        for hit in groups[0]:
            entity = hit.get("entity") or {}
            dish_name = entity.get("dish_name") or hit.get("id")
            if dish_name:
                hits.append(
                    {
                        "dish_name": str(dish_name),
                        "text": entity.get("text", ""),
                        "score": float(hit.get("distance", 0.0)),
                    }
                )
        return hits

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()
