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

## Phase 2B — E&S Market Matching ✅ (2026-07-23)

**Scope:** first E&S workflow, `verticals/es/**` only — `core/**` and `verticals/mga/**`
untouched (confirmed via `git status`; MGA router still an empty `@router` with no
`include_router` calls).

**What was built**
- `verticals/es/decision_core/carrier_profiles.py` — loads the 6-carrier panel from
  `Workflow_<n>/test_dataset/carrier_profiles/*.json` (E&S-owned; the shared
  `fixtures.loader` has no notion of a carrier panel and ignores this folder).
- `verticals/es/decision_core/matching.py` — the Matching/Ranking Engine implementing
  MM-01..MM-07 (see the dataset's `RULE_ENGINE_INTERPRETATION_GUIDE.md`) as three tiers:
  hard exclusion (class/state/premium-band), soft scoring (severity/completeness), and
  the independent MM-07 diligent-search flag. **Hybrid design**: premium-band and
  loss-run-years/required-doc checks route through the SHARED `core.rules_engine` — one
  `RuleSet`/`RuleVersion` **per carrier** (`seed_rules.py`), matching CORE_MODULES.md's
  "carrier-appetite rule sets as data" pattern. Semantic class-code scope matching,
  multi-state licensing, the per-class severity hard/soft distinction, the weighted
  composite score, and MM-07 are native Python here — they don't reduce cleanly to the
  generic evaluator's 6 field checks.
- `verticals/es/workflows/market_matching/` — `service.py` (`MarketMatchingPipeline`
  implementing `WorkflowPipeline[OutputPackage]`), `schema.py` (FE payload shape),
  `router.py` (`/api/es/market-matching/...`: run, list, detail, approve/override/
  escalate/send/issue), registered via one line in `verticals/es/router.py`.
- `tests/test_es_market_matching.py` — eval against the REAL dataset (see below), plus a
  synthetic missing-ACORD case proving the `REQUEST_INFO` path.

**Dataset:** `Workflow_10` (E&S · Market Matching — added to DATA_AND_FIXTURES.md's
mapping table), copied from the provided `Data sets/Workflow 1/market_matching_dataset/`
into `TEST_DATA_ROOT/Workflow_10/test_dataset/` unchanged, plus an authored
`Validation_Rules_Test_Dataset.md` consolidating the dataset's own README + interpretation
guide into the standard filename.

**Key decisions / deviations**
- Rule-set JSON authored against the REAL, current `core.rules_engine` shape
  (`params.value` for min/max, plain `required` for doc-presence) — not the originally
  "locked" `params.min`/`params.max`/`field2`/`{against,tolerancePct}` shape, since
  `core/rules_engine` is frozen/off-limits to this task and was never built to that spec.
- `Decision.outcome`: `PROCEED` when ≥1 carrier matches (even with per-carrier missing-info
  flags — the guide is explicit that completeness gaps must never suppress ranking),
  `DECLINE` for a true zero-match panel (submission_06), `REQUEST_INFO` only when the
  ACORD itself is missing (no class code/premium to match against ANY carrier — a
  submission-level validation gate, separate from per-carrier appetite).
- **Documented dataset/guide discrepancy** (not silently resolved): the interpretation
  guide's summary table says submission_04 excludes Ironclad for "trucking not accepted,"
  but `carrier_03_ironclad.json` actually accepts `trucking - non-hazmat` with the premium
  in-band — the real exclusion driver is MM-05 severity ($1.85M claim vs. its $500K
  ceiling), which for a non-roofing class is a soft factor per the guide's own text. This
  implementation lets Ironclad appear, score-penalized, rather than inventing an
  undocumented hard rule to match the guide's prose. See
  `Workflow_10/test_dataset/Validation_Rules_Test_Dataset.md`.
- No per-carrier `ceiling_type` (hard/soft) field exists in the source profiles despite the
  guide recommending one — implemented as "hard for roofing classes, soft elsewhere,"
  directly from the guide's own worked example, rather than fabricating carrier data.

**Verification:** `ruff check .` / `mypy src` / `alembic check` all clean; app boots,
`/api/core/health` still `{"phase":0}`; all 6 real submissions match the dataset's expected
rankings/exclusions (incl. zero-match + the one documented deviation above); synthetic
missing-ACORD → `REQUEST_INFO` case passes; full pipeline (ingest→extract→decide→draft→
package→review_queue→audit) passes end to end. Pre-existing, unrelated: 5 MGA tests
(`test_smoke_pipeline.py`, `test_jobs_async.py`) fail on this machine because the real
Workflow_1 MGA dataset isn't present at `TEST_DATA_ROOT` here — untouched by this work.
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
<<<<<<< Updated upstream
=======

## Frontend Integration — Submission Market Matching (2026-07-23) ✅

**Scope:** first live frontend↔backend wiring for this project — the `Insurance OS` repo
(TanStack Start, the E&S/wholesale-broker FE)'s `/app/workflows/submission-matching` screen
wired end-to-end to `/api/es/market-matching`. Every other FE screen (the other 9 workflow
screens, both foundation screens, analytics/assistant/settings, the marketing site) stays on
mock data (`simulateRequest`), untouched.

**What was built**
- **CORS** — `CORSMiddleware` added to `src/main.py`, allowing the FE's Lovable-sandboxed dev
  origin `http://localhost:8080` (pinned by `@lovable.dev/vite-tanstack-config`, not
  configurable via the FE's own `vite.config.ts`). Credentials disabled — Phase-0 header-stub
  auth carries no cookies.
- **Payload typing fix** — `verticals/es/workflows/market_matching/router.py`'s
  `ReviewItemOut.payload` was `dict[str, object] | None`; retyped to the real
  `MarketMatchingPayload` so the FE's OpenAPI-generated client gets accurate types instead of
  `Record<string, unknown>`. No behavior change.
- **FE-side (Insurance OS repo)**: `.env.local`/`.env.example` (`VITE_API_BASE_URL` + demo
  header-stub auth stand-in — `demo-es` / `demo-es-junior` / `junior`, the seeded ES tenant from
  `core/seed.py`), `src/lib/api/schema.d.ts` (generated via `openapi-typescript` against
  `/openapi.json`), `src/lib/api/client.ts` (single fetch wrapper: attaches the 3 stub headers,
  unwraps FastAPI's `{"detail": ...}` into a plain `Error`), `src/lib/api/marketMatching.ts`
  (typed list/detail/run/act calls).
- **Screen rewrite**: `SubmissionMarketMatching` + its 5 tabs now use React Query against the
  real API. Sections the API has no data for (document list/citations, per-carrier
  hard-exclusion/soft-score breakdown, LLM narrative, per-state diligent search, submission
  metadata like insured name/TIV/premium) show an explicit "not available yet" panel naming the
  missing endpoint/field, rather than fabricated numbers.

**Key decisions/deviations**
- Chose to wire only what the API actually returns and stub the rest explicitly, rather than
  expanding the ES schema/service to backfill the FE mock's full field set (narrative+citations,
  per-carrier requirements checklist, capacity/premium/turnaround, per-state diligent search) —
  a bigger, separate piece of work if wanted later.
- Inbox lists the real Workflow_10 fixture submissions (`submission_01..06`) via a "Run batch
  matching" button that POSTs `/run` for any not yet processed — there's no submission-upload
  endpoint, so this is the closed set the FE can drive today.

**Verification**
- Backend: `ruff check .` / `mypy src` clean; `pytest tests/test_es_market_matching.py` 9/9 pass.
- Frontend: `tsc --noEmit` clean; `eslint` clean (one pre-existing, unrelated `no-explicit-any`
  in a shared `Button` helper, untouched); `npm run build` succeeds.
- Dataset alignment confirmed: `Data sets/Workflow 1/market_matching_dataset` is byte-identical
  to the `TEST_DATA_ROOT/Workflow_10/test_dataset` fixtures the backend actually reads (only an
  added doc file differs). All 6 submissions' live API output matches the dataset README's
  documented "Expected Ranking Output" exactly, including both edge cases (partial-fit ranking
  on #01, true zero-match + diligent-search flag on #06).
- Manual round-trip: listed → ran all 6 fixtures → detail fetch → `escalate` succeeded (200,
  status updated) → `override` correctly 403'd for the junior demo role.

FE integration code lives in the `Insurance OS` repo: `src/lib/api/{client,marketMatching,schema.d}.ts`
and `src/components/app/Workflows.tsx` (`SubmissionMarketMatching` + its tabs).

### Addendum — Documents tab wired to real fixture content (2026-07-24)

The Documents tab was originally stubbed "not available yet": the pipeline reads each
submission's real files (`acord_application.txt`, `email.txt`, `financial_statement.txt`,
`loss_run.txt`, and `sov_report.txt` where present) via the connector to run extraction, but
`MarketMatchingPipeline.ingest()` never persisted them anywhere, and no route returned them.

**What changed**
- `service.py`'s `ingest()` now saves every document in the `RawBundle` via the pipeline's
  existing (previously-unused) `DocumentStore` — one `Document` row per file, keyed by
  `submission_id`, using the already-frozen Phase-1 `core.documents` interface. No new module,
  no contract change.
- `router.py`: added `GET /api/es/market-matching/{item_id}/documents` → real filename, kind, and
  full raw content per document (small `.txt` fixtures, so returning full text is fine — no
  pagination needed).
- FE `DocumentsTab` now fetches and renders this list with a real content viewer, replacing the
  stub. It shows raw content only, not field-level citations (extraction doesn't track
  per-character source offsets, so there's no highlighted-field view like the original mock).

**Data backfill:** the 6 fixture submissions were already run (as review items) before this
change, so a one-off script re-ingested documents for `submission_01..06` under `demo-es`,
writing only `Document` rows (keyed by `submission_id`) — no `review_item`/`output_package`
rows were touched, so existing statuses (e.g. escalated) were preserved. Any future `/run` call
persists documents automatically — no backfill needed going forward.

**Verification:** `ruff`/`mypy` clean; `pytest tests/test_es_market_matching.py` 9/9 still pass;
confirmed live via `GET .../documents` for all 6 submissions (4-5 real documents each, matching
the dataset's per-submission file lists exactly, e.g. `submission_02` is the one with the extra
`sov_report.txt`).

## Phase 3 — E&S Package Assembly ✅ (2026-07-24)

**Scope:** second E&S workflow, `verticals/es/workflows/package_assembly/**` only. No
migration (reuses `Decision`/`OutputPackage`/`AuditEntry` — see deviations below);
`core/common` and `verticals/mga/**` confirmed untouched via `git status`.

**What was built**
- `scenario_loader.py` — loads `Workflow_11/test_dataset/scenario_*/market_matching_output.json`
  (E&S-owned fixture access; the shared `fixtures.loader` glob doesn't match `scenario_*` and
  has no notion of this JSON shape). Normalizes single-carrier vs. multi-carrier (`carriers: []`)
  input into one flat per-carrier view.
- `submission_resolver.py` — resolves a scenario's `named_insured` back to its real Workflow_10
  submission (dynamic match, not a hardcoded table) and re-derives its `ExtractedModel` via the
  same shared `core.extraction.DefaultExtractionService` — a documented, deliberate exception to
  the PRD's "no re-extraction" (FR-1): Market Matching doesn't persist `ExtractedField` rows
  anywhere queryable, and the scenario JSON alone lacks field-level values a real SOV has (e.g.
  Scenario 04 needs `Year Built`/`Construction`/etc., verified against the actual Oakwood SOV
  text).
- `assembly.py` — PA-01..PA-06 native (Option-A: nothing here fits the generic 6-check engine —
  document-family fuzzy matching across quality-descriptor variants like "audited" vs. "reviewed"
  financials, loss-run-year arithmetic, the PA-02 auto-fill grounding boundary, three-state
  status derivation). PA-07 (precedence ordering) is architectural only, not exercised by the
  sample dataset.
- `service.py` — `PackageAssemblyPipeline` implementing `WorkflowPipeline[OutputPackage]`. Unlike
  Market Matching, this workflow ingests no raw documents — `ingest()`/`extract()` load the
  scenario + resolve the real `ExtractedModel` instead of pulling from a connector inbox. One
  fresh pipeline instance per (submission, carrier) pass — per-run state stashed on `self`.
- `router.py` — `/api/es/package-assembly`: `run` (fans out one independent pass per selected
  carrier, FR-2/FR-23), list, detail, `approve`/`edit`/`send`. Adds a check beyond
  market_matching's parity: `approve`/`send` on a `BLOCKED` package → HTTP 409 (FR-10), and every
  action writes an `AuditEntry` (FR-21) — market_matching's router doesn't do the latter today.
- `tests/test_es_package_assembly.py` — all 6 real Workflow_11 scenarios, including Scenario 04
  as a **mandatory, non-skippable release-gate case** per the PRD's own risk register.

**Dataset:** `Workflow_11` (E&S · Package Assembly — added to DATA_AND_FIXTURES.md's mapping
table), copied from `Data sets/Workflow 2/package_assembly_dataset/` into
`TEST_DATA_ROOT/Workflow_11/test_dataset/` unchanged, plus an authored
`Validation_Rules_Test_Dataset.md`.

**Key decisions / deviations (pre-approved)**
- Folder location: `verticals/es/workflows/package_assembly/` (matching FOLDER_STRUCTURE.md and
  market_matching's actual location), not the flatter `verticals/es/package_assembly/` the task
  text literally said.
- No new migration: PRD §7.2's Package Output Schema fits entirely in `OutputPackage.payload`;
  FR-21's broker-edit logging is exactly what `AuditEntry` already does.
- Gap-vs-block policy (PA-03/FR-9) is a small, evidence-based v1 default — not inlined in any
  scenario JSON — global **block**, one carrier override (Vantage/CAR-06 → **disclose** for a
  missing non-standard document type). Reproduces all 6 scenarios exactly.
- `DecisionOutcome` mapping (defined in this workflow, not `core/common`): `READY`/
  `READY_WITH_GAP` → `PROCEED`, `BLOCKED` → `REQUEST_INFO`.
- Supplemental-form auto-fill also checks the extraction service's `<kind>.locations[i].<field>`
  nested shape (discovered live, not assumed) — the real SOV extraction groups some fields
  (`year_built`, `sprinklered`) under a `locations` list rather than flat `sov.<field>` keys.
  `unit_count_estimate_from_TIV_and_class` correctly never resolves either way — no source
  exists for it in the real document, confirming the release-gate case is a genuine test, not a
  tautology.

**Verification:** `ruff check .` / `mypy src` clean (70 files); `alembic heads` still one head,
`alembic check` clean (no migration added, so no drift possible); app boots, `/api/core/health`
still `{"phase":0}`; `/api/es/package-assembly`, `/api/es/market-matching`, and
`/api/mga/submission-triage` all verified 200 live. All 6 scenarios pass, incl. Scenario 03's
per-carrier independence (same submission → Ironclad READY, Meridian BLOCKED) and Scenario 04's
auto-fill boundary. Live end-to-end `POST /run` exercised manually against Scenario 04, matching
the pytest assertions exactly. `core/common` and `verticals/mga/**` byte-identical (`git status`
shows zero changes in either).

FE wiring spec: `docs/FE_CONTRACT_package_assembly.md`.

### Addendum — Frontend Integration (2026-07-24)

Wired `Insurance OS`'s `/app/workflows/package-assembly` screen to this workflow, following the
same pattern as Market Matching's FE integration (see that phase's own addendum above).

**What changed**
- `router.py`: same payload-typing fix as market_matching's — `ReviewItemOut.payload` was
  `dict[str, object] | None`, retyped to the real `PackageAssemblyPayload`. No behavior change.
- FE: `src/lib/api/packageAssembly.ts` (typed run/list/detail/approve/edit/send calls, reusing
  `client.ts`), and `PackageAssembly` + `CarrierPackageCard` in `Workflows.tsx` rewritten to use
  real data via React Query — document checklist, supplemental form (`auto_filled: false` fields
  render as empty manual inputs, never pre-populated, per FR-2's grounding guarantee),
  diligent-search flag, blocking/gap items, cover letter, and status log all real. Approve/send
  buttons disable with a tooltip when `status === "BLOCKED"` (client-side UX only — the real
  guard is the backend's 409, which the FE surfaces as a toast either way).

**One naming collision caught during regeneration:** once both `market_matching/router.py` and
`package_assembly/router.py` define their own `ReviewItemOut`/`RunRequest` classes, FastAPI
qualifies both with their full module path in the OpenAPI schema (plain `ReviewItemOut` no
longer exists). `marketMatching.ts`'s type export was updated to the qualified name so it kept
compiling — a real regression risk to already-working Market Matching code, caught by re-running
`tsc` immediately after regenerating the schema, before writing any Package Assembly code.

**Key decision:** Package Assembly's fixture scenarios (`scenario_01..06`, `Workflow_11`) use
their own synthetic submission IDs with a carrier selection already baked in per scenario — they
don't correspond to Market Matching's live `submission_01..06` runs today (that link is an
explicit "becomes real in production" placeholder per the FE contract). So the screen gets its
own scenario-driven inbox (run `scenario_01..06` directly), mirroring Market Matching's fixture
inbox pattern. Market Matching's "Proceed to package" navigation still works exactly as before
(unchanged) and now shows an informational banner on arrival, rather than pretending to drive
data it structurally can't.

**Verification:** `ruff`/`mypy` clean (70 files); `pytest tests/test_es_package_assembly.py` 7/7
pass; `tsc`/`eslint`/`npm run build` all clean. Live: ran all 6 scenarios — results matched the
backend's own eval exactly (scenario_02 BLOCKED, scenario_03's per-carrier independence
reproduced live, scenario_04/05/06 READY, scenario_01 READY_WITH_GAP); confirmed `approve`/`send`
return 409 with the documented detail message on a BLOCKED package and succeed on non-blocked
ones. SSR smoke test across all 18 frontend routes (both live workflows + the 9 still-mocked
screens + marketing site) returned 200 with no errors — Market Matching's live data and every
other screen's mock behavior confirmed unchanged.
>>>>>>> Stashed changes
