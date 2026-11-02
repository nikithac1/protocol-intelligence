"""Ask the agent one question and print every message exchanged with the model:
the tool calls Claude chose, what each tool returned, the final answer and its citations.

  python scripts/demo_trace.py "How many oncology studies are recruiting?"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from protocol_intelligence.agent.agent import ProtocolAgent


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="?", default="How many oncology studies are recruiting?")
    ap.add_argument("--chars", type=int, default=400, help="characters of each tool result to show")
    args = ap.parse_args()

    bot = ProtocolAgent()
    r = bot.ask(args.question)
    for i, m in enumerate(bot.agent.messages, 1):
        for block in m["content"]:
            kind = next(iter(block))
            body = block[kind]
            if kind == "toolUse":
                body = json.dumps({"name": body["name"], "input": body["input"]}, indent=1)
            elif kind == "toolResult":
                body = str(body["content"])[:args.chars] + " ..."
            print(f"\n--- {i}. {m['role']} / {kind}\n{body}")

    print("\n=== citations")
    for c in r["citations"]:
        print(f"  [{c['ref']}] {c['label']}")
    print(f"unknown_refs: {r['unknown_refs']}")
    tools = ", ".join(f"{t['tool']} x{t['calls']}" for t in r["tools_used"])
    print(f"tools: {tools} | {r['latency_s']}s | tokens in/out: "
          f"{r['tokens']['input']}/{r['tokens']['output']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
