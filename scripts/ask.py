"""Ask the Protocol Intelligence agent from the terminal.

  python scripts/ask.py "What are the key exclusion criteria in NCT04175600?"
  python scripts/ask.py --chat          # multi-turn conversation
  python scripts/ask.py --json "..."    # full structured result
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from protocol_intelligence.agent.agent import ProtocolAgent  # noqa: E402


def show(r: dict) -> None:
    print("\n" + r["answer"] + "\n")
    print("Sources:")
    for c in r["citations"]:
        print(f"  [{c['ref']}] {c['label']}\n        {c['source_url']}")
    if r["unknown_refs"]:
        print(f"  WARNING: cited refs no tool returned: {r['unknown_refs']}")
    tools = ", ".join(f"{t['tool']} x{t['calls']}" for t in r["tools_used"])
    print(f"\n({r['latency_s']}s; tools: {tools}; tokens in/out: "
          f"{r['tokens']['input']}/{r['tokens']['output']})")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="?")
    ap.add_argument("--chat", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="print tool calls as they happen")
    args = ap.parse_args()

    bot = ProtocolAgent(verbose=args.verbose)
    if not args.chat:
        r = bot.ask(args.question or input("Question: "))
        print(json.dumps(r, indent=2, ensure_ascii=False)) if args.json else show(r)
        return 0
    while True:
        try:
            q = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if q.lower() in ("", "exit", "quit"):
            return 0
        show(bot.ask(q))


if __name__ == "__main__":
    sys.exit(main())
