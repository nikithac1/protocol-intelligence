"""Local test UI + JSON API for the agent.

  python scripts/serve.py            # then open http://127.0.0.1:8000
  POST /api/chat {"session_id": "...", "message": "..."}  -> answer, citations, tools, timing

Admin portal at /admin (onboard new protocol PDFs into the knowledge store). Its /api/admin/*
routes need the x-admin-key header when ADMIN_API_KEY is set in .env.
"""

from __future__ import annotations

import hmac
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from protocol_intelligence.agent.agent import ProtocolAgent
from protocol_intelligence.config import get_settings
from protocol_intelligence.ingestion import onboard

STATIC = Path(__file__).parent / "static"
MAX_SESSIONS = 50

app = FastAPI(title="Protocol Intelligence")
_sessions: dict[str, tuple[ProtocolAgent, threading.Lock]] = {}
_sessions_lock = threading.Lock()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


def _session(session_id: str | None) -> tuple[str, ProtocolAgent, threading.Lock]:
    with _sessions_lock:
        if session_id not in _sessions:
            session_id = session_id or uuid.uuid4().hex
            if len(_sessions) >= MAX_SESSIONS:  # drop the oldest conversation
                _sessions.pop(next(iter(_sessions)))
            _sessions[session_id] = (ProtocolAgent(), threading.Lock())
        agent, lock = _sessions[session_id]
    return session_id, agent, lock


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/info")
def info() -> dict:
    return {"model": get_settings().bedrock_model_id}


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict:  # sync def: FastAPI runs it in a worker thread
    session_id, agent, lock = _session(req.session_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "This conversation is still answering the previous question.")
    try:
        return {"session_id": session_id, **agent.ask(req.message)}
    except Exception as e:  # surface Bedrock/AWS errors to the tester instead of a bare 500
        raise HTTPException(502, f"{type(e).__name__}: {e}") from e
    finally:
        lock.release()


@app.delete("/api/chat/{session_id}")
def reset(session_id: str) -> dict:
    with _sessions_lock:
        _sessions.pop(session_id, None)
    return {"reset": True}


@app.get("/documents/{sha256}.pdf")
def document(sha256: str) -> FileResponse:
    """Citation link target for uploaded PDFs that have no public URL."""
    path = onboard.pdf_path(sha256)
    if not path or not path.exists():
        raise HTTPException(404, "Document not found")
    return FileResponse(path, media_type="application/pdf")


# ---------------------------------------------------------------- admin portal

_jobs: dict[str, dict] = {}
_worker = ThreadPoolExecutor(max_workers=1)  # uploads are indexed one at a time, in order


def require_admin(x_admin_key: str | None = Header(default=None)) -> None:
    expected = os.getenv("ADMIN_API_KEY", "").strip()
    if expected and not hmac.compare_digest(x_admin_key or "", expected):
        raise HTTPException(401, "Missing or wrong admin key")


def _run_job(job: dict, data: bytes, fields: dict) -> None:
    def step(msg: str) -> None:
        job["steps"].append({"at": datetime.now(UTC).isoformat(timespec="seconds"),
                             "message": msg})
    job["status"] = "running"
    try:
        job["document"] = onboard.onboard_pdf(data, step=step, **fields)
        job["status"] = "done"
    except onboard.OnboardError as e:
        job.update(status="failed", error=str(e))
    except Exception as e:  # Bedrock/AWS/PDF errors: show them instead of losing the job
        job.update(status="failed", error=f"{type(e).__name__}: {e}")


@app.get("/admin")
def admin_page() -> FileResponse:
    return FileResponse(STATIC / "admin.html")


@app.get("/api/admin/info", dependencies=[Depends(require_admin)])
def admin_info() -> dict:
    return {"therapeutic_areas": onboard.THERAPEUTIC_AREAS, "max_mb": onboard.MAX_BYTES // 2**20,
            "s3_bucket": get_settings().s3_bucket}


@app.get("/api/admin/documents", dependencies=[Depends(require_admin)])
def admin_documents() -> dict:
    return onboard.list_documents()


@app.post("/api/admin/documents", status_code=202, dependencies=[Depends(require_admin)])
def admin_upload(file: UploadFile = File(...), nct_id: str = Form(...),
                 document_title: str = Form(""), document_version: str = Form(""),
                 source_url: str = Form(""), therapeutic_area: str = Form("")) -> dict:
    data = file.file.read(onboard.MAX_BYTES + 1)
    try:  # reject bad input now rather than as a failed job
        nct_id = onboard.validate(data, nct_id, therapeutic_area or None)
    except onboard.OnboardError as e:
        raise HTTPException(400, str(e)) from e
    if source_url and not source_url.startswith(("https://", "http://")):
        raise HTTPException(400, "Source URL must start with https://")
    job = {"job_id": uuid.uuid4().hex, "status": "queued", "filename": file.filename,
           "nct_id": nct_id, "steps": [], "document": None, "error": None}
    _jobs[job["job_id"]] = job
    _worker.submit(_run_job, job, data, {
        "filename": file.filename or "upload.pdf", "nct_id": nct_id,
        "document_title": document_title, "document_version": document_version,
        "source_url": source_url, "therapeutic_area": therapeutic_area or None})
    return job


@app.get("/api/admin/jobs/{job_id}", dependencies=[Depends(require_admin)])
def admin_job(job_id: str) -> dict:
    if job_id not in _jobs:
        raise HTTPException(404, "Unknown job")
    return _jobs[job_id]


@app.delete("/api/admin/documents/{sha256}", dependencies=[Depends(require_admin)])
def admin_delete(sha256: str) -> dict:
    try:
        return onboard.delete_document(sha256)
    except onboard.OnboardError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/admin/publish", dependencies=[Depends(require_admin)])
def admin_publish() -> dict:
    try:
        return onboard.publish()
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}") from e
