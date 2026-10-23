"""Onboard a new protocol PDF into the knowledge store (admin portal back end).

upload -> validate -> save PDF -> (new study? fetch its ClinicalTrials.gov record) -> extract,
sectionize, chunk -> embed with Titan -> append to LanceDB -> optionally publish index/ to S3.

Uploads are kept apart from the batch corpus so a full rebuild keeps them:
  raw/uploads/<NCT>/<sha256>.pdf        the original file
  processed/uploaded_documents.json     one entry per uploaded document
  processed/uploaded_chunks.jsonl       its chunks (scripts/build_index.py reads this too)
  processed/uploaded_studies.jsonl      registry records of studies not in the batch corpus
Publishing copies index/ to S3 (what AgentCore loads) and backs up the upload files.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

from protocol_intelligence import storage
from protocol_intelligence.config import DATA_DIR
from protocol_intelligence.ingestion.clinicaltrials_client import ClinicalTrialsClient
from protocol_intelligence.ingestion.normalize import canonical_record
from protocol_intelligence.knowledge import study_repository
from protocol_intelligence.processing.pipeline import process_pdf
from protocol_intelligence.processing.registry_chunks import registry_chunks
from protocol_intelligence.retrieval import index

MAX_BYTES = 50 * 1024 * 1024
NCT_RE = re.compile(r"^NCT\d{8}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
THERAPEUTIC_AREAS = ["oncology", "cardiovascular", "diabetes_metabolic", "infectious_disease",
                     "mental_health", "other"]

PROCESSED = DATA_DIR / "processed"
UPLOAD_DIR = DATA_DIR / "raw" / "uploads"
DOCS_PATH = PROCESSED / "uploaded_documents.json"
CHUNKS_PATH = PROCESSED / "uploaded_chunks.jsonl"
STUDIES_PATH = PROCESSED / "uploaded_studies.jsonl"
CORPUS_STUDIES = PROCESSED / "studies.jsonl"
DIRTY_FLAG = PROCESSED / "uploads_unpublished"  # exists while local index/ differs from S3

_write_lock = threading.Lock()  # one writer at a time for the index and the JSON files


class OnboardError(ValueError):
    """Bad input; the message is safe to show the admin."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_docs() -> list[dict]:
    return json.loads(DOCS_PATH.read_text(encoding="utf-8")) if DOCS_PATH.exists() else []


def _write_docs(docs: list[dict]) -> None:
    DOCS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOCS_PATH.write_text(json.dumps(docs, indent=1), encoding="utf-8")


def _rewrite_jsonl(path: Path, keep) -> None:
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows if keep(r)),
                    encoding="utf-8")


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _studies() -> dict[str, dict]:
    return {r["nct_id"]: r for r in study_repository.read_records(CORPUS_STUDIES, STUDIES_PATH)}


def pdf_path(sha256: str) -> Path | None:
    """Local file for an uploaded document (served as its citation link)."""
    if SHA_RE.match(sha256):
        doc = next((d for d in _read_docs() if d["sha256"] == sha256), None)
        if doc:
            return DATA_DIR / doc["local_path"]
    return None


def validate(data: bytes, nct_id: str, therapeutic_area: str | None) -> str:
    """Checks a request before it is queued; returns the normalised NCT ID."""
    nct_id = nct_id.strip().upper()
    if not NCT_RE.match(nct_id):
        raise OnboardError("NCT ID must look like NCT01234567.")
    if not data.startswith(b"%PDF-"):
        raise OnboardError("The file is not a PDF.")
    if len(data) > MAX_BYTES:
        raise OnboardError(f"The file is larger than {MAX_BYTES // 2**20} MB.")
    if therapeutic_area and therapeutic_area not in THERAPEUTIC_AREAS:
        raise OnboardError(f"Therapeutic area must be one of {', '.join(THERAPEUTIC_AREAS)}.")
    _check_duplicate(hashlib.sha256(data).hexdigest())
    return nct_id


def _check_duplicate(sha: str) -> None:
    # One copy per file: a SHA-256 then identifies exactly one document in the index.
    if any(d["sha256"] == sha for d in _read_docs()):
        raise OnboardError("This exact file has already been uploaded.")
    if hit := index.find("source_sha256", sha):
        raise OnboardError(f"This exact file is already in the knowledge store "
                           f"({hit['nct_id']}, {hit['document_title']}).")


