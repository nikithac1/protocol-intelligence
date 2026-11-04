"""Build the knowledge graph (studies -> interventions, conditions, sponsors, areas) as CSV.

  python scripts/build_graph.py
Writes openCypher CSV files to data-local/index/graph/ (the format Neptune Analytics bulk-loads).
Then: python scripts/sync_s3.py up --prefix index
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from protocol_intelligence.config import DATA_DIR  # noqa: E402
from protocol_intelligence.knowledge import graph, study_repository  # noqa: E402


# Build graph files
def main() -> int:
    processed = DATA_DIR / "processed"
    records = study_repository.read_records(processed / "studies.jsonl",
                                            processed / "uploaded_studies.jsonl")
    nodes, edges = graph.build(records)
    stats = graph.write_csv(nodes, edges)
    print(f"Knowledge graph -> {graph.GRAPH_DIR}")
    for label, n in stats["nodes"].items():
        print(f"  {label:<13} {n:>4} nodes")
    print(f"  {'edges':<13} {stats['edges']:>4}")
    unlinked = [r["nct_id"] for r in records if not r.get("mesh_interventions")]
    print(f"  {len(unlinked)} studies have no MeSH interventions (no drug links)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
