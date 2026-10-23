import json

import pytest

from protocol_intelligence.knowledge import study_repository as repo
from protocol_intelligence.retrieval.hybrid import build_filter


def _study(nct, phases, status, area, enrollment, min_age="18 Years", max_age=None):
    return {
        "nct_id": nct, "brief_title": f"Study {nct}", "official_title": "", "overall_status": status,
        "phases": phases, "study_type": "INTERVENTIONAL", "allocation": "RANDOMIZED",
        "intervention_model": "PARALLEL", "primary_purpose": "TREATMENT", "masking": "NONE",
        "enrollment_count": enrollment, "enrollment_type": "ACTUAL", "sex": "ALL",
        "minimum_age": min_age, "maximum_age": max_age, "healthy_volunteers": False,
        "study_start": "2021-03", "primary_completion": None, "completion": None,
        "last_update": "2024-01-01", "lead_sponsor": "Acme", "lead_sponsor_class": "INDUSTRY",
        "has_results": True, "therapeutic_area": area, "brief_summary": "", "conditions": ["Type 2 Diabetes"],
        "mesh_conditions": [], "interventions": [{"type": "DRUG", "name": "Metformin"}],
        "mesh_interventions": [], "primary_outcomes": [{"measure": "HbA1c", "time_frame": "26 weeks",
                                                        "description": None}],
        "secondary_outcomes": [], "protocol_documents": [], "source_record_url": f"https://x/{nct}",
        "retrieved_at": "2026-10-20", "source_api_version": "2.0.5",
    }


@pytest.fixture
def db(tmp_path):
    jsonl = tmp_path / "studies.jsonl"
    rows = [_study("NCT00000001", ["PHASE3"], "RECRUITING", "diabetes_metabolic", 300),
            _study("NCT00000002", ["PHASE2", "PHASE3"], "COMPLETED", "diabetes_metabolic", 100),
            _study("NCT00000003", ["PHASE1"], "RECRUITING", "oncology", 20, "12 Years", "17 Years")]
    jsonl.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    path = tmp_path / "studies.db"
    repo.build(jsonl, db_path=path)
    return path


def test_exact_counts(db):
    assert repo.query_cohort({"phase": "PHASE3"}, db_path=db)["total_studies"] == 2
    assert repo.query_cohort({"phase": "PHASE3", "overall_status": "recruiting"},
                             db_path=db)["total_studies"] == 1


def test_group_by_phase_notes_double_counting(db):
    r = repo.query_cohort(group_by="phase", db_path=db)
    assert {row["phase"]: row["studies"] for row in r["rows"]} == {"PHASE3": 2, "PHASE2": 1, "PHASE1": 1}
    assert r["total_studies"] == 3 and "note" in r


def test_age_filter(db):
    assert [r["nct_id"] for r in repo.query_cohort({"accepts_age": 15}, db_path=db)["rows"]] == ["NCT00000003"]


def test_unknown_filter_and_group_by_rejected(db):
    with pytest.raises(ValueError):
        repo.query_cohort({"1=1; DROP TABLE studies": 1}, db_path=db)
    with pytest.raises(ValueError):
        repo.query_cohort(group_by="brief_title", db_path=db)


def test_injection_in_values_is_parameterized(db):
    r = repo.query_cohort({"condition": "x' OR '1'='1"}, db_path=db)
    assert r["total_studies"] == 0


def test_vector_filter_rejects_unsafe_values():
    assert build_filter(nct_ids=["nct00000001"]) == "nct_id IN ('NCT00000001')"
    with pytest.raises(ValueError):
        build_filter(section_types=["eligibility') OR ('a'='a"])


def test_age_parsing():
    assert repo.age_years("18 Years") == 18
    assert repo.age_years("6 Months") == 0.5
    assert repo.age_years(None) is None
