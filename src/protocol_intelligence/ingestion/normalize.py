"""Map a raw API v2 study into the canonical study record (plan section 4.2)."""

from __future__ import annotations

from protocol_intelligence.ingestion.clinicaltrials_client import protocol_doc_url, study_page_url


def _date(module: dict, key: str) -> str | None:
    return (module.get(key) or {}).get("date")


def _outcomes(items: list[dict] | None) -> list[dict]:
    return [
        {"measure": o.get("measure"), "description": o.get("description"),
         "time_frame": o.get("timeFrame")}
        for o in items or []
    ]


def protocol_docs(raw: dict) -> list[dict]:
    nct = raw["protocolSection"]["identificationModule"]["nctId"]
    docs = (raw.get("documentSection") or {}).get("largeDocumentModule", {}).get("largeDocs", [])
    return [
        {**d, "url": protocol_doc_url(nct, d["filename"])}
        for d in docs if d.get("hasProtocol") and d.get("filename")
    ]


def canonical_record(raw: dict, api_version: str, retrieved_at: str) -> dict:
    ps = raw["protocolSection"]
    ident = ps["identificationModule"]
    status = ps.get("statusModule", {})
    design = ps.get("designModule", {})
    elig = ps.get("eligibilityModule", {})
    outcomes = ps.get("outcomesModule", {})
    arms = ps.get("armsInterventionsModule", {})
    sponsor = ps.get("sponsorCollaboratorsModule", {})
    enrollment = design.get("enrollmentInfo", {})
    nct = ident["nctId"]

    return {
        "nct_id": nct,
        "source_api_version": api_version,
        "retrieved_at": retrieved_at,
        "brief_title": ident.get("briefTitle"),
        "official_title": ident.get("officialTitle"),
        "overall_status": status.get("overallStatus"),
        "phases": design.get("phases", []),
        "study_type": design.get("studyType"),
        "allocation": design.get("designInfo", {}).get("allocation"),
        "intervention_model": design.get("designInfo", {}).get("interventionModel"),
        "primary_purpose": design.get("designInfo", {}).get("primaryPurpose"),
        "masking": design.get("designInfo", {}).get("maskingInfo", {}).get("masking"),
        "conditions": ps.get("conditionsModule", {}).get("conditions", []),
        "keywords": ps.get("conditionsModule", {}).get("keywords", []),
        "interventions": [
            {"type": i.get("type"), "name": i.get("name"), "description": i.get("description")}
            for i in arms.get("interventions", [])
        ],
        "arm_groups": [
            {"label": a.get("label"), "type": a.get("type"), "description": a.get("description")}
            for a in arms.get("armGroups", [])
        ],
        "eligibility_text": elig.get("eligibilityCriteria"),
        "sex": elig.get("sex"),
        "minimum_age": elig.get("minimumAge"),
        "maximum_age": elig.get("maximumAge"),
        "healthy_volunteers": elig.get("healthyVolunteers"),
        "primary_outcomes": _outcomes(outcomes.get("primaryOutcomes")),
        "secondary_outcomes": _outcomes(outcomes.get("secondaryOutcomes")),
        "enrollment_count": enrollment.get("count"),
        "enrollment_type": enrollment.get("type"),
        "study_start": _date(status, "startDateStruct"),
        "primary_completion": _date(status, "primaryCompletionDateStruct"),
        "completion": _date(status, "completionDateStruct"),
        "last_update": _date(status, "lastUpdatePostDateStruct"),
        "lead_sponsor": sponsor.get("leadSponsor", {}).get("name"),
        "lead_sponsor_class": sponsor.get("leadSponsor", {}).get("class"),
        "has_results": raw.get("hasResults", False),
        "brief_summary": ps.get("descriptionModule", {}).get("briefSummary"),
        "mesh_conditions": [
            m.get("term") for m in
            raw.get("derivedSection", {}).get("conditionBrowseModule", {}).get("meshes", [])
        ],
        "mesh_interventions": [
            m.get("term") for m in
            raw.get("derivedSection", {}).get("interventionBrowseModule", {}).get("meshes", [])
        ],
        "protocol_documents": [
            {"label": d.get("label"), "filename": d["filename"], "url": d["url"],
             "type_abbrev": d.get("typeAbbrev"), "document_date": d.get("date"),
             "size_bytes": d.get("size")}
            for d in protocol_docs(raw)
        ],
        "source_record_url": study_page_url(nct),
    }
