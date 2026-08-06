# Insurance OS — Backend (`Insurance_BE`)

Shared backend for **two insurance verticals** that reuse one platform:

- **MGA** (underwriting on delegated authority) — powers `MGA-FE`.
- **Wholesale E&S broker** (placing risk with carriers) — powers `Insurance-FE`.

**Core idea:** build the expensive, common engine **once** (extraction, ingestion, rules, LLM, audit, reporting, auth), then add a **thin per-vertical layer** (decision core + workflow modules). One multi-tenant deployment; a `vertical` flag selects the right rules/schemas/workflows.

This lets **two developers work in parallel with zero conflict** — one on MGA Submission Triage, one on the E&S first workflow — because each owns a separate module folder and everyone depends on frozen shared interfaces.

---

## Read these docs in order
| Doc | What it covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Tech stack, request flow, multi-tenancy, high-level design |
| [docs/FOLDER_STRUCTURE.md](docs/FOLDER_STRUCTURE.md) | Where everything lives (and who owns what) |
| [docs/CORE_MODULES.md](docs/CORE_MODULES.md) | The shared modules + their interfaces (build once) |
| [docs/PHASES_AND_STAGES.md](docs/PHASES_AND_STAGES.md) | The full delivery plan, phase by phase |
| [docs/PARALLEL_WORK.md](docs/PARALLEL_WORK.md) | The no-conflict rules so 2+ devs work simultaneously |
| [docs/WORKFLOW_TEMPLATE.md](docs/WORKFLOW_TEMPLATE.md) | Step-by-step recipe to build ANY workflow |
| [docs/DATA_AND_FIXTURES.md](docs/DATA_AND_FIXTURES.md) | Test-data + validation-rules convention |
| [docs/CONNECTORS_NANGO.md](docs/CONNECTORS_NANGO.md) | Gmail / Sheets / Drive via Nango |

## Tech stack (proposed)
- **Python + FastAPI** (modular monolith — one package/router per workflow = clean ownership boundaries).
- **PostgreSQL + SQLModel/SQLAlchemy + Alembic** (data), **Redis + Arq** (async ingestion/processing jobs; async-native, chosen over Celery). Dev uses SQLite via aiosqlite (Postgres-ready).
- **OpenAI** for the LLM layer (drafting/narrative/classification, citation-enforced), behind an `LLMService` wrapper so the model is a config choice.
- **Nango** for all Google/Gmail connectors (read mail + attachments, send mail, Sheets/Drive read-write).
- **Pydantic v2** for schemas/contracts; **uv** (or Poetry), **pytest**, **ruff**, **mypy** (strict).

> FastAPI + package-per-workflow keeps parallel work safe — see PARALLEL_WORK.md. The LLM sits behind an interface, so the model/provider is a config choice, never wired into workflow code.

## One-minute mental model
```
Email/docs ─► Ingestion (Nango) ─► Extraction Core ─► Rules Engine (validation)
      ─► Decision Core (per vertical: appetite | matching) ─► LLM (draft, cited)
      ─► Output Package ─► Review Queue ─► Human approves ─► write-back + Audit
```
Everything left of "Decision Core" is **shared**. The Decision Core and the workflow orchestration are **per vertical**.

---

## Running (Phase 0)

Phase 0 is the shared skeleton: FastAPI app + health route, the 12 base tables (SQLite in
dev, Postgres-ready), frozen `core/common` contracts, header-stub tenancy/auth, empty
vertical routers, core-module interfaces (Phase 1 stubs), and the fixtures loader.

**Prereqs:** Python 3.12+. Dev DB is SQLite — no Docker, no Postgres, no Redis needed.

### 1. Create + activate a virtualenv (Windows PowerShell)
```powershell
python -m venv .venv
.\.venv\Scripts\activate
```
(Git Bash: `source .venv/Scripts/activate` · macOS/Linux: `source .venv/bin/activate`)

### 2. Install dependencies
```powershell
pip install -r requirements-dev.txt      # app + dev tools (ruff, mypy, pytest)
# or, runtime only:  pip install -r requirements.txt
```

### 3. Configure env
```powershell
copy .env.example .env
```
Defaults work out of the box (SQLite). Set `TEST_DATA_ROOT` to your test-data folder to
load fixtures.

### 4. Create the database schema
```powershell
alembic upgrade head
```

### 5. (Optional) Seed demo tenants + users
Inserts a demo MGA tenant and a demo E&S tenant, each with a junior + senior user, so the
header-stub auth and tenant→vertical lookup work immediately.
```powershell
python src/core/seed.py          # self-bootstraps src onto sys.path
# or, with src on PYTHONPATH:  python -m core.seed
```

### 6. Run the app
```powershell
uvicorn main:app --app-dir src --reload --port 4000
```
Health check:
```powershell
curl http://localhost:4000/api/core/health      # -> {"phase":0}
```
Interactive docs at http://localhost:4000/docs.

### Header-stub auth (Phase 0)
There is no login flow. Tenant/user/role come from request headers; `vertical` is looked
up from the tenant (with an `x-vertical` fallback):
```
x-tenant-id: demo-mga
x-user-id:   demo-mga-junior
x-role:      junior          # junior | senior | admin
```

