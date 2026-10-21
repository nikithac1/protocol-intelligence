# Protocol Intelligence

A chatbot that answers clinical-trial protocol questions, with citations, over 100 ClinicalTrials.gov studies.
It is built with Strands Agents, Amazon Bedrock and Bedrock AgentCore.
See `Protocol_Intelligence_AWS_Architecture_and_Implementation_Plan.docx` for the design.

## Where credentials live

| What | Stored in | In git? |
|---|---|---|
| AWS access keys / SSO | `~/.aws/credentials` / `~/.aws/config`, profile `protocol-dev` | No (outside the repo) |
| Bedrock API key | `.env` → `AWS_BEARER_TOKEN_BEDROCK` | No (`.env` is gitignored) |
| Model IDs, bucket, AgentCore IDs | `.env` (template: `.env.example`) | Only the template |

Code reads all of these through `protocol_intelligence.config` (`config.get_settings()`, `config.client("s3")`).

## Setup

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"

.\scripts\setup-credentials.ps1                    # AWS profile + Bedrock key + verification
.\scripts\setup-credentials.ps1 -Only Bedrock      # add or rotate just the Bedrock key
.\scripts\setup-credentials.ps1 -SetVar S3_BUCKET  # store any other value in .env
.venv\Scripts\python.exe scripts\check_access.py   # re-check access at any time
```

The IAM permissions the project identity needs are in `infra/iam/protocol-dev-policy.json`.

## Corpus ingestion (no AWS needed)

```powershell
.venv\Scripts\python.exe scripts\ingest_studies.py --dry-run   # selection + manifest only
.venv\Scripts\python.exe scripts\ingest_studies.py             # downloads PDFs; re-runnable
```

Outputs go to `data-local/` (gitignored): `manifest.json`, `coverage_report.md`,
`processed/studies.jsonl` (canonical records), `raw/studies/`, `raw/protocols/`.
The selection policy is in `src/protocol_intelligence/ingestion/study_selector.py`.

## Deploy to AgentCore Runtime

```powershell
.venv\Scripts\python scripts\deploy_agentcore.py          # Memory + IAM role + zip + runtime (re-run to update)
.venv\Scripts\python scripts\invoke_agentcore.py "How many studies are recruiting?"
.venv\Scripts\python scripts\invoke_agentcore.py --session <id> "Which of those are oncology?"
```

- Code (zip) deployment: no Docker. Dependencies from `deploy/requirements-runtime.txt` are built for
  Linux ARM64 into `build/agentcore/` and cached; the zip is uploaded to `s3://<bucket>/deploy/agentcore/`.
- Entrypoint `deploy/agentcore_app.py` downloads `index/lancedb/` and `index/studies.db` from S3 on cold
  start and persists each session's dialogue to AgentCore Memory (`AGENTCORE_MEMORY_ID`).
- Payload `{"prompt": "...", "user_id": "optional"}`; the session is the caller's `runtimeSessionId`.
