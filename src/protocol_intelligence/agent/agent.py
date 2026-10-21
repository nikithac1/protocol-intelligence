"""Strands agent factory and a single-call `ask()` helper that returns answer + citations."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from botocore.config import Config
from strands import Agent
from strands.models import BedrockModel

from protocol_intelligence.agent.evidence import EvidenceLedger
from protocol_intelligence.agent.prompts import SYSTEM_PROMPT
from protocol_intelligence.agent.tools import make_tools
from protocol_intelligence.config import aws_session, get_settings


def bedrock_model() -> BedrockModel:
    return BedrockModel(
        boto_session=aws_session(),
        boto_client_config=Config(read_timeout=300, retries={"max_attempts": 4, "mode": "adaptive"}),
        model_id=get_settings().bedrock_model_id,
        max_tokens=4096,
    )


@dataclass
class ProtocolAgent:
    """One conversation: the Strands agent plus the evidence ledger its tools write to."""
    ledger: EvidenceLedger = field(default_factory=EvidenceLedger)
    agent: Agent = None
    verbose: bool = False
    session_manager: object = None  # e.g. AgentCore Memory; restores history for this session

    def __post_init__(self):
        self.agent = Agent(model=bedrock_model(), system_prompt=SYSTEM_PROMPT,
                           tools=make_tools(self.ledger), session_manager=self.session_manager,
                           callback_handler=None if not self.verbose else _print_tools)

    def ask(self, question: str) -> dict:
        t0 = time.time()
        before = self._totals()
        result = self.agent(question)
        answer = str(result).strip()
        after = self._totals()  # Strands metrics accumulate over the conversation; report this turn
        tools_used = [{"tool": name, "calls": n - before["tools"].get(name, 0)}
                      for name, n in after["tools"].items() if n > before["tools"].get(name, 0)]
        return {"answer": answer, **self.ledger.resolve(answer), "tools_used": tools_used,
                "latency_s": round(time.time() - t0, 1),
                "tokens": {"input": after["input"] - before["input"],
                           "output": after["output"] - before["output"]}}

    def _totals(self) -> dict:
        m = self.agent.event_loop_metrics
        return {"tools": {name: t.call_count for name, t in m.tool_metrics.items()},
                "input": m.accumulated_usage.get("inputTokens", 0),
                "output": m.accumulated_usage.get("outputTokens", 0)}


def _print_tools(**event):
    msg = event.get("message")
    if msg and msg.get("role") == "assistant":
        for block in msg.get("content", []):
            if "toolUse" in block:
                print(f"  -> {block['toolUse']['name']}({block['toolUse']['input']})", flush=True)
