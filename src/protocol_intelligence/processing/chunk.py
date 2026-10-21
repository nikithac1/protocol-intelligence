"""Section-aware chunking with overlap and a stable citation payload (plan section 5.3)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from protocol_intelligence.processing.sectionize import Paragraph, Section, classify_section

TARGET_TOKENS = 700
MAX_TOKENS = 900
OVERLAP_TOKENS = 120
MIN_TOKENS = 100  # sections smaller than this are merged forward


def est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _split_long(p: Paragraph) -> list[Paragraph]:
    """Split an oversized paragraph on sentence boundaries."""
    if est_tokens(p.text) <= MAX_TOKENS:
        return [p]
    sentences = re.split(r"(?<=[.!?])\s+", p.text)
    out, buf = [], ""
    for s in sentences:
        if buf and est_tokens(buf + " " + s) > TARGET_TOKENS:
            out.append(Paragraph(buf, p.page_start, p.page_end))
            buf = s
        else:
            buf = f"{buf} {s}".strip()
    if buf:
        out.append(Paragraph(buf, p.page_start, p.page_end))
    # Hard split anything still too long (e.g. tables flattened into one block).
    final = []
    for q in out:
        while est_tokens(q.text) > MAX_TOKENS:
            cut = q.text.rfind(" ", 0, TARGET_TOKENS * 4)
            if cut <= 0:
                cut = TARGET_TOKENS * 4
            final.append(Paragraph(q.text[:cut], q.page_start, q.page_end))
            q = Paragraph(q.text[cut:].strip(), q.page_start, q.page_end)
        final.append(q)
    return final


@dataclass
class RawChunk:
    path: list[str]
    text: str
    page_start: int
    page_end: int


def chunk_sections(sections: list[Section]) -> list[RawChunk]:
    chunks: list[RawChunk] = []
    carry_path: list[str] | None = None
    carry: list[Paragraph] = []

    for sec in sections:
        paras = [q for p in sec.paragraphs for q in _split_long(p)]
        sec_tokens = sum(est_tokens(p.text) for p in paras)
        if carry and carry_path is not None:
            # Prepend the tiny previous section, with its heading for context.
            paras = [Paragraph(f"[{carry_path[-1]}]", carry[0].page_start, carry[0].page_start),
                     *carry, *paras]
            path = sec.path
            carry, carry_path = [], None
        else:
            path = sec.path
        if sec_tokens < MIN_TOKENS and sec is not sections[-1]:
            carry, carry_path = paras, path
            continue

        buf: list[Paragraph] = []
        for p in paras:
            if buf and sum(est_tokens(x.text) for x in buf) + est_tokens(p.text) > TARGET_TOKENS:
                chunks.append(_emit(path, buf))
                # Overlap: keep trailing paragraphs up to OVERLAP_TOKENS.
                tail, n = [], 0
                for x in reversed(buf):
                    n += est_tokens(x.text)
                    if n > OVERLAP_TOKENS:
                        break
                    tail.insert(0, x)
                buf = tail
            buf.append(p)
        if buf:
            chunks.append(_emit(path, buf))
    if carry:
        chunks.append(_emit(carry_path or ["Document"], carry))
    return chunks


def _emit(path: list[str], paras: list[Paragraph]) -> RawChunk:
    return RawChunk(path=list(path), text="\n".join(p.text for p in paras),
                    page_start=min(p.page_start for p in paras),
                    page_end=max(p.page_end for p in paras))


def page_chunks(lines_by_page: dict[int, str]) -> list[RawChunk]:
    """Fallback when headings are not detectable: one chunk per ~TARGET_TOKENS of pages."""
    paras = [Paragraph(t, p, p) for p, t in sorted(lines_by_page.items()) if t.strip()]
    sec = Section(path=["(no detectable sections)"], paragraphs=paras)
    return chunk_sections([sec])


def build_chunk_record(*, nct_id: str, doc_key: str, document_title: str, document_version: str,
                       source_url: str, source_sha256: str, raw: RawChunk, seq: int,
                       extraction_method: str, extraction_confidence: str,
                       source_type: str = "protocol_pdf") -> dict:
    section_slug = re.sub(r"[^a-z0-9]+", "-", raw.path[-1].lower()).strip("-")[:40] or "section"
    return {
        "chunk_id": f"{nct_id}|{doc_key}|{section_slug}|p{raw.page_start}|{seq:04d}",
        "nct_id": nct_id,
        "source_type": source_type,
        "document_title": document_title,
        "document_version": document_version,
        "section_path": raw.path,
        "section_type": classify_section(raw.path),
        "page_start": raw.page_start,
        "page_end": raw.page_end,
        "source_url": source_url,
        "source_sha256": source_sha256,
        "text": raw.text,
        "token_estimate": est_tokens(raw.text),
        "extraction_method": extraction_method,
        "extraction_confidence": extraction_confidence,
    }
