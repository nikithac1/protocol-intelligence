"""Show what the offline pipeline produced: files on disk, the SQLite study store and the LanceDB index.

  python scripts/inspect_index.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from protocol_intelligence.config import DATA_DIR
from protocol_intelligence.knowledge.study_repository import DB_PATH
from protocol_intelligence.retrieval.index import LOCAL_URI, TABLE


# Measure size
def size_mb(path: Path) -> float:
    files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
    return sum(p.stat().st_size for p in files) / 1e6


# Show pipeline outputs
def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"== Files under {DATA_DIR}")
    for rel in ("raw/studies", "raw/protocols", "processed/studies.jsonl", "processed/chunks.jsonl",
                "index/studies.db", "index/lancedb"):
        p = DATA_DIR / rel
        if p.exists():
            count = sum(1 for _ in p.rglob("*") if _.is_file()) if p.is_dir() else 1
            print(f"  {rel:<26} {count:>5} file(s) {size_mb(p):8.1f} MB")

    print(f"\n== SQLite study store ({DB_PATH.name}): structured facts, exact counts")
    con = sqlite3.connect(DB_PATH)
    for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        print(f"  table {table:<14} {con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:>5} rows")
    print("  studies by therapeutic area and status:")
    for row in con.execute("SELECT therapeutic_area, overall_status, COUNT(*) FROM studies "
                           "GROUP BY 1, 2 ORDER BY 1, 3 DESC"):
        print(f"    {row[0]:<20} {row[1]:<24} {row[2]}")

    import lancedb
    table = lancedb.connect(str(LOCAL_URI)).open_table(TABLE)
    print(f"\n== LanceDB index ({LOCAL_URI.name}/{TABLE}): text chunks + vectors")
    print(f"  rows: {table.count_rows()}")
    print(f"  columns: {', '.join(table.schema.names)}")
    row = table.search().where("section_type = 'eligibility' AND source_type = 'protocol_pdf'").limit(1).to_list()[0]
    print(f"  sample chunk: {row['chunk_id']}")
    print(f"    section: {row['section_path']}  (pages {row['page_start']}-{row['page_end']})")
    print(f"    text: {row['text'][:200]}...")
    print(f"    vector: {len(row['vector'])} numbers, first 6 = {[round(v, 3) for v in row['vector'][:6]]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
