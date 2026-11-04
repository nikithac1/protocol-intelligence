import pytest

from protocol_intelligence.ingestion import onboard

NCT = "NCT99999999"
OK = {"status": "ok", "pages": 1, "method": "pymupdf+headings", "confidence": "high"}
PDF_CHUNK = {"chunk_id": "pdf", "nct_id": NCT, "source_url": "https://x/a.pdf", "text": "t"}
REGISTRY_CHUNK = {"chunk_id": "registry", "nct_id": NCT}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """onboard.py with its files in tmp_path and no AWS, API or real index behind it."""
    processed = tmp_path / "processed"
    processed.mkdir()
    for name, path in {"DATA_DIR": tmp_path, "DOCS_PATH": processed / "docs.json",
                       "CHUNKS_PATH": processed / "chunks.jsonl",
                       "STUDIES_PATH": processed / "studies.jsonl",
                       "CORPUS_STUDIES": processed / "corpus.jsonl",
                       "DIRTY_FLAG": processed / "dirty"}.items():
        monkeypatch.setattr(onboard, name, path)

    class Client:
        def get_study(self, nct_id):
            return {}

        def api_version(self):
            return "2.0.5"

    added, builds = [], []
    monkeypatch.setattr(onboard, "ClinicalTrialsClient", Client)
    monkeypatch.setattr(onboard, "canonical_record", lambda *a: {"nct_id": NCT})
    monkeypatch.setattr(onboard, "registry_chunks", lambda rec: [dict(REGISTRY_CHUNK)])
    monkeypatch.setattr(onboard.index, "find", lambda *a: None)
    monkeypatch.setattr(onboard.index, "add_chunks", lambda chunks, areas: added.extend(chunks))
    monkeypatch.setattr(onboard.study_repository, "build", lambda *a: builds.append(a))
    monkeypatch.setattr(onboard, "process_pdf", lambda *a, **k: ([dict(PDF_CHUNK)], OK, []))
    return {"added": added, "builds": builds, "root": tmp_path}


def _upload(data=b"%PDF-1 good"):
    return onboard.onboard_pdf(data, filename="a.pdf", nct_id=NCT, document_title="Protocol")


def _pdfs(store):
    return list((store["root"] / "raw" / "uploads").rglob("*.pdf"))


def test_new_study_is_recorded_with_its_chunks(store):
    doc = _upload()
    assert doc["new_study"] and doc["chunks"] == 1 and doc["registry_chunks"] == 1
    assert NCT in onboard._studies()
    assert [c["chunk_id"] for c in store["added"]] == ["registry", "pdf"]
    assert len(store["builds"]) == 1 and len(_pdfs(store)) == 1


def _scanned(*a, **k):
    return [], {"status": "needs_ocr", "pages": 1}, []


def _embedding_failure(chunks, areas):
    raise RuntimeError("Bedrock is down")


@pytest.mark.parametrize("target, attr, failure, error", [
    (onboard, "process_pdf", _scanned, onboard.OnboardError),
    (onboard.index, "add_chunks", _embedding_failure, RuntimeError),
])
def test_failed_upload_leaves_nothing_and_retry_indexes_the_registry(
        store, monkeypatch, target, attr, failure, error):
    with monkeypatch.context() as m:
        m.setattr(target, attr, failure)
        with pytest.raises(error):
            _upload()
    assert NCT not in onboard._studies()
    assert store["builds"] == [] and onboard._read_docs() == [] and _pdfs(store) == []
    assert not onboard.DIRTY_FLAG.exists()

    doc = _upload()
    assert doc["new_study"] and doc["registry_chunks"] == 1
    assert [c["chunk_id"] for c in store["added"]] == ["registry", "pdf"]
