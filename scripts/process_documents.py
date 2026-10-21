"""Phase 3: extract, sectionize and chunk every downloaded protocol + registry record.

Usage:
  python scripts/process_documents.py               # all studies in the manifest
  python scripts/process_documents.py --only NCT02229084 --show 3   # debug one doc

Outputs under data-local/processed/:
  chunks.jsonl              every chunk with citation metadata
  sections/<NCT>__<file>.json   section outline per document (for review/debugging)
  extraction_report.md      per-document status, headings, chunks, confidence
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from protocol_intelligence.config import DATA_DIR  # noqa: E402
from protocol_intelligence.processing.chunk import (  # noqa: E402
    build_chunk_record, chunk_sections, page_chunks,
)
from protocol_intelligence.processing.pdf_extract import extract  # noqa: E402
from protocol_intelligence.processing.registry_chunks import registry_chunks  # noqa: E402
from protocol_intelligence.processing.sectionize import sectionize  # noqa: E402

MIN_HEADINGS_FOR_SECTIONS = 5


def process_pdf(doc: dict, title: str) -> tuple[list[dict], dict]:
    path = DATA_DIR / doc["local_path"]
    ex = extract(str(path))
    status = {"nct_id": doc["nct_id"], "filename": doc["filename"], "pages": ex.page_count,
              "toc_pages": len(ex.toc_pages), "garbled_pages": len(ex.garbled_pages),
              "boilerplate_lines_removed": ex.removed_boilerplate}
    if ex.needs_ocr:
        return [], {**status, "status": "needs_ocr", "headings": 0, "chunks": 0}

    sections, headings = sectionize(ex)
    if headings >= MIN_HEADINGS_FOR_SECTIONS:
        raws, method, confidence = chunk_sections(sections), "pymupdf+headings", "high"
        if headings < 15:
            confidence = "medium"
    else:
        by_page: dict[int, list[str]] = {}
        for ln in ex.lines:
            by_page.setdefault(ln.page, []).append(ln.text)
        raws = page_chunks({p: " ".join(t) for p, t in by_page.items()})
        method, confidence = "pymupdf+pages", "low"

    doc_key = Path(doc["filename"]).stem.lower()
    chunks = [
        build_chunk_record(
            nct_id=doc["nct_id"], doc_key=doc_key, document_title=doc.get("label") or title,
            document_version=doc.get("document_date") or doc_key, source_url=doc["source_url"],
            source_sha256=doc["sha256"], raw=r, seq=i, extraction_method=method,
            extraction_confidence=confidence,
        )
        for i, r in enumerate(raws)
    ]
    outline = [{"path": s.path, "type": s.section_type,
                "pages": [s.paragraphs[0].page_start, s.paragraphs[-1].page_end]} for s in sections]
    out = DATA_DIR / "processed" / "sections" / f"{doc['nct_id']}__{doc_key}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(outline, indent=1), encoding="utf-8")
    return chunks, {**status, "status": "ok", "headings": headings, "chunks": len(chunks),
                    "method": method, "confidence": confidence}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="process a single NCT ID")
    ap.add_argument("--show", type=int, default=0, help="print N sample chunks")
    args = ap.parse_args()

    manifest = json.loads((DATA_DIR / "manifest.json").read_text(encoding="utf-8"))
    records = {json.loads(l)["nct_id"]: json.loads(l)
               for l in (DATA_DIR / "processed" / "studies.jsonl").read_text(encoding="utf-8").splitlines()}

    all_chunks: list[dict] = []
    report: list[dict] = []
    for study in manifest["studies"]:
        nct = study["nct_id"]
        if args.only and nct != args.only:
            continue
        all_chunks.extend(registry_chunks(records[nct]))
        for doc in study["documents"]:
            if doc["status"] != "downloaded":
                report.append({"nct_id": nct, "filename": doc["filename"], "status": doc["status"]})
                continue
            try:
                chunks, status = process_pdf(doc, study["brief_title"])
            except Exception as e:  # one bad PDF must not stop the corpus
                chunks, status = [], {"nct_id": nct, "filename": doc["filename"],
                                      "status": "failed", "reason": f"{type(e).__name__}: {e}"[:200]}
            # Traceability rule: reject chunks missing citation-critical metadata.
            chunks = [c for c in chunks if c["nct_id"] and c["source_url"] and c["text"].strip()]
            all_chunks.extend(chunks)
            report.append(status)
            print(f"{nct} {doc['filename']:<22} {status['status']:<9} pages={status.get('pages', '-'):<4} "
                  f"headings={status.get('headings', '-'):<4} chunks={status.get('chunks', 0):<4} "
                  f"{status.get('confidence', status.get('reason', ''))}")

    if args.show:
        for c in [c for c in all_chunks if c["source_type"] == "protocol_pdf"][: args.show]:
            print("\n---", c["chunk_id"], c["section_type"], c["section_path"], c["token_estimate"])
            print(c["text"][:700])
    if args.only:
        return 0

    out = DATA_DIR / "processed" / "chunks.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for c in all_chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    ids = Counter(c["chunk_id"] for c in all_chunks)
    dupes = [k for k, v in ids.items() if v > 1]

    pdf_chunks = [c for c in all_chunks if c["source_type"] == "protocol_pdf"]
    lines = [
        "# Extraction report", "",
        "| Metric | Value |", "|---|---|",
        f"| Documents processed | {len(report)} |",
        *[f"| Documents {k} | {v} |" for k, v in sorted(Counter(r['status'] for r in report).items())],
        *[f"| Confidence {k} | {v} |" for k, v in sorted(Counter(r.get('confidence') for r in report if r.get('confidence')).items())],
        f"| Protocol chunks | {len(pdf_chunks)} |",
        f"| Registry chunks | {len(all_chunks) - len(pdf_chunks)} |",
        f"| Mean protocol chunk tokens (est.) | {sum(c['token_estimate'] for c in pdf_chunks) // max(1, len(pdf_chunks))} |",
        f"| Duplicate chunk IDs | {len(dupes)} |",
        "", "## Protocol chunks by section type", "", "| Section type | Chunks |", "|---|---|",
        *[f"| {k} | {v} |" for k, v in Counter(c['section_type'] for c in pdf_chunks).most_common()],
        "", "## Per document", "",
        "| NCT ID | File | Status | Pages | Headings | Chunks | Confidence |", "|---|---|---|---|---|---|---|",
        *[f"| {r['nct_id']} | {r['filename']} | {r['status']} | {r.get('pages', '')} | "
          f"{r.get('headings', '')} | {r.get('chunks', '')} | {r.get('confidence', r.get('reason', ''))} |"
          for r in report],
    ]
    (DATA_DIR / "processed" / "extraction_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{len(all_chunks)} chunks ({len(pdf_chunks)} protocol) -> {out}; duplicate ids: {len(dupes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
