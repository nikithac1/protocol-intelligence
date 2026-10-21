from protocol_intelligence.agent.evidence import EvidenceLedger, citation_label


def _p(cid, source_type="protocol_pdf", pages=(12, 13)):
    return {"chunk_id": cid, "nct_id": "NCT00000001", "source_type": source_type,
            "document_title": "Study Protocol", "section_path": "5 > 5.2 Exclusion Criteria",
            "page_start": pages[0], "page_end": pages[1], "source_url": "https://x", "text": "t"}


def test_same_chunk_keeps_its_ref():
    ledger = EvidenceLedger()
    assert ledger.register(_p("a")) == "S1"
    assert ledger.register(_p("b")) == "S2"
    assert ledger.register(_p("a")) == "S1"


def test_resolve_orders_by_first_use_and_flags_invented_refs():
    ledger = EvidenceLedger()
    ledger.register(_p("a"))
    ledger.register(_p("b"))
    r = ledger.resolve("Claim [S2]. Other [S1][S2]. Made up [S9].")
    assert [c["ref"] for c in r["citations"]] == ["S2", "S1"]
    assert r["unknown_refs"] == ["S9"]


def test_labels():
    assert citation_label(_p("a")) == ('NCT00000001, Study Protocol, pp. 12-13, '
                                       'section "5 > 5.2 Exclusion Criteria"')
    assert "p. 12," in citation_label(_p("a", pages=(12, 12)))
    assert "ClinicalTrials.gov record" in citation_label(_p("a", "registry", (None, None)))
