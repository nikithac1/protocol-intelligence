"""Compare vector, keyword (BM25) and hybrid retrieval for one query.

  python scripts/demo_search.py "patients with brain metastases excluded"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from protocol_intelligence.retrieval.hybrid import search


# Compare search modes
def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default="patients with brain metastases excluded")
    ap.add_argument("-k", type=int, default=5)
    args = ap.parse_args()

    print(f"query: {args.query}")
    for mode in ("vector", "keyword", "hybrid"):
        print(f"\n== {mode}")
        for h in search(args.query, k=args.k, mode=mode):
            print(f"  {h['score']:.4f} {h['nct_id']} {h['section_type']:<22} {h['matched_by']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
