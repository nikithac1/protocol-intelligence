"""AgentCore Runtime entrypoint (packaged at the zip root by scripts/deploy_agentcore.py).

Request payload:  {"prompt": "...", "user_id": "optional"}
Session:          the caller's runtimeSessionId; turns are persisted to AgentCore Memory.
Response:         same JSON as the local API (answer, citations, unknown_refs, tools, timing).
"""

from __future__ import annotations

import logging
import os
import threading
import time

from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext

from protocol_intelligence import storage
from protocol_intelligence.config import aws_session, get_settings

log = logging.getLogger("protocol_intelligence")
logging.basicConfig(level=logging.INFO)

# Cold start: pull the search index and study database from S3 into DATA_DIR (~50 MB).
_t0 = time.time()
for prefix in ("index/lancedb/", "index/studies.db"):
    storage.download(prefix)
log.info("Index ready in %.1fs", time.time() - _t0)

from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig  # noqa: E402
from bedrock_agentcore.memory.integrations.strands.session_manager import (  # noqa: E402
    AgentCoreMemorySessionManager,
)

from protocol_intelligence.agent.agent import ProtocolAgent  # noqa: E402

app = BedrockAgentCoreApp()
_agents: dict[str, ProtocolAgent] = {}
_lock = threading.Lock()


def _agent_for(session_id: str, user_id: str) -> ProtocolAgent:
    with _lock:
        if session_id not in _agents:
            memory_id = get_settings().agentcore_memory_id
            manager = None
            if memory_id:
                manager = AgentCoreMemorySessionManager(
                    AgentCoreMemoryConfig(memory_id=memory_id, session_id=session_id,
                                          actor_id=user_id,
                                          # earlier tool results carry refs this process no
                                          # longer has in its ledger; restore only the dialogue
                                          filter_restored_tool_context=True),
                    boto_session=aws_session())
            _agents[session_id] = ProtocolAgent(session_manager=manager)
        return _agents[session_id]


@app.entrypoint
def invoke(payload: dict, context: RequestContext) -> dict:
    prompt = (payload or {}).get("prompt", "").strip()
    if not prompt:
        return {"error": "Send {\"prompt\": \"your question\"}"}
    user_id = str(payload.get("user_id") or "anonymous")[:64]
    session_id = context.session_id or "default-session-000000000000000000"
    result = _agent_for(session_id, user_id).ask(prompt)
    return {"session_id": session_id, **result}


if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", "8080")))
