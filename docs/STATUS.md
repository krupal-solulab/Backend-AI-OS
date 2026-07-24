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

## Phase 3 — E&S Retail Agent Communication Copilot ✅ (2026-07-24)

**Scope:** third E&S workflow, `verticals/es/workflows/agent_communication/**` only. No
migration (reuses `Decision`/`OutputPackage`/`AuditEntry`, same as Package Assembly);
`core/common` and `verticals/mga/**` confirmed untouched via `git status`.

**What was built**
- `drafting.py` — the native Option-A engine: deterministic `trigger_type` classification
  (reading the field the upstream object already carries — PRD's own §5.2 mapping, not an
  inferred classifier), RA-TN tone/framing instruction selection per type, the compliance-gate
  determination (`requires_compliance_review` true only for `NO_MARKET_FOUND`, per RA-TN-06),
  `carrier_names_disclosed` (false only for that same type — every other type names its
  carrier(s) by design), and native subject-line templates for 5 of the 6 types. Nothing here
  fits the generic 6-check rules engine — it's routing logic, not a data-driven check.
- `subject_resolver.py` — FR-10's thread-reuse mechanic for `NO_RESPONSE_FOLLOWUP`: looks up the
  actual persisted prior draft's subject line in this workflow's own history first (DB lookup by
  `submission_id` + original `trigger_type` + carrier), falling back to a deterministic
  `"Re: ..."` reconstruction only if no matching prior draft exists.
- `service.py` — `AgentCommunicationPipeline`. Unlike either prior E&S workflow, this one does no
  fresh extraction/ingestion at all — its input is already the fully structured output of an
  upstream workflow or a manually-logged trigger (PRD §1/FR-2); `ingest()`/`extract()` are a thin
  pass-through. Takes an `AsyncSession` in its constructor (like Market Matching, unlike Package
  Assembly) since `decide()` needs it for the subject-line lookup. Every `DecisionOutcome` maps to
  `PROCEED` — the FE-facing gating signal is `payload.requires_compliance_review` directly, not
  the decision outcome (avoids a redundant second signal, per the pre-build discussion).
- `router.py` — `/api/es/agent-communication`: `run`, list, detail, `approve`/`edit`/`send`, plus
  two genuinely new endpoints working around frozen-enum gaps *without touching core/common*:
  - `POST /{id}/discard` — `ReviewStatus` has no "discarded" value, so this flips the workflow's
    own `payload.status` instead of routing through `review_queue.act()`.
  - `POST /{id}/compliance-clear` — `ReviewAction` has no "compliance sign-off" value; senior/admin
    only, flips `payload.requires_compliance_review` directly, logged to the audit trail.
  Also enforces two dedup rules server-side, not left as FE conventions: **FR-5** (no duplicate
  draft for an unresolved trigger — returns the existing item with `deduplicated: true`) and
  **FR-12** (at most one `NO_RESPONSE_FOLLOWUP` ever per original request — HTTP 409 on a second
  attempt, checked across ALL statuses, not just pending).
- `tests/test_es_agent_communication.py` — all 6 real Workflow_12 triggers (structural/behavioral
  assertions, not literal-text matching against `expected_draft.txt`, since LLM/mock-LLM phrasing
  varies), plus dedicated tests for FR-5, FR-12, and the full compliance-gate flow (blocked →
  junior clear forbidden → senior clear → approve succeeds).

