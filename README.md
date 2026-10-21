# Protocol Intelligence

A chatbot that answers clinical-trial protocol questions, with citations, over 100 ClinicalTrials.gov studies.
It is built with Strands Agents, Amazon Bedrock and Bedrock AgentCore.
See `Protocol_Intelligence_AWS_Architecture_and_Implementation_Plan.docx` for the design.

## How it works

```
ClinicalTrials.gov API ──► ingest ──► process (PDF → sections → chunks) ──► index
                                                                              │
                         SQLite study store (studies.db)  ◄───────────────────┤
                         LanceDB chunk index (Titan embeddings + BM25) ◄──────┘
                                                                              │
   question ──► Strands agent (Claude on Bedrock) ──► tools ──► cited answer ◄┘
                     │
                     └── AgentCore Runtime + AgentCore Memory (in AWS)
```

1. **Ingest:** select 100 interventional studies with posted protocol PDFs (20 per therapeutic area:
   oncology, cardiovascular, diabetes/metabolic, infectious disease, mental health) and download them.
2. **Process:** extract PDF text with PyMuPDF, split it into protocol sections, and chunk both the
   protocols and the registry records.
3. **Index:** canonical study records go into SQLite; chunks are embedded with Amazon Titan Text
   Embeddings v2 and stored in LanceDB with a full-text index.
4. **Answer:** the agent searches with hybrid retrieval (vector + BM25, merged by reciprocal rank
   fusion), reads source passages, and cites every claim as `[S1]`, `[S2]`. An evidence ledger checks
   that each cited reference was actually retrieved, and flags any that weren't.

### Agent tools

| Tool | Purpose |
|---|---|
| `search_protocols` | Hybrid search over protocol and registry chunks, filterable by NCT ID, section, source and area |
| `get_source_passage` | Fetch a cited chunk with its neighbouring chunks for fuller context |
| `get_study_metadata` | Structured registry metadata for specific NCT IDs |
| `query_study_cohort` | Counts and filters across the whole corpus ("how many recruiting oncology studies?") |

## Project layout

```
src/protocol_intelligence/
  config.py            settings from .env, AWS session and clients
  storage.py           S3 helpers
  ingestion/           ClinicalTrials.gov client, study selection, normalisation, PDF download
  processing/          PDF extraction, sectionizing, chunking, registry chunks
  knowledge/           SQLite study repository
  retrieval/           embeddings, LanceDB index, hybrid search
  agent/               Strands agent, tools, prompts, evidence ledger
  api/                 FastAPI app + static test UI
scripts/               command-line entry points (below)
deploy/                AgentCore entrypoint and runtime requirements
infra/iam/             IAM policy for the developer identity
tests/                 pytest suite
data-local/            generated data (gitignored)
```

## Where credentials live

| What | Stored in | In git? |
|---|---|---|
| AWS access keys / SSO | `~/.aws/credentials` / `~/.aws/config`, profile `protocol-dev` | No (outside the repo) |
| Bedrock API key | `.env` → `AWS_BEARER_TOKEN_BEDROCK` | No (`.env` is gitignored) |
| Model IDs, bucket, AgentCore IDs | `.env` (template: `.env.example`) | Only the template |

Code reads all of these through `protocol_intelligence.config` (`config.get_settings()`, `config.client("s3")`).

## Setup

Requires Python 3.11–3.12 and [uv](https://docs.astral.sh/uv/).

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"

.\scripts\setup-credentials.ps1                    # AWS profile + Bedrock key + verification
.\scripts\setup-credentials.ps1 -Only Bedrock      # add or rotate just the Bedrock key
.\scripts\setup-credentials.ps1 -SetVar S3_BUCKET  # store any other value in .env
.venv\Scripts\python.exe scripts\check_access.py   # re-check access at any time
```

The IAM permissions the project identity needs are in `infra/iam/protocol-dev-policy.json`.

## Build the knowledge base

Run these in order. Each step is re-runnable and skips work already done.

```powershell
# 1. Corpus ingestion (no AWS needed)
.venv\Scripts\python.exe scripts\ingest_studies.py --dry-run   # selection + manifest only
.venv\Scripts\python.exe scripts\ingest_studies.py             # downloads PDFs

# 2. Extract, sectionize and chunk protocols + registry records
.venv\Scripts\python.exe scripts\process_documents.py

# 3. Build the SQLite study store and the LanceDB index (calls Bedrock for embeddings)
.venv\Scripts\python.exe scripts\build_index.py

# 4. Create the S3 bucket (secure defaults) and upload the index
.venv\Scripts\python.exe scripts\setup_s3.py
.venv\Scripts\python.exe scripts\sync_s3.py up --prefix index
.venv\Scripts\python.exe scripts\sync_s3.py down --prefix index   # restore on another machine
```

Outputs go to `data-local/` (gitignored): `manifest.json`, `coverage_report.md`,
`processed/studies.jsonl` (canonical records), `raw/studies/`, `raw/protocols/`, `index/`.
The selection policy is in `src/protocol_intelligence/ingestion/study_selector.py`.

## Ask questions locally

```powershell
.venv\Scripts\python.exe scripts\ask.py "What are the key exclusion criteria in NCT04175600?"
.venv\Scripts\python.exe scripts\ask.py --chat          # multi-turn conversation
.venv\Scripts\python.exe scripts\ask.py --json "..."    # full structured result

.venv\Scripts\python.exe scripts\serve.py               # web UI at http://127.0.0.1:8000
```

The local API exposes `GET /api/info` and `POST /api/chat`.

## Deploy to AgentCore Runtime

```powershell
.venv\Scripts\python scripts\deploy_agentcore.py          # Memory + IAM role + zip + runtime (re-run to update)
.venv\Scripts\python scripts\invoke_agentcore.py "How many studies are recruiting?"
.venv\Scripts\python scripts\invoke_agentcore.py --session <id> "Which of those are oncology?"
```

- Code (zip) deployment: no Docker. Dependencies from `deploy/requirements-runtime.txt` are built for
  Linux ARM64 (`manylinux_2_28`) into `build/agentcore/` and cached; the zip is uploaded to
  `s3://<bucket>/deploy/agentcore/`.
- Entrypoint `deploy/agentcore_app.py` downloads `index/lancedb/` and `index/studies.db` from S3 on cold
  start and persists each session's dialogue to AgentCore Memory (`AGENTCORE_MEMORY_ID`).
- The execution role `ProtocolIntelligenceAgentCoreRuntimeRole` is least-privilege: Bedrock model
  calls, the one Memory, read-only `index/` in the bucket, and logs/traces.
- Payload `{"prompt": "...", "user_id": "optional"}`; the session is the caller's `runtimeSessionId`
  (at least 33 characters).
- To call the HTTPS endpoint directly (e.g. Postman), POST to
  `https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<url-encoded runtime ARN>/invocations`
  with AWS Signature v4 auth (service `bedrock-agentcore`) and the
  `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src scripts tests
```
