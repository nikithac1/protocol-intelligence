"""Phase 2-3 ingestion: discover, select, normalize and download the study corpus.

Usage:
  python scripts/ingest_studies.py --dry-run          # selection + manifest only, no PDFs
  python scripts/ingest_studies.py                    # full run (re-runnable; reuses downloads)
  python scripts/ingest_studies.py --total 15         # small technical spike

Outputs under data-local/:
  raw/studies/<NCT>.json        full API record
  raw/protocols/<NCT>/<file>    protocol PDFs
  processed/studies.jsonl       canonical study records (plan section 4.2)
  manifest.json                 selection + provenance for every study/document
  coverage_report.md            human-readable counts and gaps
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from protocol_intelligence.config import DATA_DIR  # noqa: E402
from protocol_intelligence.ingestion.clinicaltrials_client import ClinicalTrialsClient  # noqa: E402
from protocol_intelligence.ingestion.document_fetcher import fetch_document  # noqa: E402
from protocol_intelligence.ingestion.normalize import canonical_record  # noqa: E402
from protocol_intelligence.ingestion.study_selector import (  # noqa: E402
    THERAPEUTIC_AREAS, discover, select,
)


def write_report(manifest: dict, path: Path) -> None:
    studies = manifest["studies"]
    docs = [d for s in studies for d in s["documents"]]
    doc_status = Counter(d["status"] for d in docs)
    with_pdf = sum(1 for s in studies if any(d["status"] == "downloaded" for d in s["documents"]))

    def table(counter: Counter, title: str) -> list[str]:
        rows = [f"### {title}", "", "| Value | Studies |", "|---|---|"]
        rows += [f"| {k} | {v} |" for k, v in sorted(counter.items())]
        return rows + [""]

    lines = [
        "# Corpus coverage report", "",
        f"Generated: {manifest['generated_at']}  ",
        f"ClinicalTrials.gov API version: {manifest['api_version']}  ",
        f"Selection policy: {manifest['selection_policy']}", "",
        "## Summary", "",
        "| Metric | Count |", "|---|---|",
        f"| Studies selected | {len(studies)} |",
        f"| Studies with >=1 protocol PDF downloaded | {with_pdf} |",
        f"| Metadata-only studies | {len(studies) - with_pdf} |",
        f"| Protocol documents listed | {len(docs)} |",
        *[f"| Documents {k} | {v} |" for k, v in sorted(doc_status.items())],
        "",
        *table(Counter(s["area"] for s in studies), "By therapeutic area"),
        *table(Counter(s["phase"] for s in studies), "By phase"),
        *table(Counter(s["status"] for s in studies), "By overall status"),
    ]
    problems = [d for d in docs if d["status"] not in ("downloaded", "planned")]
    if problems:
        lines += ["## Documents not retrieved", "", "| NCT ID | File | Status | Reason |",
                  "|---|---|---|---|"]
        lines += [f"| {d['nct_id']} | {d['filename']} | {d['status']} | {d.get('reason', '')} |"
                  for d in problems]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--total", type=int, default=100)
    ap.add_argument("--dry-run", action="store_true", help="select + manifest, no PDF downloads")
    args = ap.parse_args()

    client = ClinicalTrialsClient()
    raw_dir, proto_dir, proc_dir = DATA_DIR / "raw" / "studies", DATA_DIR / "raw" / "protocols", DATA_DIR / "processed"
    for d in (raw_dir, proto_dir, proc_dir):
        d.mkdir(parents=True, exist_ok=True)

    api_version = client.api_version()
    print(f"API version {api_version}; discovering candidates across {len(THERAPEUTIC_AREAS)} areas...")
    pool = discover(client)
    chosen = select(pool, args.total)
    print(f"Candidate pool {len(pool)} -> selected {len(chosen)}")

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "api_version": api_version,
        "selection_policy": ("interventional; >=1 posted protocol document; statuses "
                             "COMPLETED/TERMINATED/ACTIVE_NOT_RECRUITING/RECRUITING; equal quota "
                             "per therapeutic area; round-robin over phase x status buckets; "
                             "most recently updated first"),
        "therapeutic_areas": THERAPEUTIC_AREAS,
        "studies": [],
    }
    records = []
    for i, c in enumerate(chosen, 1):
        retrieved_at = datetime.now(UTC).isoformat(timespec="seconds")
        (raw_dir / f"{c.nct_id}.json").write_text(json.dumps(c.raw, indent=1), encoding="utf-8")
        rec = canonical_record(c.raw, api_version, retrieved_at)
        rec["therapeutic_area"] = c.area
        if args.dry_run:
            doc_entries = [{"nct_id": c.nct_id, "filename": d["filename"], "source_url": d["url"],
                            "status": "planned"} for d in rec["protocol_documents"]]
        else:
            doc_entries = [fetch_document(client, c.nct_id, d, proto_dir)
                           for d in rec["protocol_documents"]]
        records.append(rec)
        manifest["studies"].append({
            "nct_id": c.nct_id, "area": c.area, "phase": c.phase, "status": c.status,
            "brief_title": rec["brief_title"], "api_url": f"{client.base_url}/studies/{c.nct_id}",
            "source_record_url": rec["source_record_url"], "selection_reason": c.reason,
            "documents": doc_entries,
        })
        ok = sum(d["status"] == "downloaded" for d in doc_entries)
        print(f"[{i:3}/{len(chosen)}] {c.nct_id} {c.area:<20} {c.phase:<14} {c.status:<22} "
              f"docs {ok}/{len(doc_entries)}")

    with (proc_dir / "studies.jsonl").open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    (DATA_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    write_report(manifest, DATA_DIR / "coverage_report.md")
    print(f"\nWrote {DATA_DIR / 'manifest.json'} and coverage_report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
