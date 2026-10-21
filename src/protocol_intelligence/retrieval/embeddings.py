"""Bedrock Titan text embeddings with parallel calls and an on-disk cache."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from botocore.config import Config

from protocol_intelligence.config import DATA_DIR, aws_session, get_settings

DIMENSIONS = 1024
MAX_CHARS = 30_000  # Titan v2 accepts 8k tokens; chunks are far smaller, this is a guard
CACHE_PATH = DATA_DIR / "index" / "embedding_cache.parquet"


@lru_cache
def _runtime():
    cfg = Config(retries={"max_attempts": 10, "mode": "adaptive"}, max_pool_connections=16)
    return aws_session().client("bedrock-runtime", config=cfg)


def _cache_key(text: str) -> str:
    model = get_settings().bedrock_embedding_model_id
    return hashlib.sha256(f"{model}|{DIMENSIONS}|{text}".encode()).hexdigest()


def embed_one(text: str) -> list[float]:
    body = json.dumps({"inputText": text[:MAX_CHARS], "dimensions": DIMENSIONS, "normalize": True})
    resp = _runtime().invoke_model(modelId=get_settings().bedrock_embedding_model_id, body=body)
    return json.loads(resp["body"].read())["embedding"]


def _load_cache(path: Path) -> dict[str, list[float]]:
    if not path.exists():
        return {}
    t = pq.read_table(path)
    return dict(zip(t.column("key").to_pylist(), t.column("vector").to_pylist()))


def _save_cache(path: Path, cache: dict[str, list[float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"key": list(cache), "vector": list(cache.values())}), path)


def embed_many(texts: list[str], workers: int = 8, progress=None) -> list[list[float]]:
    """Embed texts, reusing cached vectors; returns vectors in input order."""
    cache = _load_cache(CACHE_PATH)
    keys = [_cache_key(t) for t in texts]
    todo = sorted({k: t for k, t in zip(keys, texts) if k not in cache}.items())
    if todo:
        with ThreadPoolExecutor(workers) as pool:
            for i, (key, vec) in enumerate(zip([k for k, _ in todo],
                                               pool.map(embed_one, [t for _, t in todo])), 1):
                cache[key] = vec
                if progress and i % 250 == 0:
                    progress(i, len(todo))
                if i % 1000 == 0:
                    _save_cache(CACHE_PATH, cache)  # checkpoint long runs
        _save_cache(CACHE_PATH, cache)
    return [cache[k] for k in keys]