### Quality gates
```powershell
ruff check .
mypy src
pytest
```

---

## Running (Phase 1 — Shared Core)

Phase 1 replaces the Phase-0 stubs with real, interfaced implementations: `extraction`,
`rules_engine`, `llm`, `documents`, `review_queue`, `audit`, `reporting`, `ingestion`
(Nango mock + live stub), and `jobs` (Arq on Redis). Verticals stay empty (Phase 2).

Steps 1–5 above are unchanged. Additional notes:

- **No API key needed for tests.** The `LLMService` factory falls back to a mock provider
  when `OPENAI_API_KEY` is unset, so the smoke test runs fully offline. Set a real key to
  route to OpenAI (`LLM_MODEL_FAST/STANDARD/DEEP` pick the tier).
- **Redis is optional for tests.** The async ingestion→extraction job runs on Arq/Redis in
  production, but the test suite exercises the job logic + error queue directly (no server),
  and the Arq-burst test skips cleanly if Redis/burst isn't runnable locally.

### Run the async worker (production — needs Redis)
```powershell
# 1. start Redis (e.g. Docker):  docker run -p 6379:6379 redis
# 2. run the Arq worker:
arq core.jobs.worker.WorkerSettings      # from repo root, src on PYTHONPATH
#   PowerShell:  $env:PYTHONPATH="src"; arq core.jobs.worker.WorkerSettings
```
The worker runs `ingest_and_extract`, tracking every run in the `job_run` table; failures
are marked `error` and stay queryable via `JobRunService.errors(...)` — a visible error
queue that works even when Redis is down (the DB is the record of truth).

---

## MGA Submission Triage (Phase 2 — first workflow)

The first real workflow lives entirely in `verticals/mga/` (decision core + `submission_triage`
package) and reuses every shared service. Routes under `/api/mga/submission-triage`.

```powershell
alembic upgrade head           # includes mga_appetite_result
python src/core/seed.py        # demo-mga tenant
uvicorn main:app --app-dir src --reload --port 4000
```
Run the pipeline over fixture submissions, then read the inbox + a detail (headers required):
```powershell
$h = @{ "x-tenant-id"="demo-mga"; "x-user-id"="demo-mga-junior"; "x-role"="junior" }
# trigger triage for a fixture submission (prod: the Arq ingestion job does this)
Invoke-RestMethod -Method Post -Headers $h "http://localhost:4000/api/mga/submission-triage/run?message_id=submission_01"
Invoke-RestMethod -Headers $h "http://localhost:4000/api/mga/submission-triage"                 # inbox list
Invoke-RestMethod -Headers $h "http://localhost:4000/api/mga/submission-triage/<id>"            # TriageDetail
# human action (approve within JUNIOR_PREMIUM_CAP; send = request-info; escalate)
Invoke-RestMethod -Method Post -Headers $h -ContentType application/json `
  -Body '{"action":"approve","amount":50000}' "http://localhost:4000/api/mga/submission-triage/<id>/act"
```
The FE wiring spec (endpoints + real sample JSON + field mapping) is in
[docs/FE_CONTRACT_submission_triage.md](docs/FE_CONTRACT_submission_triage.md).

### Run the triage eval (all 10 Workflow_1 submissions vs the dataset spec)
```powershell
pytest src/verticals/mga/submission_triage/eval_test.py -v
```

## Running (Phase 2 — MGA Renewal Management, Workflow_2)
Second MGA workflow: compares the expiring term (prior policy) vs the current renewal
(questionnaire + updated loss run/financials), maps RN-01..RN-12 → `RENEW_AS_IS` /
`RENEW_WITH_CHANGES` / `NON_RENEW` (+ retention + lapse-risk). Routes under `/api/mga/renewal`.
Reuses the shared core + MGA `AppetiteConfig` (for the RN-09 recheck); `verticals/es` untouched.
```powershell
alembic upgrade head          # adds mga_renewal_result
python src/core/seed.py
uvicorn main:app --app-dir src --port 4000
curl -X POST -H "x-tenant-id: demo-mga" -H "x-user-id: demo-mga-senior" -H "x-role: senior" `
  "http://localhost:4000/api/mga/renewal/run?message_id=renewal_03"
curl -H "x-tenant-id: demo-mga" -H "x-user-id: demo-mga-senior" -H "x-role: senior" `
  http://localhost:4000/api/mga/renewal
pytest src/verticals/mga/renewal_management/eval_test.py -v     # eval (7 cases + rule-version proof)
```
FE wiring spec: [docs/FE_CONTRACT_renewal_management.md](docs/FE_CONTRACT_renewal_management.md).

### Run the end-to-end smoke test
Proves the full pipeline on the **real Workflow_1 fixtures** (requires `TEST_DATA_ROOT`):
ingest(mock) → extract → rules → (stub decision) → review item → audit entry, plus a
missing-document case (submission_09 → required-doc rule fails → REQUEST_INFO / missing-info)
and the async job + error-queue cases.
```powershell
alembic upgrade head          # base tables + job_run
python src/core/seed.py       # demo-mga tenant (used by the async job tests)
pytest tests/test_smoke_pipeline.py tests/test_jobs_async.py -v
# or just the whole suite:
pytest
```

