"""LanceDB chunk index: build from chunks.jsonl, open locally or straight from S3."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import lancedb
import pyarrow as pa
from lancedb.index import FTS

from protocol_intelligence.config import DATA_DIR, get_settings
from protocol_intelligence.retrieval.embeddings import DIMENSIONS, embed_many

TABLE = "chunks"
LOCAL_URI = DATA_DIR / "index" / "lancedb"

SCHEMA = pa.schema([
    ("chunk_id", pa.string()), ("nct_id", pa.string()), ("source_type", pa.string()),
    ("document_title", pa.string()), ("document_version", pa.string()),
    ("section_path", pa.string()),  # " > "-joined for display and keyword matching
    ("section_type", pa.string()), ("page_start", pa.int32()), ("page_end", pa.int32()),
    ("source_url", pa.string()), ("source_sha256", pa.string()), ("text", pa.string()),
    ("extraction_confidence", pa.string()), ("therapeutic_area", pa.string()),
    ("vector", pa.list_(pa.float32(), DIMENSIONS)),
])


def embedding_text(c: dict) -> str:
    """Prefix the heading path so a chunk's vector reflects where it sits in the protocol."""
    return f"{c['nct_id']} | {' > '.join(c['section_path'])}\n{c['text']}"


def build(chunks_path: Path, areas: dict[str, str], uri: Path = LOCAL_URI, progress=None) -> int:
    chunks = [json.loads(l) for l in chunks_path.read_text(encoding="utf-8").splitlines()]
    vectors = embed_many([embedding_text(c) for c in chunks], progress=progress)
    rows = [{
        "chunk_id": c["chunk_id"], "nct_id": c["nct_id"], "source_type": c["source_type"],
        "document_title": c["document_title"], "document_version": c["document_version"],
        "section_path": " > ".join(c["section_path"]), "section_type": c["section_type"],
        "page_start": c["page_start"], "page_end": c["page_end"], "source_url": c["source_url"],
        "source_sha256": c["source_sha256"], "text": c["text"],
        "extraction_confidence": c["extraction_confidence"],
        "therapeutic_area": areas.get(c["nct_id"], ""), "vector": v,
    } for c, v in zip(chunks, vectors)]
    db = lancedb.connect(str(uri))
    table = db.create_table(TABLE, data=pa.Table.from_pylist(rows, schema=SCHEMA), mode="overwrite")
    table.create_index("text", config=FTS(with_position=True), replace=True)  # BM25 keyword index
    for col in ("nct_id", "section_type", "source_type", "therapeutic_area"):
        table.create_scalar_index(col, replace=True)  # fast metadata filters
    return len(rows)


@lru_cache
def open_table():
    """Prefer local copy; fall back to s3://<bucket>/index/lancedb (e.g. inside AgentCore)."""
    if LOCAL_URI.exists():
        return lancedb.connect(str(LOCAL_URI)).open_table(TABLE)
    bucket = get_settings().s3_bucket
    return lancedb.connect(f"s3://{bucket}/index/lancedb",
                           storage_options={"region": get_settings().aws_region}).open_table(TABLE)