**Dataset:** `Workflow_12` (E&S · Retail Agent Communication — added to
DATA_AND_FIXTURES.md's mapping table), copied from `Data sets/Workflow 3/retail_comm_dataset/`
into `TEST_DATA_ROOT/Workflow_12/test_dataset/` unchanged.

**Key decisions / deviations (pre-approved)**
- Compliance-gate clearance: a dedicated router-only `compliance-clear` endpoint rather than
  reusing `ReviewAction.OVERRIDE`'s existing (unrelated) meaning.
- FR-10 subject-line reuse: DB lookup of the actual prior draft, with a deterministic fallback —
  not always-deterministic reconstruction (more correct when a prior draft genuinely exists,
  degrades gracefully when it doesn't).
- `DecisionOutcome`: all `PROCEED` — `payload.requires_compliance_review` is the FE's real gating
  signal, avoiding a redundant second one.
- No migration — same reasoning as Package Assembly.
- Subject lines are generated **natively** (templated), not by the LLM — consistent with PRD §8's
  own "tone/framing selection is deterministic routing, not an LLM judgment call" principle; the
  LLM only drafts the free-form body.
- Scope note: PRD FR-15 names exactly three broker actions (Approve & Send / Edit then Send /
  Discard) — this router implements exactly that set (`approve`, `send`, `edit`, `discard`) and
  does not add `override`/`escalate`/`issue` endpoints, since nothing in this PRD calls for them.

**Verification:** `ruff check .` clean; `mypy src` clean (78 files); `pytest
tests/test_es_agent_communication.py` 11/11 pass; full E&S suite (market_matching +
package_assembly + agent_communication) 27/27 pass together. `alembic heads` still shows one
head; `alembic check` reports no drift. `core/common`/`verticals/mga` confirmed byte-identical
via `git status`. Live: app boots, `/api/core/health` → `{"phase":0}`, all four vertical route
groups (`market-matching`, `package-assembly`, `agent-communication`, `submission-triage`) return
200. Live end-to-end `POST /run` against real Trigger 04 verified against the mock LLM's actual
output. Live compliance-gate flow exercised end to end: `approve` on a fresh No Market Found
draft → 409 → junior `compliance-clear` → 403 → senior `compliance-clear` → 200 → `approve` → 200.

**Incidental fix:** `docs/STATUS.md` had a leftover, never-cleaned-up git stash-conflict marker
(`<<<<<<< Updated upstream` / `=======` / `>>>>>>> Stashed changes`, spanning lines 162-354) from
an earlier session — the "upstream" side was empty and the "stashed" side held all the real
Market Matching FE-integration content, so resolving it was just removing the three marker lines
(zero content lost). Flagging this rather than silently leaving broken markdown in the repo.

### Addendum — Frontend Integration (2026-07-24)

Wired `Insurance OS`'s `/app/workflows/agent-copilot` screen to this workflow — with one
structural difference from Market Matching/Package Assembly's FE integrations, explained below.

**What changed**
- FE: `src/lib/api/agentCommunication.ts` (typed run/list/detail/approve/edit/send/discard/
  compliance-clear calls, reusing `client.ts`), embedding the 6 real `Workflow_12` trigger
  objects verbatim (there's no fixture-ref shorthand at the API level here — `POST /run` takes
  the full trigger object, unlike `submission_ref`/`scenario_ref` — so the FE-known "ref" is the
  whole JSON body, copied byte-for-byte from `trigger_XX/trigger_input.json`).
- Added a new `LiveDraftsSection` to the existing (otherwise-untouched) `RetailAgentCopilot`
  screen: run any of the 6 triggers, real inbox, real detail (subject/body, persistent
  compliance-review banner, carrier-disclosure indicator, grounding citations, status badge),
  real approve/send/discard/compliance-clear actions with real 409/403 enforcement.

**Key decision — additive, not a replacement:** unlike the other two workflows, this screen's
existing mock is a chat-thread UI that's also the live hand-off target for 3 OTHER still-mocked
screens (Quote Comparison, Binder & Issuance, Endorsement Processing) via query params
(`carrier`/`premium`/`insured` only). The real backend's 6 trigger types need far more structured
data than those thin params carry (agent name, agency, submission id, limits, loss context), and
2 of the 4 mock hand-off kinds (`policy-docs-delivered`, `endorsement-confirmed`) have no matching
real trigger type at all — bridging that hand-off to the real API would mean fabricating fields.
So the existing chat-thread mock and all 3 hand-offs are left completely untouched, and the real
review-queue experience was added as a separate, clearly-labeled section on the same page instead
of replacing the mock.

**Verification:** `ruff`/`mypy` clean (78 files); `pytest` 27/27 across all three E&S workflows;
`tsc`/`eslint`/`npm run build` all clean. Live: ran all 6 triggers — Trigger 03 (No Market Found)
correctly gated (`requires_compliance_review: true`, `carrier_name: null`,
`carrier_names_disclosed: false`); full compliance-gate flow reproduced live (409 → junior 403 →
senior 200 → approve 200); re-running Trigger 04 correctly deduplicated (FR-5, same item
returned); re-running Trigger 05 correctly 409'd (FR-12, one follow-up max). SSR smoke test
across all 18 frontend routes plus both Agent Copilot hand-off query-param variants returned 200
with no errors — the chat-thread mock and its 3 cross-screen hand-offs confirmed unchanged.

**Also restored:** `main.py`'s CORS middleware and `market_matching/router.py`'s
`MarketMatchingPayload` typing had reverted to their pre-integration state on disk (main.py had
no `CORSMiddleware` at all — confirmed live, an OPTIONS preflight from the FE origin returned 405
instead of the expected 200 with CORS headers). Since these are required for the already-shipped
Market Matching/Package Assembly FE integrations to reach the backend at all, both were
re-applied — same edits as documented in this file's earlier addenda, re-verified live.

## Phase 4 — Agent Communication Auto-Trigger (2026-07-24) ✅

**Scope:** closes the gap where `agent_communication` was fully built but only reachable via a
manually-POSTed trigger body. New file `verticals/es/agent_communication_hooks.py` (one new
module, cross-workflow by necessity) + **one additive line each** in `market_matching/router.py`
and `package_assembly/router.py`'s existing `run_*` handlers. `core/common`, `verticals/mga/**`,
and `agent_communication`'s own pipeline/router/drafting logic are all untouched — the hook calls
that logic (imports `AgentCommunicationPipeline` and reuses the router's own `_find_pending_duplicate`
FR-5 helper directly) rather than reimplementing any of it.

**What auto-fires now, and why not more:**
- **`NO_MARKET_FOUND`** — from `market_matching`'s own `/run`, when `Decision.outcome is DECLINE`
  (the precise true-zero-match signal; distinct from `REQUEST_INFO`, the missing-ACORD case, which
  must NOT fire this).
- **`SUBMISSION_ACKNOWLEDGMENT`** (status `READY`) / **`MISSING_INFO_REQUEST`** (status `BLOCKED`
  or `READY_WITH_GAP`) — from `package_assembly`'s own `/run`, **one per carrier**, not combined
  across a submission's multiple carriers. The PRD's own Trigger 01 sample combines carrier
  statuses into one draft, but that needs tracking "how many carriers were selected" and waiting
  for all of them to be packaged — real new infrastructure, not a mechanical extension of a thin
  hook, and explicitly deferred (approved decision, not a silent simplification).
- **`NO_RESPONSE_FOLLOWUP`** — explicitly **out of scope this pass**. FR-11 makes this
  elapsed-time-vs-carrier-acceptance-window monitoring, which needs an Arq periodic job (materially
  different infrastructure than a synchronous post-run hook), not a mechanical addition here.
- **`QUOTE_TERMS_SUMMARY` / `PLACEMENT_CONFIRMATION`** — stay manual permanently per FR-2, until
  Quote Comparison exists. Untouched; still reachable via the FE's existing `FIXTURE_TRIGGERS`.

**Key finding from tracing the data (not assumed):** neither `MarketMatchingPayload` nor
`PackageAssemblyPayload` carries `named_insured` — both pipelines resolve `acord.named_insured`
internally (confirmed in `package_assembly/submission_resolver.py`) but never return it past their
own `pipeline.run()`. Per the "only from data these workflows already produce, no new extraction"
constraint, this was **not** fixed by adding new payload fields — accepted as a v1 gap. Auto-fired
drafts get `drafting.py`'s already-existing graceful fallback (a generic `"Submission - ..."`
subject line) instead of naming the insured; verified this degrades quality, not correctness —
`build_facts()`/`_subject_line()` already treat every trigger field as optional.

**No-throw guarantee:** every hook function wraps its own body in `try/except Exception:
log.exception(...)` and returns `None` either way — a drafting failure is logged and swallowed,
never raised into `market_matching`'s or `package_assembly`'s own response.

**Verification:**
- `ruff check .` / `mypy src` clean (79 files); `pytest` — all 3 E&S suites, **27/27 unmodified**;
  `alembic check` clean, single head (no migration, none expected); no files under `core/common`
  or `verticals/mga` touched.
- Live: ran `market_matching` on `submission_06` (GreenLeaf, true zero-match) → a
  `NO_MARKET_FOUND` draft appeared via `GET /api/es/agent-communication` with **no manual trigger
  POST** (`requires_compliance_review: true`, `carrier_names_disclosed: false`, subject line
  correctly falls back to `"Submission - Market Search Update"`). Ran `package_assembly` on
  `scenario_01` (`READY_WITH_GAP`) → auto-fired `MISSING_INFO_REQUEST` scoped to the correct
  carrier (Vantage Excess Partners). Ran `scenario_06` (`READY`) → auto-fired
  `SUBMISSION_ACKNOWLEDGMENT`. Re-running a scenario whose submission already had an unresolved
  draft correctly deduplicated (FR-5) — no second item created, confirming the reused dedup
  helper works identically for auto-fired and manually-fired paths.
- Confirmed `market_matching`'s and `package_assembly`'s own `/run` responses are byte-identical
  in shape to before this change (same keys, same structure) — the hook is invisible to existing
  callers except for the side effect of a new item appearing in `agent_communication`'s list.
- Frontend: **zero code changes needed**, confirmed live rather than assumed — `GET
  /api/es/agent-communication` is a generic per-tenant list, and the auto-fired items above
  appeared in it exactly like manually-fired ones (same `ReviewItemOut`/`DraftCommunicationOut`
  shape). `tsc`/`eslint`/`npm run build` re-confirmed clean (no FE files touched this phase). SSR
  smoke test across all 18 frontend routes returned 200 with no errors.

Not committed in either repo — ready for review.
