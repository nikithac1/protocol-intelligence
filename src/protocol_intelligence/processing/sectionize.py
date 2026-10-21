"""Detect headings, build heading paths, and group paragraphs into sections."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from protocol_intelligence.processing.pdf_extract import ExtractedDoc, Line

_NUMBERED = re.compile(r"^(?:section\s+)?(\d{1,2}(?:\.\d{1,2}){0,4})\.?\s+([A-Za-z].{1,110})$", re.I)
_APPENDIX = re.compile(r"^(appendix|attachment|annex)\s+[\w\d]+\b", re.I)
_LETTERED = re.compile(r"^([A-Z])\.\s+([A-Z][A-Za-z].{1,100})$")
_SENTENCE_END = re.compile(r"[.;:,]$")

# Canonical section types used as retrieval filters. First match wins, so order matters.
SECTION_TYPES: list[tuple[str, tuple[str, ...]]] = [
    ("abbreviations", ("abbreviation", "glossary", "list of terms", "definitions")),
    ("synopsis", ("synopsis", "protocol summary", "study summary", "executive summary")),
    ("eligibility", ("eligib", "inclusion", "exclusion", "selection of", "study population",
                     "subject selection", "participant selection", "patient selection",
                     "patient population", "subject population", "participant population",
                     "trial population", "recruitment")),
    ("endpoints_outcomes", ("endpoint", "outcome measure", "efficacy assessment",
                            "efficacy evaluation", "response criteria")),
    ("objectives", ("objective", "aims", "hypothes")),
    ("statistics", ("statistic", "sample size", "power calculation", "analysis population",
                    "data analysis", "interim analysis")),
    ("safety_adverse_events", ("adverse", "safety", "toxicit", "serious", "dose limiting",
                               "stopping rule", "data safety monitoring", "dsmb")),
    ("treatment_dosing", ("treatment", "dosing", "dose", "administration", "intervention",
                          "study drug", "investigational product", "regimen",
                          "concomitant", "prohibited")),
    ("schedule_procedures", ("schedule", "procedure", "visit", "assessments",
                             "study calendar", "follow-up", "follow up")),
    ("study_design", ("study design", "design", "randomi", "blinding", "masking",
                      "overview", "synopsis", "schema")),
    ("withdrawal_discontinuation", ("withdraw", "discontinu", "early termination")),
    ("background_rationale", ("background", "rationale", "introduction")),
    ("references", ("reference", "bibliograph", "literature cited", "citations")),
    ("ethics_regulatory", ("ethic", "consent", "irb", "regulatory", "confidential",
                           "data management", "record", "monitoring", "publication",
                           "agreement", "signature", "approval", "amendment", "revision history")),
    ("biomarkers_correlative", ("biomarker", "correlative", "pharmacokinetic", "laboratory",
                                "specimen", "sample collection", "imaging")),
]


def classify_section(path: list[str]) -> str:
    for heading in reversed(path):  # most specific heading first
        h = heading.lower().replace(" ", "")  # tolerate kerning-split text: "S y n o psis"
        for section_type, keys in SECTION_TYPES:
            if any(k.replace(" ", "") in h for k in keys):
                return section_type
    return "other"


@dataclass
class Paragraph:
    text: str
    page_start: int
    page_end: int


@dataclass
class Section:
    path: list[str]
    paragraphs: list[Paragraph] = field(default_factory=list)

    @property
    def section_type(self) -> str:
        return classify_section(self.path)


_MONTHS = {"january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december", "jan", "feb", "mar", "apr", "jun",
           "jul", "aug", "sep", "sept", "oct", "nov", "dec"}
_UNITS = {"mg", "g", "kg", "mcg", "µg", "ml", "l", "gy", "iu", "units", "mmol", "mm", "cm",
          "days", "day", "weeks", "week", "months", "years", "hours", "hrs", "total", "or",
          "calendar", "minutes", "min", "x", "times", "cycles", "doses"}


def _real_numbered(text: str, m: re.Match) -> bool:
    """'5.2 Dose Modifications' yes; '50 mg', '31 January 2025', '20 Gy' no."""
    number, title = m.group(1), m.group(2)
    first = title.split()[0].lower().strip("*,/")
    if first in _MONTHS or first in _UNITS or first.startswith("mg"):
        return False
    if int(number.split(".")[0]) > 30:  # protocol section numbers stay small
        return False
    has_dot = "." in number or text[len(number):len(number) + 1] == "."
    return has_dot or title.isupper() or len(title.split()) >= 2


def _followed_by_prose(line: Line, nxt: Line | None) -> bool:
    """Table cells are short, bold, and followed by more short cells; real headings by prose."""
    return nxt is not None and nxt.block != line.block and not nxt.bold and len(nxt.text) >= 40


def _heading_level(line: Line, body_size: float, nxt: Line | None = None) -> int | None:
    """Return heading level (1 = top) or None if the line is body text."""
    text = line.text.strip()
    words = text.split()
    if not (2 <= len(text) <= 120) or len(words) > 16 or _SENTENCE_END.search(text):
        return None
    if not any(c.isalpha() for c in text):
        return None
    if sum(c.isalpha() for c in text) < 4:  # "CCI" redaction boxes, stray labels
        return None
    emphasized = line.bold or line.size >= body_size + 0.8
    m = _NUMBERED.match(text)
    if m and not _real_numbered(text, m):
        return None
    if m:
        # Numbered lines are headings only when emphasized or title-like (rules out numbered list items).
        title = m.group(2)
        titlecase = title.isupper() or sum(w[0].isupper() for w in title.split()) >= 0.6 * len(title.split())
        if emphasized or titlecase:
            return min(m.group(1).count(".") + 1, 4)
        return None
    if _APPENDIX.match(text) and emphasized:
        return 1
    # Unnumbered headings: must start uppercase and be followed by prose (rules out table cells).
    if not text[0].isupper() or not _followed_by_prose(line, nxt):
        return None
    if _LETTERED.match(text) and emphasized:
        return 3
    if emphasized and (text.isupper() or line.size >= body_size + 1.5):
        return 1 if line.size >= body_size + 1.5 else 2
    if line.bold and len(words) <= 8:
        return 3
    return None


def sectionize(doc: ExtractedDoc) -> tuple[list[Section], int]:
    """Returns sections and the number of headings detected."""
    sections: list[Section] = [Section(path=["Front matter"])]
    stack: list[tuple[int, str, str | None]] = []  # (level, text, section number)
    headings = 0
    cur_block, cur_lines, cur_pages = None, [], []

    def flush():
        nonlocal cur_lines, cur_pages
        if cur_lines:
            text = " ".join(cur_lines)
            text = re.sub(r"(\w)- (\w)", r"\1\2", text)  # de-hyphenate line breaks
            sections[-1].paragraphs.append(Paragraph(text, cur_pages[0], cur_pages[-1]))
        cur_lines, cur_pages = [], []

    for i, line in enumerate(doc.lines):
        nxt = doc.lines[i + 1] if i + 1 < len(doc.lines) else None
        level = _heading_level(line, doc.body_size, nxt)
        if level is not None:
            flush()
            headings += 1
            m = _NUMBERED.match(line.text.strip())
            number = m.group(1) if m else None
            if number:
                # "6.5" may only nest under "6" (or "6.x" ancestors); drops misplaced parents
                # when the parent heading itself was missed.
                while stack:
                    top_level, _, top_number = stack[-1]
                    if top_number and number.startswith(top_number + "."):
                        break
                    if not top_number and top_level < level:
                        break  # keep a genuinely higher unnumbered ancestor
                    stack.pop()
            else:
                while stack and stack[-1][0] >= level:
                    stack.pop()
            stack.append((level, line.text.strip(), number))
            sections.append(Section(path=[h for _, h, _ in stack]))
            cur_block = None
            continue
        # Bullet / list markers start a new paragraph so lists are never merged into prose.
        starts_item = bool(re.match(r"^([•\-–▪○●]|\d{1,2}[.)]|[a-z][.)]|\(\w{1,3}\))\s", line.text))
        if line.block != cur_block or starts_item:
            flush()
            cur_block = line.block
        cur_lines.append(line.text)
        cur_pages.append(line.page)
    flush()
    return [s for s in sections if s.paragraphs], headings
