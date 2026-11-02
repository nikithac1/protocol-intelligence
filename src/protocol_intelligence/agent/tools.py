"""Agent tools (plan section 7.1). Each tool is bound to one EvidenceLedger per conversation."""

from __future__ import annotations

import json
import re

from strands import tool

from protocol_intelligence.agent.evidence import EvidenceLedger, citation_label
from protocol_intelligence.knowledge import graph, study_repository
from protocol_intelligence.retrieval import hybrid
from protocol_intelligence.retrieval.index import open_table

SECTION_TYPES = ["synopsis", "eligibility", "endpoints_outcomes", "objectives", "statistics",
                 "safety_adverse_events", "treatment_dosing", "schedule_procedures", "study_design",
                 "withdrawal_discontinuation", "background_rationale", "biomarkers_correlative",
                 "ethics_regulatory", "abbreviations", "references", "other"]
THERAPEUTIC_AREAS = ["oncology", "diabetes_metabolic", "cardiovascular", "infectious_disease",
                     "mental_health"]
_CHUNK_ID = re.compile(r"^NCT\d{8}\|[a-z0-9_]+\|[a-z0-9-]+\|p\d+\|\d{4}$")
_PASSAGE_COLS = ["chunk_id", "nct_id", "source_type", "document_title", "section_path",
                 "section_type", "page_start", "page_end", "source_url", "text",
                 "extraction_confidence"]


def _as_list(v) -> list[str] | None:
    if v in (None, "", []):
        return None
    return [v] if isinstance(v, str) else list(v)


def _passage_out(ledger: EvidenceLedger, p: dict) -> dict:
    return {"ref": ledger.register(p), "cite_as": citation_label(p), "nct_id": p["nct_id"],
            "section_type": p["section_type"], "extraction_confidence": p["extraction_confidence"],
            "text": p["text"]}


