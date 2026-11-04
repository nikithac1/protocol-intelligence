"""Neptune Analytics graph for the knowledge graph tool. Safe to re-run.

  python scripts/setup_neptune.py role      # IAM role Neptune uses to read the CSVs (free)
  python scripts/setup_neptune.py up        # role + create graph (BILLED HOURLY) + load CSVs
  python scripts/setup_neptune.py load      # clear the graph and re-import (after build_graph.py)
  python scripts/setup_neptune.py status
  python scripts/setup_neptune.py pause     # billed at 10% while paused
  python scripts/setup_neptune.py resume
  python scripts/setup_neptune.py delete    # stops all charges; no snapshot is kept

Before `up` or `load`: python scripts/build_graph.py, then
python scripts/sync_s3.py up --prefix index/graph. The graph is the smallest size (16 m-NCU, no
replicas) and has a public endpoint that still requires SigV4-signed IAM requests.
                                                                           -> NEPTUNE_GRAPH_ID
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from botocore.exceptions import ClientError  # noqa: E402

from protocol_intelligence.config import client, get_settings  # noqa: E402
from protocol_intelligence.storage import bucket  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy_agentcore import wait  # noqa: E402
from setup_s3 import save_env  # noqa: E402

GRAPH_NAME = "protocol-intelligence-graph"
ROLE_NAME = "protocol-intelligence-neptune-import"
PREFIX = "index/graph/"
MEMORY_GB = 16  # smallest size: $0.48/h running, $0.048/h paused (us-east-1, Nov 2026)


# Create import role
def ensure_role(account: str) -> str:
    iam = client("iam")
    # Neptune's import does not pass aws:SourceAccount, so a condition on it blocks the role.
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "neptune-graph.amazonaws.com"},
        "Action": "sts:AssumeRole"}]}
    data_bucket = bucket()
    policy = {"Version": "2012-10-17", "Statement": [
        {"Sid": "ReadGraphCsv", "Effect": "Allow", "Action": "s3:GetObject",
         "Resource": f"arn:aws:s3:::{data_bucket}/{PREFIX}*"},
        {"Sid": "ListBucket", "Effect": "Allow",
         "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
         "Resource": f"arn:aws:s3:::{data_bucket}"},
    ]}
    try:
        arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
        iam.update_assume_role_policy(RoleName=ROLE_NAME, PolicyDocument=json.dumps(trust))
        created = False
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        arn = iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust),
                              Description="Neptune Analytics reads knowledge graph CSVs from S3",
                              Tags=[{"Key": "Project", "Value": "ProtocolIntelligence"}]
                              )["Role"]["Arn"]
        created = True
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="read-graph-csv",
                        PolicyDocument=json.dumps(policy))
    print(f"Import role {'created' if created else 'updated'}: {arn}")
    if created:
        time.sleep(15)  # new roles take a few seconds before AWS services can assume them
    return arn


# Find graph
def find_graph(ng) -> dict | None:
    for page in ng.get_paginator("list_graphs").paginate():
        for g in page["graphs"]:
            if g["name"] == GRAPH_NAME:
                return g
    return None


# Get graph ID
def graph_id(ng) -> str:
    g = find_graph(ng)
    if not g:
        raise SystemExit(f"No graph named {GRAPH_NAME}. Create it with: setup_neptune.py up")
    return g["id"]


# Create graph
def ensure_graph(ng) -> str:
    if g := find_graph(ng):
        print(f"Graph exists: {g['id']} ({g['status']})")
        gid = g["id"]
    else:
        print(f"Creating graph {GRAPH_NAME} ({MEMORY_GB} m-NCU, 0 replicas). Billing starts now.")
        gid = ng.create_graph(graphName=GRAPH_NAME, provisionedMemory=MEMORY_GB, replicaCount=0,
                              publicConnectivity=True, deletionProtection=False,
                              tags={"Project": "ProtocolIntelligence"})["id"]
    save_env("NEPTUNE_GRAPH_ID", gid)
    wait(lambda: ng.get_graph(graphIdentifier=gid), {"AVAILABLE"}, {"FAILED"}, "graph", 1800)
    print(f"Graph AVAILABLE: {gid}  (NEPTUNE_GRAPH_ID saved to .env)")
    return gid


# Reload graph data
def load(ng, gid: str, role_arn: str) -> None:
    info = ng.get_graph(graphIdentifier=gid)
    if info.get("status") != "AVAILABLE":
        raise SystemExit(f"Graph is {info.get('status')}; resume it first")
    print("Clearing the graph before import")
    ng.reset_graph(graphIdentifier=gid, skipSnapshot=True)
    wait(lambda: ng.get_graph(graphIdentifier=gid), {"AVAILABLE"}, {"FAILED"}, "reset", 1800)
    source = f"s3://{bucket()}/{PREFIX}"
    print(f"Importing {source}")
    task = ng.start_import_task(graphIdentifier=gid, source=source, format="OPEN_CYPHER",
                                roleArn=role_arn, failOnError=True)["taskId"]
    wait(lambda: ng.get_import_task(taskIdentifier=task),
         {"SUCCEEDED"}, {"FAILED", "CANCELLED", "ROLLING_BACK"}, "import", 1800)
    counts = json.loads(ng.execute_query(
        graphIdentifier=gid, language="OPEN_CYPHER",
        queryString="MATCH (n) RETURN labels(n)[0] AS label, count(n) AS n")["payload"].read())
    for row in sorted(counts["results"], key=lambda r: r["label"]):
        print(f"  {row['label']:<13} {row['n']:>4} nodes")


# Run Neptune action
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["role", "up", "load", "status", "pause", "resume", "delete"])
    action = ap.parse_args().action
    account = client("sts").get_caller_identity()["Account"]
    ng = client("neptune-graph")

    if action == "role":
        ensure_role(account)
    elif action == "up":
        role_arn = ensure_role(account)
        load(ng, ensure_graph(ng), role_arn)
        print("\nNext: set GRAPH_BACKEND=neptune in .env and ask a question")
    elif action == "load":
        load(ng, graph_id(ng), ensure_role(account))
    elif action == "status":
        g = find_graph(ng)
        print(f"{g['id']}: {g['status']}, {g.get('provisionedMemory')} m-NCU" if g else "No graph")
    elif action == "pause":
        g = find_graph(ng) or {"status": "missing"}
        if g["status"] != "AVAILABLE":
            print(f"Nothing to pause: graph is {g['status']}")
            return 0
        ng.stop_graph(graphIdentifier=g["id"])
        print("Pausing (billed at 10% while paused). Resume with: setup_neptune.py resume")
    elif action == "resume":
        gid = graph_id(ng)
        if ng.get_graph(graphIdentifier=gid)["status"] == "STOPPED":
            ng.start_graph(graphIdentifier=gid)
        wait(lambda: ng.get_graph(graphIdentifier=gid), {"AVAILABLE"}, {"FAILED"}, "resume", 1800)
        print("Graph AVAILABLE")
    elif action == "delete":
        ng.delete_graph(graphIdentifier=graph_id(ng), skipSnapshot=True)
        save_env("NEPTUNE_GRAPH_ID", "")
        print("Deleting. Charges stop once it is gone.")
        if get_settings().graph_backend == "neptune":
            print("Set GRAPH_BACKEND=local in .env: graph questions fail while it is neptune.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
