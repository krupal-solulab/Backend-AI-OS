# Build Status

## Phase 0 — Foundations ✅ (frozen)
- FastAPI skeleton, 12 base tables (SQLite dev / Postgres-ready), Alembic, frozen core/common
  contracts (incl. RuleResult.score), header-stub tenancy/auth, empty vertical routers,
  core-module Phase-1 stubs, fixtures loader (proven on real Workflow_1 data), seed script.
- Gates: ruff / mypy / pytest green; /api/core/health → {"phase":0}.

## Phase 1 — Shared Core ✅ (2026-07-23)

**What was built** — the Phase-0 stubs are now real, interfaced implementations:
- `core/extraction` — `DefaultExtractionService`: content+filename classification and cited
  `Key: Value` extraction; fields namespaced by doc kind; emits `documents.<kind>.present`
  flags; tolerant of the variable doc set (2–5+ docs, missing/extra docs never error).
- `core/rules_engine` — `DefaultRulesEngine` + pure `evaluate_ruleset`: all 6 checks
  (required·regex·min·max·compare·crossDoc) over the Rules-Console JSON shape; DB-backed
  publish/rollback over `RuleSet`→`RuleVersion`.
- `core/llm` — `LLMService` over `OpenAIProvider`/`MockLLMProvider` (factory by key):
  grounded, per-claim citation post-validation, tier routing (fast/standard/deep).
- `core/documents` (`LocalDocumentStore`: DB meta + disk), `core/review_queue`
  (`DefaultReviewQueueService`: RBAC + `JUNIOR_PREMIUM_CAP` → `AuthorityError`),
  `core/audit` (`DefaultAuditService`: append-only record/query), `core/reporting`
  (`DefaultReportingService`: group-by rollup).
- `core/ingestion` — `MockConnectorService` (fixtures) + `LiveNangoConnectorService` stub +
  `build_connector_service` factory. No live calls, no auto-send.
- `core/jobs` — Arq/Redis: `ingest_and_extract` task, `JobRunService`, `WorkerSettings`;
  `job_run` table is the queryable error queue.

**Key decisions / deviations**
- **Arq, not Celery** (async-native). Redis now in scope for Phase 1. Docs updated
  (README, ARCHITECTURE §9, CORE_MODULES).
- **Additive `JobRun` table** (+ migration `a6e8c2701d39`) so the error queue is queryable
  without Redis. No `core/common` contract change (contracts remain frozen).
- **Session convention**: DB-backed service methods take an `AsyncSession` first arg
  (Phase-1 detail; frozen contracts stay in `core/common`).
- **Stub decision lives in the test harness only** (required-fail → REQUEST_INFO else
  PROCEED). `core/` and `verticals/` remain decision-free; verticals stay empty.
- **Smoke-test rules** live in `tests/fixtures/ruleset_workflow1.json`, loaded into a
  `RuleVersion` at test setup (not hardcoded, not seeded).
- **No API key / no Redis needed for tests**: LLM uses the mock; job logic + error queue are
  tested directly; the Arq-burst test uses fakeredis and **skips cleanly** if incompatible
  (this environment's fakeredis lacks `INFO`, so burst skips — direct job tests still prove
  the pipeline + error queue).

**How to run the smoke test**
```powershell
.\.venv\Scripts\activate
pip install -r requirements-dev.txt
copy .env.example .env        # ensure TEST_DATA_ROOT points at the real "test data" folder
alembic upgrade head
python src/core/seed.py       # demo-mga tenant (async job tests)
pytest tests/test_smoke_pipeline.py tests/test_jobs_async.py -v
# full suite + gates:
ruff check .; mypy src; pytest
```
Production async worker (needs Redis): `arq core.jobs.worker.WorkerSettings` (with `src` on
`PYTHONPATH`).

## Phase 2 — MGA Submission Triage (2026-07-23) ✅

First real workflow + the MGA decision core, entirely under `verticals/mga/` (+ one additive
migration). `verticals/es/` untouched; `core/common` contracts unchanged (frozen).

**What was built**
- **Step 0 — extraction tuning** (additive, `core/extraction`): canonical `loss_run.total_incurred`
  (+ `_period`) and `sov.total_insurable_value`; repeating rows → `loss_run.claims` /
  `sov.locations` lists (no last-wins collapse); `coerce_number` for currency/qualified values;
  illegibility confidence heuristic. Proven in `tests/test_extraction_tuning.py`.
- **Step 1 — `verticals/mga/decision_core`** (Appetite Engine): EC-01 (manual review) → hard rules
  HR-01..04 (short-circuit DECLINE before LLM, FR-23) → completeness CR-01..04 → consistency
  CC-01/02 + SOV/limit → timing TR-01 → trend LT-02. Thresholds are data (`AppetiteConfig`).
  Transparent 0–100 score.
- **Step 2 — rule sets as data**: `verticals/mga/rulesets/workflow1_validation.json` (6-check
  Rules-Console shape) + `ensure_ruleset` loader (RuleSet→RuleVersion, published).
- **Step 3 — `verticals/mga/submission_triage`**: pipeline service + routes
  (`/api/mga/submission-triage` list / `{id}` detail / `{id}/act` / `run`); registered in the MGA
  vertical router. `TriageDetail`/`Submission` response schemas map 1:1 to MGA-FE.
- **MGA table**: additive `mga_appetite_result` (migration `dfddb1e69297`); `verticals/mga/models`
  registered in `migrations/env.py` so `alembic check` stays clean.
- **Step 4 — eval** (`verticals/mga/submission_triage/eval_test.py`): all 10 Workflow_1 submissions
  match the dataset's expected recommendation (01/06/10 PROCEED, 02/04/05/07/09 REQUEST_INFO,
  03 DECLINE on HR-01+HR-02, 08 manual review), plus RBAC/authority-cap checks.

**Key decisions / deviations** (all pre-approved)
- Compound appetite logic is MGA code with data thresholds (Option A); frozen `RuleCheckType`
  untouched. submission_08 manual review = `REQUEST_INFO` + `details.manual_review` +
  `MANUAL_REVIEW` flag (no new enum). `act` verbs = approve / send (=request-info, human-only,
  no auto-send) / escalate (frozen `ReviewAction`). Score model documented in `decision_core`.
- One infra touch outside `verticals/mga/`: added `import verticals.mga.models` to
  `migrations/env.py` (required so the new table is in `target_metadata` and `alembic check`
  passes). `act` keys off the submission id (what the FE has), resolving the review item server-side.

**How to run**
```powershell
alembic upgrade head; python src/core/seed.py
pytest src/verticals/mga/submission_triage/eval_test.py -v      # the eval
uvicorn main:app --app-dir src --port 4000                       # then POST /run, GET list/detail, POST /act
```
FE wiring spec: `docs/FE_CONTRACT_submission_triage.md`.

## Parallel-work conventions (MGA + E&S fully independent)
- **Contracts frozen** (`core/common`); each vertical owns `verticals/<v>/**` + its own vertical router.
- **`migrations/env.py` auto-discovers** every `verticals/*/models.py` — no shared edit to add tables.
- **`pyproject.toml` needs no per-workflow edits**: the mypy override `*.eval_test` covers every
  in-package eval for both verticals.
- **Migrations / multiple heads:** independent migrations from the same parent create two Alembic
  heads. After integrating, `alembic heads`; if two, `alembic merge heads -m "..."` then
  `alembic upgrade head`. Prefer **branch-per-workflow + PR** so `main` advances conflict-free.
- Net: day-to-day development needs no cross-vertical pulls; sync only to land on shared `main`.
