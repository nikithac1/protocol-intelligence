"""Public HTTPS front door for the AgentCore runtime (Lambda Function URL, API-key protected).

Callers need no AWS credentials:
  POST <function url>   x-api-key: <key>   {"prompt": "...", "session_id": "optional, >= 33 chars"}
The function signs the call to AgentCore with its own role and returns the agent's JSON
(which includes "session_id" -- send it back to ask follow-up questions).
"""

import base64
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REGION = os.environ["AWS_REGION"]
API_KEY = os.environ["PROXY_API_KEY"]
RUNTIME_URL = (f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/"
               f"{urllib.parse.quote(os.environ['AGENTCORE_RUNTIME_ARN'], safe='')}/invocations")
SESSION = boto3.Session()


def _reply(status: int, body) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": body if isinstance(body, str) else json.dumps(body)}


def handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not hmac.compare_digest(headers.get("x-api-key", ""), API_KEY):
        return _reply(401, {"error": "missing or invalid x-api-key header"})

    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        body = json.loads(raw)
    except ValueError:
        return _reply(400, {"error": "body must be JSON"})
    prompt = body.get("prompt") if isinstance(body, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        return _reply(400, {"error": "'prompt' is required"})

    session_id = body.get("session_id") or headers.get("x-session-id") or f"session-{uuid.uuid4().hex}"
    if len(session_id) < 33:
        return _reply(400, {"error": "session_id must be at least 33 characters"})

    payload = json.dumps({"prompt": prompt, "user_id": body.get("user_id")}).encode("utf-8")
    signed = AWSRequest("POST", RUNTIME_URL, data=payload, headers={
        "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id})
    SigV4Auth(SESSION.get_credentials().get_frozen_credentials(), "bedrock-agentcore",
              REGION).add_auth(signed)
    req = urllib.request.Request(RUNTIME_URL, data=payload, headers=dict(signed.headers),
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=290) as r:
            return _reply(r.status, r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return _reply(e.code, {"error": e.read().decode("utf-8", "replace")[:2000]})
