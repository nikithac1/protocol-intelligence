"""Call the deployed agent on AgentCore Runtime.

  python scripts/invoke_agentcore.py "How many studies are recruiting?"
  python scripts/invoke_agentcore.py --session <id> "Which of those are oncology?"   # follow-up
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from botocore.config import Config  # noqa: E402

from ask import show  # noqa: E402
from protocol_intelligence.config import aws_session, get_settings  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--session", help="reuse a session id for follow-up questions")
    ap.add_argument("--user", default="tester")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    arn = get_settings().agentcore_runtime_arn
    if not arn:
        print("AGENTCORE_RUNTIME_ARN is not set; run scripts/deploy_agentcore.py first")
        return 1
    session = args.session or f"session-{uuid.uuid4().hex}"  # AgentCore needs >= 33 chars
    rt = aws_session().client("bedrock-agentcore", config=Config(read_timeout=300))
    resp = rt.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=session,
                                   payload=json.dumps({"prompt": args.question,
                                                       "user_id": args.user}))
    result = json.loads(resp["response"].read())
    if args.json or "answer" not in result:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        show(result)
    print(f"\nsession: {session}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
