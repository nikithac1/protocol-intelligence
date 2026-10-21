"""Hybrid retrieval: vector + BM25 keyword search, metadata filters, reciprocal rank fusion.

Returns compact evidence records with everything needed for a citation (plan section 5.2).
"""

from __future__ import annotations

import re

from protocol_intelligence.retrieval.embeddings import embed_one
from protocol_intelligence.retrieval.index import open_table

RRF_K = 60
CANDIDATES = 40
_SAFE = re.compile(r"^[A-Za-z0-9_\- ]+$")


def _sql_list(values: list[str]) -> str:
    for v in values:
        if not _SAFE.match(v):  # filters come from the model; never interpolate anything odd
            raise ValueError(f"Invalid filter value: {v!r}")
    return ", ".join(f"'{v}'" for v in values)


def build_filter(nct_ids=None, section_types=None, source_types=None, therapeutic_areas=None) -> str | None:
    parts = []
    if nct_ids:
        parts.append(f"nct_id IN ({_sql_list([n.upper() for n in nct_ids])})")
    if section_types:
        parts.append(f"section_type IN ({_sql_list(section_types)})")
    if source_types:
        parts.append(f"source_type IN ({_sql_list(source_types)})")
    if therapeutic_areas:
        parts.append(f"therapeutic_area IN ({_sql_list(therapeutic_areas)})")
    return " AND ".join(parts) or None


def _fts_query(query: str) -> str:
    # Plain words only: strips characters the FTS parser treats as operators.
    return " ".join(re.findall(r"[A-Za-z0-9]+", query))


def search(query: str, k: int = 8, nct_ids: list[str] | None = None,
           section_types: list[str] | None = None, source_types: list[str] | None = None,
           therapeutic_areas: list[str] | None = None, mode: str = "hybrid") -> list[dict]:
    table = open_table()
    where = build_filter(nct_ids, section_types, source_types, therapeutic_areas)
    cols = ["chunk_id", "nct_id", "source_type", "document_title", "section_path", "section_type",
            "page_start", "page_end", "source_url", "text", "extraction_confidence"]
    ranked: dict[str, dict] = {}

    def add(results: list[dict], label: str) -> None:
        for rank, r in enumerate(results, 1):
            entry = ranked.setdefault(r["chunk_id"], {**{c: r[c] for c in cols}, "score": 0.0,
                                                      "matched_by": []})
            entry["score"] += 1.0 / (RRF_K + rank)
            entry["matched_by"].append(f"{label}#{rank}")

    if mode in ("hybrid", "vector"):
        q = table.search(embed_one(query)).select(cols).limit(CANDIDATES)
        if where:
            q = q.where(where, prefilter=True)
        add(q.to_list(), "vector")
    fts = _fts_query(query)
    if mode in ("hybrid", "keyword") and fts:
        q = table.search(fts, query_type="fts").select(cols).limit(CANDIDATES)
        if where:
            q = q.where(where, prefilter=True)
        add(q.to_list(), "keyword")

    hits, seen = [], set()
    for h in sorted(ranked.values(), key=lambda e: e["score"], reverse=True):
        # Same passage from another protocol version or an overlapping chunk adds no evidence.
        fingerprint = (h["nct_id"], " ".join(h["text"].split())[:200])
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        h["score"] = round(h["score"], 5)
        hits.append(h)
        if len(hits) == k:
            break
    return hits
