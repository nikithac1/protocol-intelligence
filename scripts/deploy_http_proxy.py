"""Deploy a public HTTPS endpoint for the agent (Lambda Function URL + API key). Safe to re-run.

  python scripts/deploy_http_proxy.py
  python scripts/deploy_http_proxy.py --rotate-key

Callers need no AWS setup -- just the URL and key (both saved to .env as PROXY_URL / PROXY_API_KEY):
  curl -X POST "$PROXY_URL" -H "x-api-key: $PROXY_API_KEY" -H "Content-Type: application/json" \
       -d '{"prompt": "How many studies are recruiting?"}'
Requires the runtime from scripts/deploy_agentcore.py (AGENTCORE_RUNTIME_ARN).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import secrets
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from botocore.exceptions import ClientError  # noqa: E402

from protocol_intelligence.config import PROJECT_ROOT, client, get_settings  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from setup_s3 import save_env  # noqa: E402

FUNCTION = "protocol-intelligence-http-proxy"
ROLE_NAME = "ProtocolIntelligenceHttpProxyRole"
HANDLER_FILE = PROJECT_ROOT / "deploy" / "http_proxy_lambda.py"


def ensure_role(runtime_arn: str) -> str:
    iam = client("iam")
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole"}]}
    policy = {"Version": "2012-10-17", "Statement": [
        {"Sid": "InvokeAgent", "Effect": "Allow", "Action": "bedrock-agentcore:InvokeAgentRuntime",
         "Resource": [runtime_arn, f"{runtime_arn}/*"]}]}
    try:
        arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
        created = False
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        arn = iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust),
                              Description="Lambda HTTP proxy for the Protocol Intelligence agent",
                              Tags=[{"Key": "Project", "Value": "ProtocolIntelligence"}])["Role"]["Arn"]
        created = True
    iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn=(
        "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"))
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="invoke-agent",
                        PolicyDocument=json.dumps(policy))
    print(f"Proxy role {'created' if created else 'updated'}: {arn}")
    return arn


def package() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(HANDLER_FILE, "http_proxy_lambda.py")
    return buf.getvalue()


def ensure_function(lam, role_arn: str, env: dict) -> None:
    code = package()
    config = dict(Runtime="python3.12", Handler="http_proxy_lambda.handler", Role=role_arn,
                  Timeout=300, MemorySize=256, Environment={"Variables": env})
    try:
        lam.get_function(FunctionName=FUNCTION)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        print(f"Creating function {FUNCTION}")
        for attempt in range(12):  # a new role takes a few seconds before Lambda can assume it
            try:
                lam.create_function(FunctionName=FUNCTION, Code={"ZipFile": code},
                                    Architectures=["arm64"],
                                    Tags={"Project": "ProtocolIntelligence"}, **config)
                break
            except ClientError as err:
                if "cannot be assumed" not in str(err) or attempt == 11:
                    raise
                time.sleep(5)
    else:
        print(f"Updating function {FUNCTION}")
        lam.update_function_code(FunctionName=FUNCTION, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
        lam.update_function_configuration(FunctionName=FUNCTION, **config)
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)


def ensure_url(lam) -> str:
    cors = {"AllowOrigins": ["*"], "AllowMethods": ["POST"],
            "AllowHeaders": ["content-type", "x-api-key", "x-session-id"], "MaxAge": 86400}
    try:
        url = lam.update_function_url_config(FunctionName=FUNCTION, AuthType="NONE",
                                             Cors=cors)["FunctionUrl"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        url = lam.create_function_url_config(FunctionName=FUNCTION, AuthType="NONE",
                                             InvokeMode="BUFFERED", Cors=cors)["FunctionUrl"]
    # Public URL: both permissions are required; the API key is checked inside the function.
    for sid, extra in [("public-url", {"Action": "lambda:InvokeFunctionUrl",
                                       "FunctionUrlAuthType": "NONE"}),
                       ("public-url-invoke", {"Action": "lambda:InvokeFunction",
                                              "InvokedViaFunctionUrl": True})]:
        try:
            lam.add_permission(FunctionName=FUNCTION, StatementId=sid, Principal="*", **extra)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceConflictException":
                raise
    return url


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rotate-key", action="store_true", help="issue a new API key")
    args = ap.parse_args()

    settings = get_settings()
    if not settings.agentcore_runtime_arn:
        print("AGENTCORE_RUNTIME_ARN is not set; run scripts/deploy_agentcore.py first.")
        return 1
    key = os.environ.get("PROXY_API_KEY")
    if args.rotate_key or not key:
        key = secrets.token_urlsafe(32)

    lam = client("lambda")
    role_arn = ensure_role(settings.agentcore_runtime_arn)
    ensure_function(lam, role_arn, {"AGENTCORE_RUNTIME_ARN": settings.agentcore_runtime_arn,
                                    "PROXY_API_KEY": key})
    url = ensure_url(lam)
    save_env("PROXY_URL", url)
    save_env("PROXY_API_KEY", key)
    print(f"\nEndpoint: {url}\nAPI key and URL saved to .env (PROXY_URL, PROXY_API_KEY)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
