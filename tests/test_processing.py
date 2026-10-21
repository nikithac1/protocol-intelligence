from protocol_intelligence.processing.chunk import MAX_TOKENS, chunk_sections, est_tokens
from protocol_intelligence.processing.pdf_extract import ExtractedDoc, Line, _is_garbled
from protocol_intelligence.processing.sectionize import (
    Paragraph, Section, _heading_level, classify_section, sectionize,
)

BODY = 10.0
PROSE = Line("This paragraph is ordinary protocol prose that follows a heading.", 1, BODY, False, 99)


def level(text, bold=True, size=BODY, nxt=PROSE):
    return _heading_level(Line(text, 1, size, bold, 1), BODY, nxt)


def test_numbered_headings_detected_with_depth():
    assert level("5. ELIGIBILITY CRITERIA") == 1
    assert level("5.2 Exclusion Criteria") == 2
    assert level("5.2.1 Prior Therapy") == 3


def test_doses_dates_and_redactions_are_not_headings():
    for text in ["50 mg", "31 January 2025", "20 Gy", "3 mg/kg or 240 mg", "7 Calendar Days", "CCI"]:
        assert level(text) is None, text


def test_bold_table_cells_are_not_headings():
    next_cell = Line("ALT", 1, BODY, True, 2)
    assert level("Total Bilirubin", nxt=next_cell) is None


def test_sentences_are_not_headings():
    assert level("Patients must have adequate renal function.", bold=False) is None


def test_numbered_child_never_nests_under_wrong_parent():
    lines = [
        Line("1. STUDY SYNOPSIS", 1, 12, True, 1), PROSE,
        Line("6.5 Exclusion Criteria", 5, 12, True, 3),
        Line("Patients with active infection are excluded from participation here.", 5, BODY, False, 4),
    ]
    sections, _ = sectionize(ExtractedDoc(lines, 5, BODY, 500))
    assert sections[-1].path == ["6.5 Exclusion Criteria"]
    assert sections[-1].section_type == "eligibility"


def test_classification_tolerates_kerning_split_text():
    assert classify_section(["S y n o psis"]) == "synopsis"
    assert classify_section(["7. STATISTICAL METHODS", "7.1 Sample Size"]) == "statistics"
    assert classify_section(["Exclusion Criteria"]) == "eligibility"


def test_garbled_text_detection():
    assert _is_garbled(" ".join(["9=05>0>6 ;0<:3456 G H I -J K"] * 10))
    assert not _is_garbled("The study will enroll patients with diabetes and the primary endpoint is " * 5)


def test_chunks_respect_max_size_and_keep_pages():
    paras = [Paragraph("word " * 300, p, p) for p in range(1, 8)]
    chunks = chunk_sections([Section(path=["4. Treatment Plan"], paragraphs=paras)])
    assert len(chunks) > 1
    assert all(est_tokens(c.text) <= MAX_TOKENS + 200 for c in chunks)  # + overlap allowance
    assert chunks[0].page_start == 1 and chunks[-1].page_end == 7
