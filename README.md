# Protocol Intelligence

A chatbot that answers clinical-trial protocol questions, with citations, over 100 ClinicalTrials.gov studies.
It is built with Strands Agents, Amazon Bedrock and Bedrock AgentCore.
See `Protocol_Intelligence_AWS_Architecture_and_Implementation_Plan.docx` for the design.

## Use the chatbot (no AWS account needed)

All you need is the endpoint URL and an API key (ask the project owner for the key).

```
POST https://i7uud2rh46hdcieedegoairhhy0kuqtt.lambda-url.us-east-1.on.aws/
x-api-key: <API key>
Content-Type: application/json

{"prompt": "your question", "session_id": "optional - send it back for follow-up questions"}
```

**macOS / Linux**

```bash
URL="https://i7uud2rh46hdcieedegoairhhy0kuqtt.lambda-url.us-east-1.on.aws/"
KEY="<API key>"

curl -sS --max-time 300 -X POST "$URL" -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d '{"prompt": "How many studies are recruiting?"}'

# answer text only (needs jq)
curl -sS --max-time 300 -X POST "$URL" -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d '{"prompt": "How many studies are recruiting?"}' | jq -r .answer
```

**Windows PowerShell**

```powershell
$url = "https://i7uud2rh46hdcieedegoairhhy0kuqtt.lambda-url.us-east-1.on.aws/"
$key = "<API key>"

$r = Invoke-RestMethod -Method Post -Uri $url -TimeoutSec 300 -ContentType "application/json" `
       -Headers @{ "x-api-key" = $key } -Body '{"prompt": "How many studies are recruiting?"}'
$r.answer

