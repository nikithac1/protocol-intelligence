"""Turn a protocol PDF into an ordered list of lines with layout hints.

Output is layout-aware but format-agnostic: each Line carries page, font size, bold flag
and block id so sectionize.py can detect headings and paragraphs. Repeated running
headers/footers and table-of-contents pages are removed here.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

_DOT_LEADER = re.compile(r"(\.{4,}|…{2,}|\s{2,})\s*\d{1,3}\s*$")
_DIGITS = re.compile(r"\d+")
# "S T A T I S T I C A L" -> "STATISTICAL" (letter-spaced headings)
_LETTER_SPACED = re.compile(r"\b(?:[A-Z]{1,2} ){2,}[A-Z]{1,2}\b")
_STOPWORDS = {"the", "and", "of", "to", "in", "for", "with", "be", "is", "will", "or", "a",
              "on", "by", "as", "at", "are", "this", "from", "study", "patients", "an", "not"}


def _is_garbled(page_text: str) -> bool:
    """Broken font encodings yield symbol soup; real prose has plenty of stopwords."""
    tokens = re.findall(r"[A-Za-z]+", page_text.lower())
    if len(tokens) < 30:
        return False  # too little text to judge (figures, signature pages)
    return sum(t in _STOPWORDS for t in tokens) / len(tokens) < 0.04


@dataclass
class Line:
    text: str
    page: int  # 1-based
    size: float
    bold: bool
    block: int  # global block counter; lines sharing a block form a paragraph


@dataclass
class ExtractedDoc:
    lines: list[Line]
    page_count: int
    body_size: float
    text_chars: int
    toc_pages: list[int] = field(default_factory=list)
    garbled_pages: list[int] = field(default_factory=list)
    removed_boilerplate: int = 0

    @property
    def needs_ocr(self) -> bool:
        too_sparse = self.page_count > 0 and self.text_chars / self.page_count < 200
        return too_sparse or len(self.garbled_pages) > 0.5 * self.page_count


def _norm_boiler(text: str) -> str:
    return _DIGITS.sub("#", text.strip().lower())


def extract(path: str) -> ExtractedDoc:
    doc = pymupdf.open(path)
    pages: list[list[Line]] = []
    block_id = 0
    sizes: Counter[float] = Counter()

    for pno, page in enumerate(doc, start=1):
        page_lines: list[Line] = []
        for b in page.get_text("dict", sort=True)["blocks"]:
            if b.get("type") != 0:
                continue
            block_id += 1
            for ln in b["lines"]:
                spans = [s for s in ln["spans"] if s["text"].strip()]
                if not spans:
                    continue
                text = " ".join("".join(s["text"] for s in ln["spans"]).split())
                text = _LETTER_SPACED.sub(lambda m: m.group(0).replace(" ", ""), text)
                chars = sum(len(s["text"]) for s in spans)
                size = round(max(s["size"] for s in spans), 1)
                bold_chars = sum(len(s["text"]) for s in spans
                                 if s["flags"] & 16 or "bold" in s["font"].lower())
                sizes[round(spans[0]["size"], 1)] += chars
                page_lines.append(Line(text, pno, size, bold_chars >= 0.8 * chars, block_id))
        pages.append(page_lines)

    # Running headers/footers: normalized text seen in the top/bottom 6 lines of >=30% of pages.
    edge: Counter[str] = Counter()
    for pl in pages:
        for ln in {_norm_boiler(x.text) for x in pl[:6] + pl[-6:]}:
            edge[ln] += 1
    threshold = max(3, int(0.3 * len(pages)))
    boiler = {t for t, c in edge.items() if c >= threshold and len(t) < 160}

    lines: list[Line] = []
    removed = 0
    toc_pages: list[int] = []
    garbled: list[int] = []
    for pno, pl in enumerate(pages, start=1):
        if _is_garbled(" ".join(x.text for x in pl)):
            garbled.append(pno)  # excluded from text; candidate for OCR
            continue
        kept = []
        for i, ln in enumerate(pl):
            at_edge = i < 6 or i >= len(pl) - 6
            if at_edge and (_norm_boiler(ln.text) in boiler
                            or re.fullmatch(r"(page\s*)?\d+(\s*(of|/)\s*\d+)?", ln.text, re.I)):
                removed += 1
                continue
            kept.append(ln)
        # TOC page: many lines ending in a dot leader + page number.
        leaders = sum(1 for ln in kept if _DOT_LEADER.search(ln.text))
        if kept and leaders >= 5 and leaders >= 0.25 * len(kept):
            toc_pages.append(kept[0].page)
            continue
        lines.extend(kept)

    body = sizes.most_common(1)[0][0] if sizes else 10.0
    return ExtractedDoc(lines, doc.page_count, body, sum(len(x.text) for x in lines),
                        toc_pages, garbled, removed)
