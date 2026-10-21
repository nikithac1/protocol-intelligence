"""Verify every credential/service the project depends on and print a status table.

Usage:  python scripts/check_access.py [--no-invoke]
--no-invoke skips the two tiny paid model calls (chat + embedding).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from protocol_intelligence import config  # noqa: E402


def _short(err: Exception) -> str:
    msg = str(err)
    return msg.split(" because ")[0][:160]


def check_identity(s):
    ident = config.client("sts").get_caller_identity()
    return f"{ident['Arn']} (account {ident['Account']})"


def check_bedrock_list(s):
    profiles = config.client("bedrock").list_inference_profiles(maxResults=100)
    ids = [p["inferenceProfileId"] for p in profiles["inferenceProfileSummaries"]]
    claude = [i for i in ids if "anthropic" in i]
    configured = "listed" if s.bedrock_model_id in ids else "NOT listed - check BEDROCK_MODEL_ID"
    return f"{len(claude)} Claude inference profiles; {s.bedrock_model_id} {configured}"


def check_bedrock_chat(s):
    resp = config.client("bedrock-runtime").converse(
        modelId=s.bedrock_model_id,
        messages=[{"role": "user", "content": [{"text": "Reply with the single word: OK"}]}],
        inferenceConfig={"maxTokens": 64},
    )
    text = "".join(c.get("text", "") for c in resp["output"]["message"]["content"]).strip()
    auth = "Bedrock API key" if s.has_bedrock_api_key else "IAM credentials"
    return f"{s.bedrock_model_id} replied {text[:20]!r} via {auth}"


def check_bedrock_embed(s):
    resp = config.client("bedrock-runtime").invoke_model(
        modelId=s.bedrock_embedding_model_id,
        body=json.dumps({"inputText": "eligibility criteria"}),
    )
    dims = len(json.loads(resp["body"].read())["embedding"])
    return f"{s.bedrock_embedding_model_id} -> {dims}-dim vector"


def check_agentcore(s):
    runtimes = config.client("bedrock-agentcore-control").list_agent_runtimes()
    return f"{len(runtimes.get('agentRuntimes', []))} agent runtimes visible"


def check_ctgov(s):
    r = httpx.get(f"{s.ctgov_api_base}/studies", params={"pageSize": 1}, timeout=20)
    r.raise_for_status()
    nct = r.json()["studies"][0]["protocolSection"]["identificationModule"]["nctId"]
    return f"API v2 reachable (sample {nct})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-invoke", action="store_true", help="skip paid model calls")
    args = ap.parse_args()

    s = config.get_settings()
    print(f"Profile: {s.aws_profile or '(default chain)'}   Region: {s.aws_region}   "
          f"Bedrock API key: {'set' if s.has_bedrock_api_key else 'not set'}\n")

    checks = [
        ("AWS identity", check_identity),
        ("Bedrock (list models)", check_bedrock_list),
        ("Bedrock chat model", None if args.no_invoke else check_bedrock_chat),
        ("Bedrock embeddings", None if args.no_invoke else check_bedrock_embed),
        ("AgentCore control plane", check_agentcore),
        ("ClinicalTrials.gov", check_ctgov),
    ]
    failures = 0
    for name, fn in checks:
        if fn is None:
            print(f"  SKIP  {name:<26} --no-invoke")
            continue
        try:
            print(f"  OK    {name:<26} {fn(s)}")
        except Exception as e:  # report every failure, keep going
            failures += 1
            print(f"  FAIL  {name:<26} {type(e).__name__}: {_short(e)}")
    print(f"\n{len(checks) - failures} passed / skipped, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