# follow-up in the same conversation
$body = @{ prompt = "How many of those are oncology?"; session_id = $r.session_id } | ConvertTo-Json
(Invoke-RestMethod -Method Post -Uri $url -TimeoutSec 300 -ContentType "application/json" `
   -Headers @{ "x-api-key" = $key } -Body $body).answer
```

**Python** (`pip install requests`)

```python
import requests
URL, KEY = "https://i7uud2rh46hdcieedegoairhhy0kuqtt.lambda-url.us-east-1.on.aws/", "<API key>"

def ask(prompt, session_id=None):
    r = requests.post(URL, headers={"x-api-key": KEY}, timeout=300,
                      json={"prompt": prompt, "session_id": session_id})
    r.raise_for_status()
    return r.json()

first = ask("How many studies are recruiting?")
print(first["answer"])
print(ask("How many of those are oncology?", first["session_id"])["answer"])
```

**Postman:** method POST, the URL above, Authorization *No Auth*, header `x-api-key`, raw JSON body.

### Response

```json
{
  "session_id": "session-3f2a...",
  "answer": "There are **17 studies** currently recruiting ... [S1]",
  "citations": [
    {"ref": "S1", "label": "NCT04175600, Study Protocol, pp. 22-23, section \"...\"",
     "nct_id": "NCT04175600", "document_title": "Study Protocol", "section_path": "...",
     "page_start": 22, "page_end": 23,
     "source_url": "https://cdn.clinicaltrials.gov/large-docs/00/NCT04175600/Prot_000.pdf"}
  ],
  "unknown_refs": [],
  "tools_used": [{"tool": "query_study_cohort", "calls": 1}],
  "latency_s": 6.7,
  "tokens": {"input": 4812, "output": 306}
}
```

- `answer` is Markdown; `[S1]`, `[S2]` point to entries in `citations`.
- `unknown_refs` lists any reference in the answer that was not backed by retrieved evidence (normally empty).
- **Follow-ups:** send the returned `session_id` with the next question. Omit it to start a new conversation.
  Conversations are kept for 30 days. A custom `session_id` must be at least 33 characters.

### Errors and timing

| Status | Meaning |
|---|---|
| 401 | Missing or wrong `x-api-key` |
| 400 | Body is not JSON, `prompt` is missing, or `session_id` is shorter than 33 characters |
| 5xx | Agent error; retry, or check the CloudWatch logs (see [Operations](#operations)) |

The first question after the agent has been idle takes 20–60 s (it loads the search index); later
questions take about 4–50 s depending on how much searching they need. Use a timeout of at least 300 s.

### Example questions

- What are the key exclusion criteria in NCT04175600?
- How many phase 3 studies are in the corpus, broken down by therapeutic area?
- Compare the primary endpoints of the diabetes studies that use semaglutide or tirzepatide.
- Which oncology studies allow patients with brain metastases?

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
  ingestion/           ClinicalTrials.gov client, study selection, PDF download, admin onboarding
  processing/          PDF extraction, sectionizing, chunking, registry chunks
  knowledge/           SQLite study repository
  retrieval/           embeddings, LanceDB index, hybrid search
  agent/               Strands agent, tools, prompts, evidence ledger
  api/                 FastAPI app, chat UI and admin portal
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

## Onboard new protocol PDFs (admin portal)

`scripts/serve.py` also serves an admin portal at http://127.0.0.1:8000/admin (linked from the chat
header). Drop in a PDF, give its NCT ID, and it is indexed in about 15–60 s:

1. Checks the file: it must be a PDF of at most 50 MB with a text layer, and not already be in the
   store (matched by SHA-256).
2. If the NCT ID is not in the corpus, fetches its ClinicalTrials.gov record into the study store and
   indexes its registry chunks too.
3. Extracts, sectionizes and chunks the PDF with the same pipeline as the batch corpus, embeds the
   chunks with Titan, and appends them to LanceDB. The local chatbot can search them right away.
4. **Publish to AgentCore** uploads `index/` to S3. New runtime sessions then load the updated store.

Set `ADMIN_API_KEY` in `.env` to require a key. The page asks for it once per browser tab.
Uploads are stored apart from the batch corpus, so `build_index.py` keeps them on a rebuild:
`raw/uploads/`, `processed/uploaded_documents.json`, `processed/uploaded_chunks.jsonl` and
`processed/uploaded_studies.jsonl`. Give a public URL for each upload so that citations from the
deployed agent can link to the PDF. Without one they link to `/documents/<sha256>.pdf` on the
local server.

| Endpoint (header `x-admin-key`) | Purpose |
|---|---|
| `POST /api/admin/documents` | multipart: `file`, `nct_id`, optional `document_title`, `document_version`, `source_url`, `therapeutic_area`; returns `202` with a job |
| `GET /api/admin/jobs/{job_id}` | job status and steps: `queued`, `running`, `done` or `failed` |
| `GET /api/admin/documents` | every protocol document in the index, uploads first, plus totals |
| `DELETE /api/admin/documents/{sha256}` | remove an uploaded document (and the study it added, if any) |
| `POST /api/admin/publish` | upload `index/` to S3 for AgentCore; back up the upload files |

```powershell
curl.exe -H "x-admin-key: $env:ADMIN_API_KEY" -F nct_id=NCT01234567 -F "document_title=Protocol v2" `
  -F "file=@C:\path\protocol.pdf" http://127.0.0.1:8000/api/admin/documents
```

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

## Deploy the public HTTPS endpoint

```powershell
.venv\Scripts\python scripts\deploy_http_proxy.py               # Lambda Function URL + API key (re-run to update)
.venv\Scripts\python scripts\deploy_http_proxy.py --rotate-key  # issue a new key (old one stops working)
```

A Lambda (`deploy/http_proxy_lambda.py`) checks the `x-api-key` header and calls the runtime with its
own role (`ProtocolIntelligenceHttpProxyRole`, allowed only `InvokeAgentRuntime` on this runtime). The
URL and key are saved to `.env` as `PROXY_URL` / `PROXY_API_KEY`; the key is never committed. This is
what [Use the chatbot](#use-the-chatbot-no-aws-account-needed) calls.

- There is one shared key: anyone who has it can query the agent and incur Bedrock cost. Share it only
  with intended users and rotate it if it leaks (rotation affects everyone).
- Requests are not tied to individual users; `user_id` in the body is passed through but not verified.

## Operations

| Resource (us-east-1, account 422928733699) | Name / ID |
|---|---|
| Public endpoint (Lambda Function URL) | `https://i7uud2rh46hdcieedegoairhhy0kuqtt.lambda-url.us-east-1.on.aws/` |
| Lambda proxy | `protocol-intelligence-http-proxy` (role `ProtocolIntelligenceHttpProxyRole`) |
| AgentCore Runtime | `protocol_intelligence-Bi1uvDEDYb`, endpoint `DEFAULT` |
| AgentCore Memory | `protocol_intelligence_memory-aKf2988eR8` (30-day conversation history) |
| Runtime execution role | `ProtocolIntelligenceAgentCoreRuntimeRole` |
| S3 bucket | `protocol-intelligence-422928733699-us-east-1` (`index/`, `deploy/agentcore/`) |

- Logs: CloudWatch log groups `/aws/lambda/protocol-intelligence-http-proxy` and
  `/aws/bedrock-agentcore/runtimes/protocol_intelligence-Bi1uvDEDYb-DEFAULT`.
- Update the agent: `scripts/deploy_agentcore.py`. Update the proxy: `scripts/deploy_http_proxy.py`.
  Neither changes the public URL or the key (unless `--rotate-key`).
- Rebuild the knowledge base, then `sync_s3.py up --prefix index`; new runtime sessions load the new index.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src scripts tests
```
