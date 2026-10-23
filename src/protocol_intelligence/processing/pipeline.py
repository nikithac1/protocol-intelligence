"""One protocol PDF -> citation-ready chunks (shared by the batch script and admin uploads)."""

from __future__ import annotations

from pathlib import Path

from protocol_intelligence.processing.chunk import build_chunk_record, chunk_sections, page_chunks
from protocol_intelligence.processing.pdf_extract import extract
from protocol_intelligence.processing.sectionize import sectionize

MIN_HEADINGS_FOR_SECTIONS = 5


def process_pdf(path: Path, *, nct_id: str, doc_key: str, document_title: str,
                document_version: str, source_url: str, sha256: str
                ) -> tuple[list[dict], dict, list[dict]]:
    """Returns (chunks, status, section outline). status["status"] is "ok" or "needs_ocr"."""
    ex = extract(str(path))
    status = {"nct_id": nct_id, "pages": ex.page_count, "toc_pages": len(ex.toc_pages),
              "garbled_pages": len(ex.garbled_pages),
              "boilerplate_lines_removed": ex.removed_boilerplate}
    if ex.needs_ocr:
        return [], {**status, "status": "needs_ocr", "headings": 0, "chunks": 0}, []

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

    chunks = [
        build_chunk_record(
            nct_id=nct_id, doc_key=doc_key, document_title=document_title,
            document_version=document_version, source_url=source_url, source_sha256=sha256,
            raw=r, seq=i, extraction_method=method, extraction_confidence=confidence,
        )
        for i, r in enumerate(raws)
    ]
    # Traceability rule: reject chunks missing citation-critical metadata.
    chunks = [c for c in chunks if c["nct_id"] and c["source_url"] and c["text"].strip()]
    outline = [{"path": s.path, "type": s.section_type,
                "pages": [s.paragraphs[0].page_start, s.paragraphs[-1].page_end]} for s in sections]
    return chunks, {**status, "status": "ok", "headings": headings, "chunks": len(chunks),
                    "method": method, "confidence": confidence}, outline
