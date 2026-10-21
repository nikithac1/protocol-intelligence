"""Local test UI + JSON API for the agent.

  python scripts/serve.py            # then open http://127.0.0.1:8000
  POST /api/chat {"session_id": "...", "message": "..."}  -> answer, citations, tools, timing
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from protocol_intelligence.agent.agent import ProtocolAgent
from protocol_intelligence.config import get_settings

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
