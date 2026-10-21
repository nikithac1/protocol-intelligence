"""Download and validate protocol documents; idempotent by file presence + SHA-256."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from protocol_intelligence.config import DATA_DIR
from protocol_intelligence.ingestion.clinicaltrials_client import ClinicalTrialsClient
from protocol_intelligence.ingestion.study_selector import MAX_DOC_BYTES


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_document(client: ClinicalTrialsClient, nct_id: str, doc: dict, out_dir: Path) -> dict:
    """Return a provenance entry describing the outcome; never raises for one bad file."""
    target = out_dir / nct_id / doc["filename"]
    entry = {
        "nct_id": nct_id, "filename": doc["filename"], "label": doc.get("label"),
        "source_url": doc["url"], "local_path": target.relative_to(DATA_DIR).as_posix(),
        "document_date": doc.get("document_date"),
    }
    if (doc.get("size_bytes") or 0) > MAX_DOC_BYTES:
        return {**entry, "status": "skipped", "reason": "exceeds size limit"}
    try:
        if target.exists():
            data = target.read_bytes()
            entry["reused_local_copy"] = True
        else:
            data = client.download(doc["url"])
        if not data.startswith(b"%PDF"):
            return {**entry, "status": "quarantined", "reason": "not a PDF (bad magic bytes)"}
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
        return {**entry, "status": "downloaded", "size_bytes": len(data),
                "sha256": sha256(data),
                "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds")}
    except Exception as e:  # recorded in the manifest rather than aborting the run
        return {**entry, "status": "failed", "reason": f"{type(e).__name__}: {e}"[:300]}
