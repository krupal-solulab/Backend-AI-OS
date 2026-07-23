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