def make_tools(ledger: EvidenceLedger) -> list:
    @tool
    def search_protocols(query: str, nct_ids: list[str] | None = None,
                         section_types: list[str] | None = None,
                         therapeutic_areas: list[str] | None = None,
                         source: str = "any", k: int = 8) -> str:
        """Search protocol documents and registry records (hybrid keyword + semantic search).

        Use for any question whose answer is in protocol text: eligibility, dosing, schedules,
        endpoints, safety rules, statistics, rationale. Every passage returned has a `ref`
        (e.g. S4) that must be cited in the answer as [S4].

        Args:
            query: Natural-language search text; include the key clinical terms.
            nct_ids: Restrict to these studies, e.g. ["NCT04175600"].
            section_types: Restrict to section types. One or more of: synopsis, eligibility,
                endpoints_outcomes, objectives, statistics, safety_adverse_events,
                treatment_dosing, schedule_procedures, study_design, withdrawal_discontinuation,
                background_rationale, biomarkers_correlative, ethics_regulatory, abbreviations,
                references, other. Leave empty when unsure (sections are auto-classified).
            therapeutic_areas: Restrict to areas: oncology, diabetes_metabolic, cardiovascular,
                infectious_disease, mental_health.
            source: "protocol" (PDF text only), "registry" (ClinicalTrials.gov record only),
                or "any".
            k: Number of passages, 1-15.
        """
        source_types = {"protocol": ["protocol_pdf"], "registry": ["registry"]}.get(source)
        try:
            hits = hybrid.search(query, k=max(1, min(int(k), 15)), nct_ids=_as_list(nct_ids),
                                 section_types=_as_list(section_types), source_types=source_types,
                                 therapeutic_areas=_as_list(therapeutic_areas))
        except ValueError as e:
            return json.dumps({"error": str(e)})
        if not hits:
            return json.dumps({"passages": [], "note": "No matching passages. Try broader terms "
                               "or remove filters before concluding the corpus lacks this."})
        return json.dumps({"passages": [_passage_out(ledger, h) for h in hits]})

    @tool
    def get_source_passage(chunk_ref: str, neighbors: int = 1) -> str:
        """Read a cited passage with the text immediately before and after it in the same document.

        Use when a search hit is cut off mid-list or mid-table, or you need surrounding context
        (e.g. the rest of an exclusion list). Neighbouring passages get their own refs.

        Args:
            chunk_ref: A ref returned earlier (e.g. "S3").
            neighbors: Passages to include on each side, 0-3.
        """
        p = ledger.by_ref.get(chunk_ref.strip("[] "))
        if not p:
            return json.dumps({"error": f"Unknown ref {chunk_ref}. Use a ref from a previous result."})
        cid = p["chunk_id"]
        if not _CHUNK_ID.match(cid):
            return json.dumps({"error": "This ref is structured metadata; there is no surrounding text."})
        nct, doc, _, _, seq = cid.split("|")
        n = max(0, min(int(neighbors), 3))
        rows = (open_table().search().where(f"nct_id = '{nct}'").select(_PASSAGE_COLS)
                .limit(5000).to_list())
        same_doc = sorted((r for r in rows if r["chunk_id"].split("|")[1] == doc),
                          key=lambda r: int(r["chunk_id"].rsplit("|", 1)[1]))
        idx = next(i for i, r in enumerate(same_doc) if r["chunk_id"] == cid)
        window = same_doc[max(0, idx - n): idx + n + 1]
        return json.dumps({"passages": [_passage_out(ledger, r) for r in window]})

    @tool
    def get_study_metadata(nct_ids: list[str]) -> str:
        """Structured registry facts for specific studies: title, phase, status, enrollment,
        sponsor, dates, age/sex limits, conditions, interventions, primary/secondary outcomes.

        Each study comes with a `ref` to cite as [Sn] (it points to the ClinicalTrials.gov record).

        Args:
            nct_ids: One or more NCT ids, e.g. ["NCT05488366"]. Max 10.
        """
        out = []
        for rec in study_repository.get_studies([n.upper() for n in _as_list(nct_ids)[:10]]):
            if "error" not in rec:
                rec["ref"] = ledger.register({
                    "chunk_id": f"{rec['nct_id']}|registry-metadata", "nct_id": rec["nct_id"],
                    "source_type": "registry", "document_title": "ClinicalTrials.gov registry record",
                    "section_path": "Registry record > Structured metadata", "section_type": "synopsis",
                    "page_start": None, "page_end": None, "source_url": rec["source_record_url"],
                    "text": f"{rec['brief_title']} ({rec['phase']}, {rec['overall_status']})",
                    "extraction_confidence": "high"})
            out.append(rec)
        return json.dumps({"studies": out}, default=str)

    @tool
    def query_study_cohort(filters: dict | None = None, group_by: str | None = None,
                           limit: int = 25) -> str:
        """Exact counts, lists and breakdowns across the 100-study corpus, computed by the
        database, not estimated. Use for "how many", "which studies", "compare by phase" questions.

        The result has a `ref` to cite as [Sn] after any figure taken from it.

        Args:
            filters: Any of: phase ("PHASE1".."PHASE4", "EARLY_PHASE1"), overall_status
                (COMPLETED, TERMINATED, ACTIVE_NOT_RECRUITING, RECRUITING), therapeutic_area,
                condition (substring), intervention (substring), intervention_type (DRUG,
                BIOLOGICAL, DEVICE, BEHAVIORAL, ...), lead_sponsor_class (INDUSTRY, OTHER, NIH,
                ...), allocation, primary_purpose, sex, nct_ids, min_enrollment, max_enrollment,
                start_year_from, start_year_to, accepts_age (years), has_results,
                healthy_volunteers.
            group_by: Optional breakdown: phase, overall_status, therapeutic_area,
                lead_sponsor_class, allocation, primary_purpose, start_year, sex, has_results,
                intervention_type.
            limit: Max studies to list (when not grouping), 1-100.
        """
        try:
            result = study_repository.query_cohort(filters or {}, group_by,
                                                   limit=max(1, min(int(limit), 100)))
        except ValueError as e:
            return json.dumps({"error": str(e)})
        spec = json.dumps({"filters": filters or {}, "group_by": group_by}, sort_keys=True)
        result["ref"] = ledger.register({
            "chunk_id": f"cohort|{spec}", "nct_id": "", "source_type": "registry_query",
            "document_title": "Study database (ClinicalTrials.gov registry records)",
            "section_path": f"Registry query over the 100 indexed studies: {spec}",
            "section_type": "", "page_start": None, "page_end": None,
            "source_url": "https://clinicaltrials.gov", "text": f"{result['total_studies']} studies",
            "extraction_confidence": "high"})
        return json.dumps(result, default=str)

    @tool
    def query_trial_graph(question_type: str, nct_id: str | None = None,
                          other_nct_id: str | None = None, kind: str | None = None,
                          name: str | None = None, therapeutic_area: str | None = None,
                          min_areas: int = 2, limit: int = 10) -> str:
        """Relationships between studies from the knowledge graph: studies linked through a shared
        intervention (MeSH drug or treatment), condition or lead sponsor. Use for "which studies
        are related to X", "what links the diabetes studies", "which drugs appear in more than one
        area", "how is study A connected to study B". Use search_protocols for protocol text.

        The result has a `ref` to cite as [Sn] after any fact taken from it.

        Args:
            question_type: One of:
                related_studies (nct_id): other studies sharing an intervention, condition or
                    sponsor, most shared links first.
                shared_across_areas (kind, min_areas): interventions, conditions or sponsors whose
                    studies span several therapeutic areas.
                shared_within_area (therapeutic_area, kind): what two or more studies of one area
                    have in common.
                studies_linked_to (kind, name): studies with an intervention, condition or sponsor
                    whose name contains `name`.
                connection (nct_id, other_nct_id): shortest chain of shared links between two
                    studies, up to 6 hops.
            nct_id: e.g. "NCT05488366".
            other_nct_id: The second study, for connection.
            kind: "intervention", "condition" or "sponsor".
            name: Text to match for studies_linked_to, e.g. "metformin" or "Yale".
            therapeutic_area: oncology, diabetes_metabolic, cardiovascular, infectious_disease or
                mental_health.
            min_areas: For shared_across_areas: minimum number of areas, 2-5.
            limit: Max rows, 1-50.
        """
        try:
            result = graph.run(question_type, nct_id=nct_id, other_nct_id=other_nct_id, kind=kind,
                               name=name, therapeutic_area=therapeutic_area,
                               min_areas=max(2, min(int(min_areas), 5)),
                               limit=max(1, min(int(limit), 50)))
        except (ValueError, graph.GraphUnavailable) as e:
            return json.dumps({"error": str(e)})
        spec = json.dumps({"question_type": result["question_type"], **result["args"]},
                          sort_keys=True)
        result["ref"] = ledger.register({
            "chunk_id": f"graph|{spec}", "nct_id": "", "source_type": "graph",
            "document_title": "Knowledge graph (ClinicalTrials.gov registry links)",
            "section_path": f"Knowledge graph query over the indexed studies: {spec}",
            "section_type": "", "page_start": None, "page_end": None,
            "source_url": "https://clinicaltrials.gov", "text": f"{len(result['results'])} results",
            "extraction_confidence": "high"})
        return json.dumps(result, default=str)

    tools = [search_protocols, get_source_passage, get_study_metadata, query_study_cohort]
    if graph.enabled():  # GRAPH_BACKEND=off, or no graph built yet: the agent runs without it
        tools.append(query_trial_graph)
    return tools
