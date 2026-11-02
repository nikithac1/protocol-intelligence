import pytest

from protocol_intelligence.agent.evidence import EvidenceLedger
from protocol_intelligence.agent.tools import make_tools
from protocol_intelligence.knowledge import graph


def _study(nct, area, drugs=(), conditions=(), sponsor="Acme"):
    return {"nct_id": nct, "brief_title": f"Study {nct}", "therapeutic_area": area,
            "phases": ["PHASE2"], "overall_status": "COMPLETED", "enrollment_count": 50,
            "mesh_interventions": list(drugs), "mesh_conditions": list(conditions),
            "lead_sponsor": sponsor}


@pytest.fixture
def local_graph(tmp_path, monkeypatch):
    records = [
        _study("NCT00000001", "oncology", ["Pembrolizumab", "Biopsy"], ["Sarcoma"], "Uni A"),
        _study("NCT00000002", "infectious_disease", ["pembrolizumab"], ["HIV"], "Uni B"),
        _study("NCT00000003", "infectious_disease", ["Metformin", "Biopsy"], ["HIV"], "Uni C"),
        _study("NCT00000004", "diabetes_metabolic", ["metformin"], ["Obesity"], "Uni A"),
        _study("NCT00000005", "diabetes_metabolic", [], ["Obesity"], "Uni D"),
        _study("NCT00000006", "mental_health", [], [], "Uni E"),
    ]
    monkeypatch.setattr(graph, "GRAPH_DIR", tmp_path / "graph")
    graph.write_csv(*graph.build(records), out_dir=tmp_path / "graph")
    yield
    graph._local.cache_clear()


def test_build_filters_procedures_and_merges_case():
    nodes, edges = graph.build([_study("NCT00000001", "oncology", ["Biopsy", "Metformin"]),
                                _study("NCT00000002", "oncology", ["metformin"])])
    assert list(nodes["Intervention"]) == ["intervention:metformin"]
    assert ("study:NCT00000002", "TESTS", "intervention:metformin") in edges


def test_related_studies_lists_shared_links(local_graph):
    rows = graph.run("related_studies", nct_id="nct00000001")["results"]
    assert [(r["nct_id"], r["shared"]) for r in rows] == [
        ("NCT00000002", ["Intervention: pembrolizumab"]),
        ("NCT00000004", ["Sponsor: Uni A"])]  # the shared "Biopsy" procedure is not a link


def test_shared_across_and_within_areas(local_graph):
    rows = graph.run("shared_across_areas", kind="intervention")["results"]
    assert {r["name"]: r["areas"] for r in rows} == {
        "metformin": ["diabetes_metabolic", "infectious_disease"],
        "pembrolizumab": ["infectious_disease", "oncology"]}
    within = graph.run("shared_within_area", therapeutic_area="infectious_disease",
                       kind="condition")["results"]
    assert within == [{"name": "HIV", "studies": ["NCT00000002", "NCT00000003"]}]


def test_connection_finds_shortest_multi_hop_path(local_graph):
    row = graph.run("connection", nct_id="NCT00000001", other_nct_id="NCT00000005")["results"][0]
    assert [step["name"] for step in row["path"]] == [
        "NCT00000001", "Uni A", "NCT00000004", "Obesity", "NCT00000005"]
    assert row["hops"] == 4


def test_unconnected_study_and_bad_input(local_graph):
    out = graph.run("connection", nct_id="NCT00000001", other_nct_id="NCT00000006")
    assert out["results"] == [] and "note" in out
    with pytest.raises(ValueError):
        graph.run("related_studies")
    with pytest.raises(ValueError):
        graph.run("shared_across_areas", kind="drug")


def test_tool_registers_a_citable_ref(local_graph):
    ledger = EvidenceLedger()
    tools = {t.tool_name: t for t in make_tools(ledger)}
    assert "query_trial_graph" in tools
    tools["query_trial_graph"](question_type="studies_linked_to", kind="sponsor", name="uni a")
    resolved = ledger.resolve("Two studies [S1].")
    assert resolved["unknown_refs"] == []
    assert resolved["citations"][0]["label"].startswith("Knowledge graph query")


def test_tool_is_absent_when_graph_is_off(local_graph, monkeypatch):
    monkeypatch.setenv("GRAPH_BACKEND", "off")
    graph.get_settings.cache_clear()
    try:
        assert "query_trial_graph" not in {t.tool_name for t in make_tools(EvidenceLedger())}
    finally:
        monkeypatch.delenv("GRAPH_BACKEND")
        graph.get_settings.cache_clear()
