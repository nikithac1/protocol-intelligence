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


def _rows(chunks: list[dict], areas: dict[str, str], progress=None) -> list[dict]:
    vectors = embed_many([embedding_text(c) for c in chunks], progress=progress)
    return [{
        "chunk_id": c["chunk_id"], "nct_id": c["nct_id"], "source_type": c["source_type"],
        "document_title": c["document_title"], "document_version": c["document_version"],
        "section_path": " > ".join(c["section_path"]), "section_type": c["section_type"],
        "page_start": c["page_start"], "page_end": c["page_end"], "source_url": c["source_url"],
        "source_sha256": c["source_sha256"], "text": c["text"],
        "extraction_confidence": c["extraction_confidence"],
        "therapeutic_area": areas.get(c["nct_id"], ""), "vector": v,
    } for c, v in zip(chunks, vectors)]


def read_chunks(*paths: Path) -> list[dict]:
    return [json.loads(l) for p in paths if p.exists()
            for l in p.read_text(encoding="utf-8").splitlines()]


def build(chunks_paths: list[Path], areas: dict[str, str], uri: Path = LOCAL_URI,
          progress=None) -> int:
    rows = _rows(read_chunks(*chunks_paths), areas, progress)
    db = lancedb.connect(str(uri))
    table = db.create_table(TABLE, data=pa.Table.from_pylist(rows, schema=SCHEMA), mode="overwrite")
    table.create_index("text", config=FTS(with_position=True), replace=True)  # BM25 keyword index
    for col in ("nct_id", "section_type", "source_type", "therapeutic_area"):
        table.create_scalar_index(col, replace=True)  # fast metadata filters
    open_table.cache_clear()
    return len(rows)


def _local_table():
    if not LOCAL_URI.exists():
        raise RuntimeError(f"No local index at {LOCAL_URI}; run scripts/build_index.py "
                           "or scripts/sync_s3.py down --prefix index first")
    return open_table()  # the agent's cached handle, so searches see the change immediately


def add_chunks(chunks: list[dict], areas: dict[str, str]) -> int:
    """Embed and append chunks, replacing any rows with the same chunk_id."""
    table = _local_table()
    rows = _rows(chunks, areas)
    ids = [c["chunk_id"] for c in chunks]
    for i in range(0, len(ids), 500):
        table.delete("chunk_id IN ({})".format(", ".join(_quote(x) for x in ids[i:i + 500])))
    table.add(pa.Table.from_pylist(rows, schema=SCHEMA))
    table.optimize()  # fold the new rows into the FTS and scalar indexes
    return len(rows)


def find(column: str, value: str) -> dict | None:
    rows = (open_table().search().where(f"{column} = {_quote(value)}")
            .select(["nct_id", "document_title"]).limit(1).to_list())
    return rows[0] if rows else None


def delete_where(**equals: str) -> int:
    """Delete rows matching every column = value pair; returns the number removed."""
    table = _local_table()
    before = table.count_rows()
    table.delete(" AND ".join(f"{col} = {_quote(v)}" for col, v in equals.items()))
    table.optimize()
    return before - table.count_rows()


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@lru_cache
def open_table():
    """Prefer local copy; fall back to s3://<bucket>/index/lancedb (e.g. inside AgentCore)."""
    if LOCAL_URI.exists():
        return lancedb.connect(str(LOCAL_URI)).open_table(TABLE)
    bucket = get_settings().s3_bucket
    return lancedb.connect(f"s3://{bucket}/index/lancedb",
                           storage_options={"region": get_settings().aws_region}).open_table(TABLE)