def onboard_pdf(data: bytes, *, filename: str, nct_id: str, document_title: str,
                document_version: str = "", source_url: str = "",
                therapeutic_area: str | None = None, step=lambda msg: None) -> dict:
    """Index one PDF. `step` receives progress messages. Returns the document entry."""
    nct_id = validate(data, nct_id, therapeutic_area)
    sha = hashlib.sha256(data).hexdigest()
    with _write_lock:
        _check_duplicate(sha)  # again under the lock: two identical uploads may be queued

        step("Saving the PDF")
        rel = Path("raw") / "uploads" / nct_id / f"{sha}.pdf"
        (DATA_DIR / rel).parent.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / rel).write_bytes(data)

        studies = _studies()
        new_chunks: list[dict] = []
        new_study = nct_id not in studies
        if new_study:
            step(f"{nct_id} is not in the corpus: fetching its ClinicalTrials.gov record")
            ct = ClinicalTrialsClient()
            try:
                record = canonical_record(ct.get_study(nct_id), ct.api_version(), _now())
            except Exception as e:  # unknown ID, network error: still index the PDF
                step(f"Registry record unavailable ({type(e).__name__}); indexing the PDF only")
                record = None
            if record:
                record["therapeutic_area"] = therapeutic_area or "other"
                _append_jsonl(STUDIES_PATH, [record])
                study_repository.build(CORPUS_STUDIES, STUDIES_PATH)
                studies[nct_id] = record
                new_chunks += registry_chunks(record)

        step("Extracting text and splitting it into protocol sections")
        doc_key = f"upload_{sha[:12]}"
        title = document_title.strip() or Path(filename).stem
        chunks, status, _ = process_pdf(
            DATA_DIR / rel, nct_id=nct_id, doc_key=doc_key, document_title=title,
            document_version=document_version.strip() or _now()[:10],
            source_url=source_url.strip() or f"/documents/{sha}.pdf", sha256=sha)
        if status["status"] == "needs_ocr":
            (DATA_DIR / rel).unlink(missing_ok=True)
            raise OnboardError("No text layer found (scanned PDF). Run OCR on it and upload again.")
        new_chunks += chunks

        step(f"Embedding {len(new_chunks)} chunks with Titan and adding them to the index")
        areas = {n: r.get("therapeutic_area") or "" for n, r in studies.items()}
        if nct_id not in studies:  # registry record unavailable
            areas[nct_id] = therapeutic_area or "other"
        index.add_chunks(new_chunks, areas)
        _append_jsonl(CHUNKS_PATH, new_chunks)
        DIRTY_FLAG.touch()

        doc = {"sha256": sha, "nct_id": nct_id, "document_title": title, "filename": filename,
               "local_path": rel.as_posix(), "source_url": chunks[0]["source_url"] if chunks else "",
               "size_bytes": len(data), "pages": status["pages"], "chunks": len(chunks),
               "registry_chunks": len(new_chunks) - len(chunks), "new_study": new_study,
               "extraction_method": status["method"], "extraction_confidence": status["confidence"],
               "uploaded_at": _now(), "published": False}
        _write_docs([*_read_docs(), doc])
        step("Done")
        return doc


def delete_document(sha256: str) -> dict:
    """Remove an uploaded document's chunks (and its study, if the upload added it)."""
    if not SHA_RE.match(sha256):
        raise OnboardError("Invalid document id.")
    with _write_lock:
        docs = _read_docs()
        doc = next((d for d in docs if d["sha256"] == sha256), None)
        if not doc:
            raise OnboardError("Only uploaded documents can be removed here.")
        nct = doc["nct_id"]
        removed = index.delete_where(source_sha256=sha256, nct_id=nct)
        _rewrite_jsonl(CHUNKS_PATH, lambda c: c["source_sha256"] != sha256)
        docs = [d for d in docs if d["sha256"] != sha256]
        if doc["new_study"] and not any(d["nct_id"] == nct for d in docs):
            removed += index.delete_where(nct_id=nct)  # its registry chunks
            _rewrite_jsonl(CHUNKS_PATH, lambda c: c["nct_id"] != nct)
            _rewrite_jsonl(STUDIES_PATH, lambda r: r["nct_id"] != nct)
            study_repository.build(CORPUS_STUDIES, STUDIES_PATH)
        _write_docs(docs)
        (DATA_DIR / doc["local_path"]).unlink(missing_ok=True)
        DIRTY_FLAG.touch()
        return {"deleted": sha256, "chunks_removed": removed}


def list_documents() -> dict:
    """Every protocol document in the index, with uploads marked."""
    uploads = {d["sha256"]: d for d in _read_docs()}
    table = index.open_table()
    rows = (table.search().where("source_type = 'protocol_pdf'")
            .select(["nct_id", "document_title", "source_sha256", "source_url", "page_end"])
            .limit(table.count_rows()).to_list())
    docs: dict[str, dict] = {}
    for r in rows:
        d = docs.setdefault(r["source_sha256"], {
            "sha256": r["source_sha256"], "nct_id": r["nct_id"], "document_title": r["document_title"],
            "source_url": r["source_url"], "chunks": 0, "pages": 0, "uploaded": False})
        d["chunks"] += 1
        d["pages"] = max(d["pages"], r["page_end"] or 0)
    for sha, u in uploads.items():
        docs.setdefault(sha, {"sha256": sha, "nct_id": u["nct_id"], "chunks": 0,
                              "document_title": u["document_title"], "source_url": u["source_url"]})
        docs[sha].update(uploaded=True, uploaded_at=u["uploaded_at"], published=u["published"],
                         pages=u["pages"], new_study=u["new_study"],
                         extraction_confidence=u["extraction_confidence"])
    uploaded = sorted((d for d in docs.values() if d["uploaded"]),
                      key=lambda d: d["uploaded_at"], reverse=True)
    corpus = sorted((d for d in docs.values() if not d["uploaded"]), key=lambda d: d["nct_id"])
    return {"documents": uploaded + corpus, "studies": len(_studies()),
            "chunks": table.count_rows(), "unpublished_changes": DIRTY_FLAG.exists()}


def publish() -> dict:
    """Upload index/ to S3; new AgentCore sessions then load the updated knowledge store."""
    with _write_lock:
        stats = storage.upload("index")
        for prefix in ("raw/uploads", *(f"processed/{p.name}" for p in
                                        (DOCS_PATH, CHUNKS_PATH, STUDIES_PATH) if p.exists())):
            if (DATA_DIR / prefix).exists():
                storage.upload(prefix)  # backup only; the runtime reads index/
        DIRTY_FLAG.unlink(missing_ok=True)
        docs = _read_docs()
        for d in docs:
            d["published"] = True
        _write_docs(docs)
        return stats
