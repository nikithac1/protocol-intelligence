"""Phase 4-5: build the SQLite study store and the LanceDB chunk index.

  python scripts/build_index.py           # build both (embeddings are cached; rebuilds are cheap)
  python scripts/build_index.py --studies-only
Then: python scripts/sync_s3.py up --prefix index
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from protocol_intelligence.config import DATA_DIR  # noqa: E402
from protocol_intelligence.knowledge import study_repository  # noqa: E402
from protocol_intelligence.retrieval import index  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--studies-only", action="store_true")
    args = ap.parse_args()

    studies = DATA_DIR / "processed" / "studies.jsonl"
    n = study_repository.build(studies)
    print(f"SQLite study store: {n} studies -> {study_repository.DB_PATH}")
    if args.studies_only:
        return 0

    areas = {json.loads(l)["nct_id"]: json.loads(l).get("therapeutic_area", "")
             for l in studies.read_text(encoding="utf-8").splitlines()}
    t0 = time.time()
    rows = index.build(DATA_DIR / "processed" / "chunks.jsonl", areas,
                       progress=lambda i, total: print(f"  embedded {i}/{total}", flush=True))
    print(f"LanceDB index: {rows} chunks -> {index.LOCAL_URI} ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
