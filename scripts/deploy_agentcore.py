"""Deploy the agent to AgentCore Runtime (code/zip deployment, no Docker needed). Safe to re-run.

  python scripts/deploy_agentcore.py              # memory + role + package + create/update runtime
  python scripts/deploy_agentcore.py --rebuild-deps

Steps:
  1. AgentCore Memory (short-term conversation events, 30-day expiry)  -> AGENTCORE_MEMORY_ID
  2. IAM execution role with least-privilege access (Bedrock, Memory, S3 read, logs/traces)
  3. Zip: Linux ARM64 dependencies + src/protocol_intelligence + deploy/agentcore_app.py
  4. Upload zip to s3://<bucket>/deploy/agentcore/, create or update the runtime, wait for READY
                                                                        -> AGENTCORE_RUNTIME_ARN
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from botocore.exceptions import ClientError  # noqa: E402

from protocol_intelligence.config import PROJECT_ROOT, client, get_settings  # noqa: E402
from protocol_intelligence.storage import bucket  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from setup_s3 import save_env  # noqa: E402

NAME = "protocol_intelligence"
MEMORY_NAME = f"{NAME}_memory"
ROLE_NAME = "ProtocolIntelligenceAgentCoreRuntimeRole"
BUILD = PROJECT_ROOT / "build" / "agentcore"
REQUIREMENTS = PROJECT_ROOT / "deploy" / "requirements-runtime.txt"
ENTRYPOINT = PROJECT_ROOT / "deploy" / "agentcore_app.py"
PACKAGE_SRC = PROJECT_ROOT / "src" / "protocol_intelligence"
EXCLUDE_DIRS = {"__pycache__", "api", "ingestion", "processing"}  # not needed at runtime
PYTHON = "3.12"
MAX_ZIP_MB = 250


def wait(describe, done: set[str], failed: set[str], what: str, timeout: int = 900) -> dict:
    t0 = time.time()
    while True:
        info = describe()
        status = info["status"]
        if status in done:
            return info
        if status in failed:
            raise RuntimeError(f"{what} failed: {info.get('failureReason', status)}")
        if time.time() - t0 > timeout:
            raise TimeoutError(f"{what} still {status} after {timeout}s")
        print(f"  {what}: {status}...", flush=True)
        time.sleep(10)


# ------------------------------------------------------------------ 1. memory
def ensure_memory(ctl) -> str:
    existing = get_settings().agentcore_memory_id
    for page in ctl.get_paginator("list_memories").paginate():
        for m in page["memories"]:
            if m["id"] == existing or m["id"].startswith(f"{MEMORY_NAME}-"):
                if m["status"] in ("ACTIVE", "CREATING", "UPDATING"):
                    print(f"Memory exists: {m['id']} ({m['status']})")
                    return m["id"]
    print(f"Creating AgentCore Memory {MEMORY_NAME} (short-term, 30-day expiry)")
    mem = ctl.create_memory(name=MEMORY_NAME, eventExpiryDuration=30,
                            description="Protocol Intelligence conversation history")["memory"]
    return mem["id"]


# ------------------------------------------------------------------ 2. role
def ensure_role(account: str, region: str, memory_id: str, data_bucket: str) -> str:
    iam = client("iam")
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
        "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": account},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account}:*"}}}]}
    logs = f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*"
    policy = {"Version": "2012-10-17", "Statement": [
        {"Sid": "Logs", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:DescribeLogStreams", "logs:CreateLogStream",
                    "logs:PutLogEvents"], "Resource": [logs, f"{logs}:log-stream:*"]},
        {"Sid": "LogGroups", "Effect": "Allow", "Action": "logs:DescribeLogGroups",
         "Resource": f"arn:aws:logs:{region}:{account}:log-group:*"},
        {"Sid": "Traces", "Effect": "Allow",
         "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules", "xray:GetSamplingTargets"], "Resource": "*"},
        {"Sid": "Metrics", "Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
         "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}},
        {"Sid": "BedrockModels", "Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
         # us.* inference profiles route to foundation models in several US regions
         "Resource": ["arn:aws:bedrock:*::foundation-model/*",
                      f"arn:aws:bedrock:{region}:{account}:inference-profile/*"]},
        {"Sid": "Memory", "Effect": "Allow",
         "Action": ["bedrock-agentcore:CreateEvent", "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:ListEvents", "bedrock-agentcore:DeleteEvent",
                    "bedrock-agentcore:GetMemory", "bedrock-agentcore:ListSessions",
                    "bedrock-agentcore:ListActors", "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:ListMemoryRecords"],
         "Resource": f"arn:aws:bedrock-agentcore:{region}:{account}:memory/{memory_id}"},
        {"Sid": "WorkloadIdentity", "Effect": "Allow",
         "Action": ["bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
         "Resource": [f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default",
                      f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default/workload-identity/{NAME}-*"]},
        {"Sid": "ReadIndexFromS3", "Effect": "Allow", "Action": "s3:GetObject",
         "Resource": f"arn:aws:s3:::{data_bucket}/index/*"},
        {"Sid": "ListIndex", "Effect": "Allow", "Action": "s3:ListBucket",
         "Resource": f"arn:aws:s3:::{data_bucket}",
         "Condition": {"StringLike": {"s3:prefix": ["index/*"]}}},
    ]}
    try:
        arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
        iam.update_assume_role_policy(RoleName=ROLE_NAME, PolicyDocument=json.dumps(trust))
        created = False
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        arn = iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust),
                              Description="AgentCore Runtime execution role for Protocol Intelligence",
                              Tags=[{"Key": "Project", "Value": "ProtocolIntelligence"}])["Role"]["Arn"]
        created = True
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="runtime-access",
                        PolicyDocument=json.dumps(policy))
    print(f"Execution role {'created' if created else 'updated'}: {arn}")
    if created:
        time.sleep(15)  # new roles take a few seconds before AWS services can assume them
    return arn


# ------------------------------------------------------------------ 3. package
def build_dependencies(force: bool) -> Path:
    deps = BUILD / "deps"
    stamp = BUILD / "deps.sha256"
    digest = hashlib.sha256(REQUIREMENTS.read_bytes() + PYTHON.encode()).hexdigest()
    if not force and deps.exists() and stamp.exists() and stamp.read_text() == digest:
        print("Dependencies unchanged (cached)")
        return deps
    shutil.rmtree(deps, ignore_errors=True)
    print("Installing Linux ARM64 dependencies (first run takes a minute)...")
    subprocess.run(["uv", "pip", "install", "--target", str(deps), "--python-version", PYTHON,
                    "--python-platform", "aarch64-manylinux_2_28", "--only-binary", ":all:",
                    "-r", str(REQUIREMENTS), "--quiet"], check=True)
    stamp.write_text(digest)
    return deps


def build_zip(deps: Path) -> Path:
    out = BUILD / "deployment.zip"
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in deps.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                z.write(path, path.relative_to(deps).as_posix())
        for path in PACKAGE_SRC.rglob("*"):
            rel = path.relative_to(PACKAGE_SRC)
            if path.is_file() and not (set(rel.parts) & EXCLUDE_DIRS):
                z.write(path, f"protocol_intelligence/{rel.as_posix()}")
        z.write(ENTRYPOINT, "agentcore_app.py")
    size = out.stat().st_size / 1e6
    print(f"Package: {out} ({size:.1f} MB)")
    if size > MAX_ZIP_MB:
        raise RuntimeError(f"Package is {size:.0f} MB; AgentCore code deployments allow {MAX_ZIP_MB} MB")
    return out


def upload_zip(zip_path: Path, data_bucket: str) -> str:
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()[:16]
    key = f"deploy/agentcore/{NAME}-{digest}.zip"
    client("s3").upload_file(str(zip_path), data_bucket, key)
    print(f"Uploaded s3://{data_bucket}/{key}")
    return key


# ------------------------------------------------------------------ 4. runtime
def deploy_runtime(ctl, role_arn: str, data_bucket: str, key: str, memory_id: str) -> str:
    s = get_settings()
    spec = dict(
        agentRuntimeArtifact={"codeConfiguration": {
            "code": {"s3": {"bucket": data_bucket, "prefix": key}},
            "runtime": "PYTHON_" + PYTHON.replace(".", "_"),
            "entryPoint": ["agentcore_app.py"]}},
        roleArn=role_arn,
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": "HTTP"},
        lifecycleConfiguration={"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
        environmentVariables={
            "AWS_REGION": s.aws_region, "BEDROCK_MODEL_ID": s.bedrock_model_id,
            "BEDROCK_EMBEDDING_MODEL_ID": s.bedrock_embedding_model_id,
            "S3_BUCKET": data_bucket, "AGENTCORE_MEMORY_ID": memory_id,
            "DATA_DIR": "/tmp/data-local"},
        description="Protocol Intelligence: grounded Q&A over 100 clinical trial protocols",
    )
    existing = next((r for page in ctl.get_paginator("list_agent_runtimes").paginate()
                     for r in page["agentRuntimes"] if r["agentRuntimeName"] == NAME), None)
    if existing:
        rid = existing["agentRuntimeId"]
        print(f"Updating runtime {rid}")
        ctl.update_agent_runtime(agentRuntimeId=rid, **spec)
    else:
        print(f"Creating runtime {NAME}")
        rid = ctl.create_agent_runtime(agentRuntimeName=NAME, **spec,
                                       tags={"Project": "ProtocolIntelligence"})["agentRuntimeId"]
    info = wait(lambda: ctl.get_agent_runtime(agentRuntimeId=rid), {"READY"},
                {"CREATE_FAILED", "UPDATE_FAILED"}, "Runtime")
    print(f"Runtime READY: {info['agentRuntimeArn']} (version {info['agentRuntimeVersion']})")
    return info["agentRuntimeArn"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild-deps", action="store_true")
    args = ap.parse_args()

    s = get_settings()
    account = client("sts").get_caller_identity()["Account"]
    data_bucket = bucket()
    ctl = client("bedrock-agentcore-control")

    memory_id = ensure_memory(ctl)
    save_env("AGENTCORE_MEMORY_ID", memory_id)
    role_arn = ensure_role(account, s.aws_region, memory_id, data_bucket)
    zip_path = build_zip(build_dependencies(args.rebuild_deps))
    key = upload_zip(zip_path, data_bucket)
    wait(lambda: ctl.get_memory(memoryId=memory_id)["memory"], {"ACTIVE"}, {"FAILED"}, "Memory")
    arn = deploy_runtime(ctl, role_arn, data_bucket, key, memory_id)
    save_env("AGENTCORE_RUNTIME_ARN", arn)
    print("\nDone. Test with: python scripts/invoke_agentcore.py \"How many studies are recruiting?\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
