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

## Phase 3 — E&S Quote Comparison & Recommendation Copilot ✅ (2026-07-27)

**Scope:** fourth E&S workflow, `verticals/es/workflows/quote_comparison/**` — the biggest build
in the vertical so far per the PRD's own warning (genuinely new extraction target + comparison
logic, not a reuse of any prior workflow's pattern). One deliberate, narrow addition outside that
folder: a new function in the existing `verticals/es/agent_communication_hooks.py` (FR-20's
downstream handoff). No migration; `core/common` and `verticals/mga/**` confirmed untouched via
`git status`.

**What was built**
- `quote_parser.py` — native carrier-response-email parser. Deliberately NOT
  `core.extraction.DefaultExtractionService`: that service parses `Label: value` strictly per
  line, which would silently truncate this dataset's own most important field — subjectivity
  clauses that wrap across lines (verified against the real fixture text, not assumed).
  Declinations (pure prose, no Key:Value structure) and multi-value lines ("Deductible: $25,000
  (all perils), Wind/Hail: $100,000") also don't fit the shared extractor. Classifies
  QUOTE vs DECLINATION (presence of a `Premium:` line), splits/classifies subjectivities as
  routine vs. material (deadline/dependency/outstanding-underwriting-info keyword heuristics,
  validated against all 6 scenarios' expected outcomes), classifies endorsement basis, and
  extracts declination reasons + the dollar figure driving them.
- `comparison_engine.py` — QC-01 (comparability: limits + both deductible types + every
  endorsement's basis must match exactly, never premium alone), QC-03 (declination consistency,
  lightweight per the approved plan — reuses Market Matching's existing
  `load_carrier_panel(10)`/`severity_ceiling` data, no new infrastructure), QC-06 (mode
  selection: `SINGLE_RECOMMENDATION` / `MULTI_OPTION` / `SINGLE_QUOTE_URGENT` /
  `SINGLE_QUOTE_ROUTINE`), QC-07 (validity/urgency — a material subjectivity's own dependency can
  drive urgency independent of the validity window, per FR-16). Also
  `recompute_urgency_from_payload` — a pure, read-time-only projection (no DB write) recomputing
  urgency against today's date on every `GET`, deferring real scheduled-job infrastructure (same
  call made for Agent Communication's `NO_RESPONSE_FOLLOWUP`).
- `service.py` — `QuoteComparisonPipeline`. Unlike Package Assembly/Agent Communication, this
  workflow DOES ingest raw documents and DOES run real extraction (just a native one). Resolves
  an explicit `as_of` reference date from `WorkflowInput.params` (fixture/test determinism) or
  defaults to the real current date in production.
- `router.py` — `/api/es/quote-comparison`: `run`, list, detail (read-time recompute),
  `select/{quote_id}` (fires the Agent Communication handoff — see below),
  `request-revised-terms`/`mark-lapsed` (FR-23's other two logged-only broker decisions).
- `agent_communication_hooks.fire_quote_comparison_result` — extends the existing hook module
  with one new function. Deliberately fires from quote_comparison's `/select/{quote_id}`, NOT
  its `/run` — per the PRD's own FR-23 ordering ("broker marks which quote(s) to present...
  which feeds FR-20's downstream trigger"), the broker's selection is the trigger, not the
  system's recommendation by itself. `MULTI_OPTION` cases never auto-fire until the broker picks
  one — confirmed live (see Verification).
- `tests/test_es_quote_comparison.py` — all 6 real Workflow_13 scenarios (comparability,
  materiality, urgency, declination consistency), the read-time recompute function directly, and
  the full FR-20 handoff + MULTI_OPTION-doesn't-autofire behavior end to end.

**Dataset:** `Workflow_13` (E&S · Quote Comparison — added to DATA_AND_FIXTURES.md's mapping
table), copied from `Data sets/Workflow 4/quote_comparison_dataset/` into
`TEST_DATA_ROOT/Workflow_13/test_dataset/` unchanged.

**Key decisions / deviations (pre-approved)**
- `MULTI_OPTION` never auto-fires Agent Communication — only `SINGLE_RECOMMENDATION`/
  `SINGLE_QUOTE_*` cases have one clear quote to hand off, and even those wait for the broker's
  explicit `/select` action, not `/run`.
- QC-07's validity threshold: 5 business days (the PRD's own explicit placeholder default),
  approximated as calendar days for v1 (thresholds are placeholders pending real broker
  validation per every rules document in this project).
- QC-03: lightweight version built (declination_reason + consistent/inconsistent/
  unable_to_determine against existing carrier severity-ceiling data) — not skipped, since it
  reuses data that already exists and costs little.
- No migration — same reasoning as Package Assembly/Agent Communication.
- `carrier_id` is always `null` in v1 (carrier identity resolved by name only, via email-domain
  mapping) — flagged in the FE contract, not silently left unexplained.

**Bug found and fixed via live testing, not just unit tests:** the initial deductible parser
split on the first comma to separate "all perils" from "wind/hail" values — but
`"$25,000 (all perils), Wind/Hail: $100,000"` has a THOUSANDS-SEPARATOR comma inside the first
dollar figure itself, which corrupted the split (`"$25"` / `"000 (all perils), Wind/Hail:
$100,000"`). All 10 pytest tests passed anyway, because none of them asserted the actual
deductible VALUES — only comparability/mode outcomes, which happened to still be correct despite
the corrupted values. Caught immediately during live `curl` verification (the whole reason this
project's process requires live checks, not just green tests), fixed with a targeted
label-anchored regex instead of positional comma-splitting, and a new test assertion added
locking in the correct parsed values for both carriers in Scenario 02 so this bug class can't
silently regress again.

**Verification:** `ruff check .` / `mypy src` clean (87 files); `pytest
tests/test_es_quote_comparison.py` 10/10 pass; full E&S suite (market_matching + package_assembly
+ agent_communication + quote_comparison) 37/37 pass together. `alembic heads` still one head;
`alembic check` no drift. `core/common`/`verticals/mga` byte-identical via `git status`. Live: app
boots, all four vertical route groups + MGA's return 200. Live-verified Scenario 02's deductible
values after the fix (`$25,000`/`$100,000` and `$10,000`/`$50,000`, matching the source emails
exactly). Live-exercised the full FR-20 handoff: `select`-ing a quote created a new pending item
in `agent-communication`'s list (12 → 13); confirmed `MULTI_OPTION`'s `/run` alone does NOT
create one. Live-exercised `mark-lapsed` (`payload.status` → `"LAPSED"`). Frontend: not touched
this phase (no FE work requested for this workflow yet).

### Addendum — Frontend Integration (2026-07-27)

Wired `Insurance OS`'s `/app/workflows/quote-comparison` screen to this workflow — same additive
pattern as Agent Communication's FE integration (not a full replace), for an analogous reason.

**What changed**
- FE: `src/lib/api/quoteComparison.ts` (typed run/list/detail/select/request-revised-terms/
  mark-lapsed calls, reusing `client.ts`), with `FIXTURE_SCENARIOS` — just `{ref, label}` pairs
  this time, not embedded JSON (unlike Agent Communication's triggers, `POST /run` here only
  needs `scenario_ref`, same shorthand pattern as Market Matching/Package Assembly).
- Added a new `LiveComparisonsSection` to the existing (otherwise untouched) `QuoteComparison`
  screen: run any of the 6 `Workflow_13` scenarios, real inbox, real detail — mode badge
  (`SINGLE_RECOMMENDATION`/`MULTI_OPTION`/`SINGLE_QUOTE_URGENT`/`SINGLE_QUOTE_ROUTINE`),
  comparability banner, per-quote subjectivities split by real `materiality` (`routine`/
  `material` — the mock's 3-tier Standard/Material/Deal-breaker per-quote model doesn't match
  the real per-subjectivity 2-tier one, so the live section uses the real values directly rather
  than remapping them), urgency flags, declination display, and real actions.

**Key decision — additive, not a replacement (same reasoning as Agent Communication):** the
existing mock's "Present to retail agent" button is a real, working hand-off into Agent
Copilot's mock chat-thread UI (`trigger=quote-summary`) that must keep working unmodified. The
real backend's hand-off is architecturally different in a way that makes replacing the mock
button pointless anyway: selecting a quote in the live section fires the real Agent
Communication draft **directly, server-side** (`POST .../select/{quote_id}`) — there's no
navigation involved at all, unlike the mock's client-side route change. So both stay side by
side: the mock table/recommendation/hand-off exactly as before, plus a separate real section
below it whose own "Present this quote" action does the actual FR-20 handoff in one call.

**Verification:** `ruff`/`mypy` clean (87 files); `pytest` 37/37 across all four E&S workflows;
`tsc`/`eslint`/`npm run build` all clean. Live: ran Scenario 02 — confirmed `MULTI_OPTION`,
`directly_comparable: false`, Harbor $74,000 / Coastal $81,500 matching the FE_CONTRACT sample
exactly; selected Harbor's quote and confirmed a real `QUOTE_TERMS_SUMMARY` draft appeared in
Agent Communication's list with correct `named_insured`/`carrier_name`/subject line/grounding
citations, not deduplicated. SSR smoke test across all 18 frontend routes returned 200 with no
errors — the mock table and its hand-off into Agent Copilot confirmed unchanged.

## Phase 3 — E&S Binder & Policy Issuance Coordination Copilot ✅ (2026-07-27)

**Scope:** fifth E&S workflow, `verticals/es/workflows/binder_issuance/**` — closes the full
placement lifecycle (submission → match → package → quote → compare → bind and issue). Two
deliberate, PRD-mandated additions outside that folder (both explicitly called for by FR-19, not
scope creep): one new function in `agent_communication_hooks.py`, and a 7th trigger type
(`POLICY_DOCUMENTS_DELIVERED`) added to `agent_communication/drafting.py`. No migration;
`core/common` and `verticals/mga/**` confirmed untouched via `git status`.

**What was built**
- `bind_parser.py` — native parser for the workflow's TWO new extraction targets: carrier bind
  confirmation emails and issued-policy declarations pages (the latter has no email headers at
  all — a third, structurally distinct format). Independent of Quote Comparison's own
  `quote_parser.py` (no cross-import), per the approved plan — the declarations-page format
  alone is different enough that a shared implementation wouldn't fully unify the two anyway.
- `coordination_engine.py` — BI-01 (bind-order fidelity), BI-02 (pre-bind blocking — absence of
  `lifecycle_stage` means implicitly `PRE_BIND`, inherited as-is from Quote Comparison's QC-02,
  never re-classified), BI-03/BI-05 (field-by-field reconciliation — premium, limits [compared
  by NUMERIC SIGNATURE, not exact phrasing, so a synthesized "Property $X; GL Y" string
  correctly reconciles against a single descriptive line covering the same figures], both
  deductible types, effective date [normalized to real `date` objects before comparing — a
  same-date-different-format pair, e.g. ISO vs MM/DD/YYYY, must never falsely mismatch]), BI-04
  (issuance monitoring against the carrier's OWN stated timeline, upper-bound used when a range
  is given, e.g. "30-45 days"), BI-07 (post-bind ongoing obligations — due date computed as
  `effective_date + (N-1)` days for a "within N days" clause, verified by hand against the
  dataset's own worked example: 60 days from an 08/01/2027 binding → due 09/29/2027, not 09/30,
  confirming "within N days" means before N full days elapse, not N days later).
  `recompute_live_state` — read-time-only projection (no DB write) for BI-04/BI-07, same deferred
  "no new scheduler" pattern as Quote Comparison's QC-07 — now the THIRD instance of this
  deferral in the vertical (FR-26 flags shared infra as overdue; tracked honestly, not silently
  repeated a third time).
- `service.py` — `BinderIssuancePipeline`. Branches on which of two genuinely different input
  shapes a scenario represents: pre-bind (`broker_bind_instruction.json` + optional
  `carrier_bind_confirmation.txt`) or post-issuance (`bind_record.json` + optional
  `issued_policy_document_extract.txt`).
- `router.py` — `/api/es/binder-issuance`: `run`, list, detail (read-time recompute),
  `resolve-confirmation-discrepancy`/`resolve-policy-discrepancy` (workflow-owned — FR-23's
  "Accept carrier's version"/"Flag as carrier error" don't map onto the frozen `ReviewAction`
  enum, per the approved plan), `escalate` (reuses the existing `ReviewAction.ESCALATE`, which
  already fits). Unlike Quote Comparison, Placement Confirmation/Policy Documents Delivered fire
  automatically from THIS workflow's own `/run`/resolve actions — the broker doesn't need a
  separate "select" step here, since BI-06's gate is "a verified-clean reconciliation," not "the
  broker chose."
- `agent_communication_hooks.fire_binder_issuance_result` — extends the existing hook module;
  fires whichever of the two triggers `payload["downstream_triggers_fired"]` already marks
  eligible (computed by binder_issuance's own service before this hook is ever called).
- `tests/test_es_binder_issuance.py` — all 6 real Workflow_14 scenarios, the read-time-recompute
  reminder function directly, and the full discrepancy → resolve → downstream-trigger-release
  flow for BOTH reconciliation stages end to end.

**Dataset:** `Workflow_14` (E&S · Binder & Policy Issuance — added to DATA_AND_FIXTURES.md's
mapping table), copied from `Data sets/Workflow 5/binder_issuance_dataset/` into
`TEST_DATA_ROOT/Workflow_14/test_dataset/` unchanged.

**Key decisions / deviations (pre-approved)**
- Scheduled monitoring: continue the deferred read-time-recompute pattern rather than building
  shared Arq infra this pass — FR-26's suggestion is tracked, not silently ignored, in this
  entry and the FE contract.
- FR-23's two non-mappable resolution actions: dedicated workflow-owned endpoints
  (`resolve-confirmation-discrepancy`/`resolve-policy-discrepancy`), not forced onto
  `APPROVE`/`OVERRIDE`'s existing (different) meanings.
- No migration — same reasoning as every prior E&S workflow.
- `bind_record`-stage scenarios (05/06) are treated as already-confirmed-clean by construction —
  no BI-03 discrepancy data exists at that later stage in this dataset, so
  `reconciliation_status` defaults to `CLEAN` there. Flagged explicitly in the FE contract as a
  v1 modeling simplification, not a bug.

**Bug found and fixed via live testing, not just unit tests:** `resolve-policy-discrepancy`
initially re-fired `placement_confirmation` in addition to `policy_documents_delivered`, because
the hook was passed the FULL stored payload (whose `placement_confirmation` flag was already
`true` from ingestion, per the `bind_record`-stage simplification above) rather than just the one
trigger being resolved. All 9 other pytest tests passed; only Scenario 06's end-to-end handoff
test caught it (`len(after) == 2`, not `1`) — fixed by passing a minimal single-trigger payload
snapshot into the hook instead of the whole dict.

**Verification:** `ruff check .` / `mypy src` clean (95 files); `pytest
tests/test_es_binder_issuance.py` 10/10 pass; full E&S suite (market_matching + package_assembly
+ agent_communication + quote_comparison + binder_issuance) 47/47 pass together. `alembic heads`
still one head; `alembic check` no drift. `core/common`/`verticals/mga` byte-identical via `git
status`. Live: app boots, all five vertical route groups + MGA's return 200. Live-verified
Scenario 03's discrepancy correctly suppresses Placement Confirmation, then
`resolve-confirmation-discrepancy` correctly releases it (agent-communication list: 13 → 13 → 14).
Live-verified Scenario 06's issued-policy discrepancy suppresses Policy Documents Delivered.
Live-verified Scenario 05's overdue alert (`expected_by_date: 2027-09-04`,
`overdue_alert_fired: true` at `as_of: 2027-09-25`) exactly matching the dataset's own "21 days
overdue" narrative. Frontend: not touched this phase (no FE work requested for this workflow yet).

### Addendum — Frontend Integration (2026-07-27)

Wired `Insurance OS`'s `/app/workflows/binder-issuance` screen to this workflow — same additive
pattern as Agent Communication/Quote Comparison's FE integrations, for the same reason.

**What changed**
- FE: `src/lib/api/binderIssuance.ts` (typed run/list/detail/resolve-confirmation-discrepancy/
  resolve-policy-discrepancy/escalate calls, reusing `client.ts`), with `FIXTURE_SCENARIOS`
  (`{ref, label}` pairs — `/run` only needs `scenario_ref`, same shorthand as Quote Comparison).
- Added a new `LiveBindersSection` to the existing (otherwise untouched) `BinderIssuance` screen:
  run any of the 6 `Workflow_14` scenarios, real inbox, real detail — bind-order status,
  visually distinct discrepancy banners for both the bind-confirmation and issued-policy stages
  with real resolve actions, overdue-issuance alert, post-bind ongoing obligations, and
  downstream-trigger-fired indicators.
- **New naming collision surfaced by this workflow**: `binder_issuance/schema.py` and
  `quote_comparison/schema.py` each define their own `SubjectivityOut` class. openapi-typescript
  qualified both with their module path once both existed in the same spec, which broke
  `quoteComparison.ts`'s existing plain `SubjectivityOut` reference — fixed by qualifying it
  (`verticals__es__workflows__quote_comparison__schema__SubjectivityOut`), same pattern already
  used for the per-router `ReviewItemOut`/`RunRequest` collisions. No behavior change, purely a
  generated-type reference fix.

**Key decision — additive, not a replacement (same reasoning as Agent Communication/Quote
Comparison):** the existing mock's `sendPlacementConfirmation`/`sendPolicyDocsDelivered` are the
real, working source of 2 of Agent Copilot's 4 mock hand-offs (`placement-confirmation`/
`policy-docs-delivered`) and had to keep working unmodified. The real backend's hand-off is
architecturally different anyway — Placement Confirmation/Policy Documents Delivered fire
automatically server-side the moment a reconciliation is verified-clean (on `/run` or on
resolving a discrepancy), no broker button or navigation involved. So both stay side by side.

**Verification:** `ruff`/`mypy` clean (95 files); `pytest` 47/47 across all five E&S workflows;
`tsc`/`eslint`/`npm run build` all clean. Live: ran Scenario 03 — confirmed `DISCREPANCY_FLAGGED`
with the exact deductible/effective-date mismatch from the FE_CONTRACT sample; resolved it with
`accept_carrier_version` and confirmed a real `PLACEMENT_CONFIRMATION` draft appeared in Agent
Communication's list, `bound_terms.deductible_all_perils` correctly reflecting the *reconciled*
$5,000 (the carrier's confirmed figure), not the originally requested $2,500. SSR smoke test
across all 18 frontend routes returned 200 with no errors — the mock panels and their hand-offs
into Agent Copilot confirmed unchanged.

## Phase 3 — E&S Endorsement / Mid-Term Change Processing Copilot ✅ (2026-07-27)

**Scope:** sixth E&S workflow, `verticals/es/workflows/endorsement/**`. One deliberate,
PRD-mandated addition outside that folder (per FR-16, same pattern as Binder & Issuance's
FR-19): an 8th trigger type, `ENDORSEMENT_CONFIRMED`, added to `agent_communication/drafting.py`
+ one new function in `agent_communication_hooks.py`. No migration; `core/common` and
`verticals/mga/**` confirmed untouched via `git status`.

**What was built**
- `endorsement_parser.py` — this workflow's two genuinely new parsing challenges: splitting a
  multi-part change request's free-text detail ("Add BOTH Midstate Distribution Co. AND
  Harborline Logistics...") into distinct items for EP-05's item-level reconciliation, and
  parsing the carrier's issued-endorsement email (a third new carrier-email shape — "Added as
  scheduled additional insured: X" lines, no premium/deductible focus). Everything else
  (named_insured, carrier, the structured change type/detail) arrives pre-extracted in
  `bound_policy_context.json` — no ACORD-style document extraction needed here.
- `classification_engine.py` — EP-01 (type-based classification first — additional-insured/
  address-correction types always routine, limit-increase/class-addition/location-addition types
  never purely routine regardless of size — THEN a materiality-within-type check for everything
  else, e.g. headcount changes: percentage AND absolute premium both must exceed threshold,
  verified against Scenario 06's control case — a 75% headcount increase stays ROUTINE because
  the account's $2,400 premium is too small in absolute terms), EP-02 (three-outcome appetite
  recheck — reuses Market Matching's `CarrierProfile` DATA layer directly, NOT its `matching.py`
  ranking logic; prefers `bound_policy_context.json`'s own embedded accepted/excluded class lists
  when present, falls back to the real Workflow_10 carrier panel otherwise — verified this
  actually differs per scenario by reading the real fixture files, not assumed), EP-05 (item-level
  list reconciliation — genuinely different shape from Binder & Issuance's scalar-field equality
  check), EP-06 (proration inputs computed uniformly from real dates — effective/expiration/
  reference date — regardless of which convenience fields a given scenario happens to embed;
  cross-checked against both scenarios' own stated day counts and matched exactly).
- `service.py` — `EndorsementPipeline`. Branches on which of two input shapes a scenario
  represents: pre-issuance (`bound_policy_context.json` + `endorsement_request_email.txt`) or
  post-issuance reconciliation (`endorsement_request_sent.json` + `carrier_issued_endorsement.txt`).
- `router.py` — `/api/es/endorsement`: `run`, list, detail, `resolve-discrepancy` (workflow-owned
  — FR-19's resolution actions don't map onto the frozen `ReviewAction` enum), `send`/`escalate`
  (reuse existing `ReviewAction.SEND`/`ESCALATE`, which already fit). `ENDORSEMENT_CONFIRMED`
  fires automatically from this workflow's own `run`/`resolve-discrepancy` actions, same pattern
  as Binder & Issuance.
- `agent_communication_hooks.fire_endorsement_result` — extends the existing hook module with the
  8th trigger type.
- `tests/test_es_endorsement.py` — all 6 real Workflow_15 scenarios, plus the full
  discrepancy → resolve → downstream-trigger-release flow end to end.

**Dataset:** `Workflow_15` (E&S · Endorsement / Mid-Term Change Processing — added to
DATA_AND_FIXTURES.md's mapping table), copied from
`Data sets/Workflow 6/endorsement_dataset/` into `TEST_DATA_ROOT/Workflow_15/test_dataset/`
unchanged.

**Key decisions / deviations (pre-approved)**
- "Request acknowledged" trigger: NOT built — re-scanned every FR (1-22); only FR-16 mandates
  something new. Section 2.2's mention is a scope-boundary description, not a build requirement.
- No migration — same reasoning as every prior E&S workflow.
- EP-02's appetite data source: embedded fields preferred, real carrier-panel fallback used —
  confirmed as a genuine per-scenario difference by reading the actual fixtures (Scenario 03
  embeds accepted/excluded lists directly; Scenario 04 does not and requires the real Coastal
  Mutual `CarrierProfile` lookup to confirm "apartment building management" covers the new
  location's habitational use).

**Bug found and fixed via the test's own literal-value assertion, not a live-only surprise:** the
initial item-splitter blanket-stripped trailing periods from every split item, intended to trim a
final sentence-ending period but instead truncating legitimate abbreviations too ("Midstate
Distribution Co." → "Midstate Distribution Co"). Caught immediately by the test asserting the
exact expected item list (not just reconciliation behavior, which would have passed either way
since the substring-match reconciliation check tolerates the truncation) — fixed by removing the
blanket strip entirely; only the surrounding regex's own stop-words are trimmed now.

**Verification:** `ruff check .` / `mypy src` clean (103 files); `pytest tests/test_es_endorsement.py`
9/9 pass; full E&S suite (market_matching + package_assembly + agent_communication +
quote_comparison + binder_issuance + endorsement) 56/56 pass together. `alembic heads` still one
head; `alembic check` no drift. `core/common`/`verticals/mga` byte-identical via `git status`.
Live: app boots, all six vertical route groups + MGA's return 200. Live-verified Scenario 03's
appetite-unknown surfaces correctly (never auto-approved/rejected). Live-verified Scenario 05's
partial reconciliation (`requested_items` has 2 entries, `issued_items` has 1, `DISCREPANCY_FLAGGED`,
trigger held) then `resolve-discrepancy` correctly releases it (agent-communication list: 15 → 15
→ 16). Frontend: not touched this phase (no FE work requested for this workflow yet).

### Addendum — Frontend Integration (2026-07-27)

Wired `Insurance OS`'s `/app/workflows/endorsement-processing` screen to this workflow — same
additive pattern as the three prior FE integrations, for the same reason.

**What changed**
- FE: `src/lib/api/endorsement.ts` (typed run/list/detail/resolve-discrepancy/send/escalate calls,
  reusing `client.ts`), with `FIXTURE_SCENARIOS` (`{ref, label}` pairs).
- Added a new `LiveEndorsementsSection` to the existing (otherwise untouched)
  `EndorsementProcessing` screen: run any of the 6 `Workflow_15` scenarios, real inbox, real
  detail — classification badge, the three-outcome appetite banner (within/outside/unknown,
  visually distinct for `APPETITE_UNKNOWN`), state-licensing-clarification flag, premium/proration
  inputs (never a confirmed figure), the drafted request text, item-level reconciliation
  discrepancy with real resolve actions, and downstream-trigger-fired indicator.
- **Third instance of the same naming collision**: `endorsement/schema.py` also defines its own
  `DiscrepancyOut` class (now three workflows — quote_comparison, binder_issuance, endorsement —
  each with their own same-named class the generator disambiguates once more than one is in the
  spec at once). Fixed `binderIssuance.ts`'s now-broken plain reference the same way as before
  (qualified by module path); wrote `endorsement.ts`'s reference pre-qualified from the start.

**Key decision — additive, not a replacement (same reasoning as the three prior FE
integrations):** the existing mock's `sendEndorsementConfirmed` is the real, working source of the
4th and final Agent Copilot mock hand-off (`endorsement-confirmed`) and had to keep working
unmodified. The real backend fires `ENDORSEMENT_CONFIRMED` automatically server-side on a
verified-clean reconciliation — no broker button or navigation involved, same as Binder &
Issuance's pattern. Both stay side by side. With this workflow, **all four of Agent Copilot's
mock hand-off sources now have a real counterpart living alongside them** (Quote Comparison,
Binder & Issuance ×2, Endorsement) — none of the mock hand-offs were touched in any of them.

**Verification:** `ruff`/`mypy` clean (103 files); `pytest` 56/56 across all six E&S workflows;
`tsc`/`eslint`/`npm run build` all clean. Live: ran Scenario 03 — confirmed `APPETITE_UNKNOWN`
with the exact detail text from the FE_CONTRACT sample; ran Scenario 05 — confirmed
`requested_items` (2, including the correctly-un-truncated "Midstate Distribution Co.") vs.
`issued_items` (1) producing `DISCREPANCY_FLAGGED`; resolved it and confirmed a real
`ENDORSEMENT_CONFIRMED` draft appeared in Agent Communication with correct `named_insured`/
`carrier_name`/subject line. SSR smoke test across all 18 frontend routes returned 200 with no
errors — all four mock panels and their hand-offs into Agent Copilot confirmed unchanged.

## Phase 3 — E&S Renewal Remarketing Copilot ✅ (2026-07-27)

**Scope:** seventh E&S workflow, `verticals/es/workflows/renewal_remarketing/**` — the last
workflow gated on real bound-policy data. No migration; `core/common` and `verticals/mga/**`
confirmed genuinely untouched via `git status` (zero new files, not just unmodified existing
ones).

**What was built**
- `remarket_engine.py` — RR-01/RR-02 (fresh native exposure/loss-history change detection — NOT
  a port from MGA Renewal Management, confirmed by direct inspection that workflow was never
  built: `verticals/mga/workflows/` is empty, no Workflow_2 fixture data exists anywhere on disk;
  this is native logic informed only by the PRD's description), RR-03/RR-07 (incumbent
  appetite-vs-actual-responsiveness — silence is itself a distinct signal, not merged into the
  appetite check), RR-04 (the central four-state trigger decision — `NO_REMARKET` /
  `LIGHT_REMARKET_CHECK` / `FULL_REMARKET` / `URGENT_REMARKET`, verified against all 6 scenarios
  to never collapse to binary: Scenario 02's disproportionate-pricing FULL, Scenario 03's
  explained-growth-plus-size-shift LIGHT, Scenario 04's silence-driven URGENT, and Scenarios
  01/06's NO_REMARKET are all genuinely distinct code paths, confirmed live), RR-06 (native
  2-offer comparability + exception-quote flagging — simple enough not to need Quote Comparison's
  full multi-quote engine), RR-08 (remarketing-history weighting — parses the ACTUAL fixture
  shape, a descriptive string, not the PRD schema's structured list; absence of history has no
  suppressive effect, per FR-11).
- `service.py` — `RenewalRemarketingPipeline`. Detects two input shapes from the data itself: a
  trigger-decision pass or a post-remarket comparison pass (Scenario 05's shape, via presence of
  `alternative_quote_received`).
- `router.py` — `/api/es/renewal-remarketing`: `run`, list, detail, `initiate-remarket` (RR-05 —
  genuinely re-invokes the real `MarketMatchingPipeline` against the original Workflow_10
  bind-time submission for the same named insured, a separate broker-approval-gated action per
  the PRD's own step sequence, NOT automatic inside `/run`; 409 on `NO_REMARKET`),
  `accept-incumbent` (workflow-owned — records `final_decision`, which no frozen `ReviewAction`
  carries), `escalate` (reuses the existing `ReviewAction.ESCALATE`, which already fits).
- `tests/test_es_renewal_remarketing.py` — all 6 real Workflow_16 scenarios, plus the
  `initiate-remarket`/`accept-incumbent`/`escalate` actions, including confirming the RR-05
  re-invocation actually creates a real Market Matching review item (not just a flag).

**Dataset:** `Workflow_16` (E&S · Renewal Remarketing — added to DATA_AND_FIXTURES.md's mapping
table, with an explicit note distinguishing this from the MGA table's separate, never-built
"Renewal Management" row at index 2), copied from
`Data sets/Workflow 7/renewal_remarketing_dataset/` into `TEST_DATA_ROOT/Workflow_16/test_dataset/`
unchanged. Simplest fixture shape of any E&S workflow so far — every scenario is one
already-structured `renewal_context.json`, no raw emails, no new extraction target at all.

**Key decisions / deviations (pre-approved)**
- Scheduled monitoring (FR-19, "the fourth such process in this vertical"): continued the
  deferred pattern — no new Arq infra built, consistent with the same call made 3 times already.
- No migration — remarketing history treated as input data (read from the fixture, reasoned
  over, logged to the audit trail) rather than a new accumulating table; real multi-year
  cross-cycle persistence is a natural production evolution, not testable within this
  fixture-driven pass.
- No new Agent Communication trigger — re-scanned all 20 FRs; none mandates one (unlike Binder &
  Issuance's FR-19 / Endorsement's FR-16, which explicitly did).
- RR-05's execution model: a separate `initiate-remarket` action, not automatic — matches the
  PRD's own §4 step 6b ("broker approves initiating a remarket, which re-invokes Market
  Matching") more faithfully than firing it inside `/run`.

**Verification:** `ruff check .` / `mypy src` clean (110 files); `pytest
tests/test_es_renewal_remarketing.py` 11/11 pass; full E&S suite (all seven workflows) 67/67 pass
together. `alembic heads` still one head; `alembic check` no drift. `core/common`/`verticals/mga`
byte-identical via `git status`. Live: app boots, all seven vertical route groups + MGA's return
200. Live-verified Scenarios 02/03/04 produce three genuinely distinct trigger levels (FULL/
LIGHT/URGENT) with correctly differentiated reasoning text. Live-verified `initiate-remarket` on
Scenario 02 genuinely re-invokes Market Matching — confirmed by checking
`GET /api/es/market-matching`'s own list grew by one real item (8 → 9), not just a flag. Frontend:
not touched this phase (no FE work requested for this workflow yet).

### Addendum — Frontend Integration (2026-07-27)

Wired `Insurance OS`'s `/app/workflows/renewal-remarketing` screen to this workflow — same
additive pattern as the prior FE integrations, though for a different reason this time.

**What changed**
- FE: `src/lib/api/renewalRemarketing.ts` (typed run/list/detail/initiate-remarket/
  accept-incumbent/escalate calls, reusing `client.ts`), with `FIXTURE_SCENARIOS`
  (`{ref, label}` pairs).
- Added a new `LiveRenewalsSection` to the existing (otherwise untouched) `RenewalRemarketing`
  screen: run any of the 6 `Workflow_16` scenarios, real inbox, real detail — the four-state
  trigger badge with its reasoning summary, exposure/loss-history/incumbent-response signals,
  remarketing-history grounding text, the post-remarket comparison view (incumbent vs.
  alternative, exception-quote flag rendered with equal prominence per the FE_CONTRACT's
  explicit "never default to the lower premium" instruction), and real
  initiate/accept/escalate actions.

**Key decision — additive again, but for a new reason:** unlike the last four workflows, this
mock's actions (`reinvokeMarketMatching`/`initiateUrgentRemarket`) are pure local log entries —
no cross-screen hand-off to preserve here. The additive pattern was kept anyway, for consistency
with the rest of the app and because the real data model still doesn't map cleanly onto the
mock's (single 3-tier per-quote materiality vs. the real schema's directly-comparable/
material-differences model; a `remarketedTimesInHistory` counter vs. the real free-text
`remarketing_history_detail`). Same reasoning as Market Matching/Package Assembly's original
"wire what's real, stub the rest" call — just expressed as a separate section instead of a
tab-level stub, per the pattern this app has settled into.

**Verification:** `ruff`/`mypy` clean (110 files); `pytest` 67/67 across all seven E&S workflows;
`tsc`/`eslint`/`npm run build` all clean. Live: ran Scenarios 02/03/04 — confirmed
`FULL_REMARKET`/`LIGHT_REMARKET_CHECK`/`URGENT_REMARKET` with reasoning text matching the
FE_CONTRACT samples exactly; called `initiate-remarket` on Scenario 02 and confirmed
`/api/es/market-matching`'s list grew by one real item (9 → 10, continuing from this phase's
own backend verification); confirmed a `409` when calling `initiate-remarket` on a
`NO_REMARKET` decision (Scenario 01); ran Scenario 05 and confirmed the exception-quote
comparison ($171,000 Palmetto alternative flagged `is_exception_based: true` alongside the
$187,000 Ironclad incumbent). SSR smoke test across all 18 frontend routes returned 200 with
no errors — the mock panel confirmed unchanged.

Not committed in either repo — ready for review.

## Phase 3 — E&S Diligent Search & Compliance Documentation Copilot ✅ (2026-07-27)

**Scope:** eighth E&S workflow, `verticals/es/workflows/diligent_search/**` — the highest
legal-stakes workflow in the vertical (PRD §8: a wrongly generated affidavit is a potentially
fraudulent record, not just a bad recommendation). No migration; `core/common` and
`verticals/mga/**` confirmed genuinely untouched via `git status` (zero new files, not just
unmodified existing ones).

**What was built**
- `compliance_engine.py` — DS-01/DS-02 per-state requirement + export-list/exemption
  determination (a genuine three-way split — `REQUIRED` / `EXEMPT` / `PENDING_DETERMINATION`,
  never defaulted from an absent state entry), DS-03 (strict written-evidence-only sufficiency
  counting, with a gap-detail phrasing verified to match the dataset's own worked numbers —
  Scenario 03's "need 1 more decline, upgrade Carrier B's verbal one" counts against declinations
  ON FILE, not written-only, since a verbal decline already occupies one of the required slots),
  DS-04's generation gate (`document_eligible` set true ONLY on a confirmed SUFFICIENT
  determination — enforced structurally here, not left to `draft()` to check). The core judgment
  call: an unconditional export-list note (Texas, "IS on the export list... not required")
  auto-resolves to EXEMPT; a hedged, account-specific note (Florida, "MAY be export-eligible...
  for large commercial accounts") routes to PENDING_DETERMINATION instead (FR-7), detected via
  hedge-language matching (`may`/`depends`/`for large`/etc.) rather than trusting
  `export_list_class: true` alone.
- `service.py` — `DiligentSearchPipeline`. Detects two input shapes from the data itself: a
  single-state case (`state`/`state_requirement`/`declinations_on_file` at the top level) or a
  multi-state case (`states` + a `state_requirements` dict that may omit entries entirely).
  `draft()` generates one grounded document per DS-04-eligible state (never for any other state —
  no partial/best-effort path exists to bypass) plus one overall per-state-checklist summary
  draft.
- `schema.py` — `ComplianceRecordPayload`, mirroring PRD §7 closely. One additive field beyond the
  literal schema: `generated_document_text` per state (the literal schema only has
  `document_generated: boolean`, which gives the FE nothing to actually show).
  `retention_period_years` is always `null` — no scenario supplies real per-state legal reference
  data, and FR-8 calls that a required discovery input, not something to derive from reasoning.
- `router.py` — `/api/es/diligent-search`: `run`, list, detail, `approve`/`escalate` (both reuse
  the existing frozen `ReviewAction` values — no new workflow-owned action endpoint was needed,
  unlike several prior workflows, since `APPROVE`/`ESCALATE` already cover the PRD's full broker
  action set).
- `tests/test_es_diligent_search.py` — all 4 real Workflow_17 scenarios, plus `approve`/`escalate`.
  Scenario 03 is the mandatory release-gate test: asserts `document_generated is False` AND
  `generated_document_text is None` when evidence is insufficient.

**Dataset:** `Workflow_17` (E&S · Diligent Search & Compliance Documentation — added to
DATA_AND_FIXTURES.md's mapping table), copied from
`Data sets/Workflow 8/diligent_search_dataset/` into `TEST_DATA_ROOT/Workflow_17/test_dataset/`
unchanged. Only 4 scenarios (smaller than every prior dataset) and the simplest fixture shape of
any E&S workflow so far, tied with Renewal Remarketing — every scenario is one already-structured
`case_context.json`, no raw emails, no new extraction target at all.

**Key decisions / deviations (pre-approved)**
- Florida's hedged export-list note (Scenario 04) routes to `PENDING_DETERMINATION`, not `EXEMPT`
  — per FR-7, account-specific export eligibility must be flagged for human/legal review, never
  auto-resolved just because `export_list_class` is technically `true` in the data.
- `generated_document_text` added as an additive schema field beyond PRD §7's literal shape, so
  DS-04's "generate the compliant document" produces actual grounded content, not just a boolean.
- `retention_period_years` left `null` everywhere rather than inventing plausible-sounding
  per-state year counts — the honest answer per this project's grounding discipline, since FR-8
  explicitly calls this a required discovery input.
- No migration — `OutputPackage.payload` carries the full compliance record, same as every prior
  E&S workflow.
- No new Agent Communication trigger, no scheduled job, no cross-workflow re-invocation —
  re-scanned all 8 FRs; none applies here (unlike Binder & Issuance/Endorsement's new triggers or
  Renewal Remarketing's re-invocation).

**Verification:** `ruff check .` / `mypy src` clean (117 files); `pytest tests/test_es_diligent_search.py`
6/6 pass; full E&S suite (all eight workflows) green together (99 passed overall across the whole
suite; the 11 pre-existing MGA/extraction failures are unrelated to this change — `Workflow_1`
fixture data is absent from `TEST_DATA_ROOT` on this machine, a pre-existing condition unrelated
to any E&S work this session). `alembic heads` still one head; no migration added, so no drift.
`core/common`/`verticals/mga` byte-identical via `git status` (only `verticals/es/router.py`
modified, one `include_router` line, plus new files under `workflows/diligent_search/`). Live: app
boots, all eight vertical route groups + MGA's return 200. Live-verified all 4 scenarios end to
end: Scenario 01 generates a real grounded document; Scenario 02 logs the exemption with an
explicit `exemption_basis`, distinct from a blank/missing state; **Scenario 03 (the release gate)
confirmed to generate `generated_document_text: null` and `document_generated: false`** — zero
document text produced when evidence is insufficient; Scenario 04 renders a genuine 8-state
checklist (TN/GA `REQUIRED`, FL `PENDING_DETERMINATION` per the FR-7 hedge-language call, the
remaining 5 states explicitly flagged incomplete). Frontend: not touched this phase (no FE work
requested for this workflow).

### Addendum — Frontend Integration (2026-07-27)

Wired `Insurance OS`'s `/app/workflows/diligent-search` screen to this workflow — same additive
pattern as the prior FE integrations (no cross-screen hand-off to preserve here, same as Renewal
Remarketing; kept for consistency with the rest of the app).

**What changed**
- FE: `src/lib/api/diligentSearch.ts` (typed run/list/detail/approve/escalate calls, reusing
  `client.ts`), with `FIXTURE_SCENARIOS` (`{ref, label}` pairs, only 4 this time).
- Added a new `LiveComplianceSection` to the existing (otherwise untouched)
  `DiligentSearchCompliance` screen: run any of the 4 `Workflow_17` scenarios, real inbox, real
  detail — full per-state checklist (never a single collapsed verdict), requirement/exemption/
  sufficiency badges, exemption basis text, gap detail, per-declination written-vs-verbal
  evidence display, the document-generated flag with real generated affidavit text when present,
  the "not yet sourced" retention-period disclosure, and real approve/escalate actions (escalate
  shown only when a state is genuinely `PENDING_DETERMINATION`).

**Verification:** `ruff`/`mypy` clean (117 files); `pytest` 73/73 across all eight E&S workflows;
`tsc`/`eslint`/`npm run build` all clean. Live: ran all 4 scenarios — confirmed Scenario 01's
real generated document text, Scenario 02's `EXEMPT` with explicit `exemption_basis` (never
blank), **Scenario 03's release gate** (`document_generated: false`, `generated_document_text:
null`), and Scenario 04's full 8-state checklist with Florida correctly landing on
`PENDING_DETERMINATION` rather than auto-exempt; confirmed `escalate` on the Florida state
succeeds. SSR smoke test across all 18 frontend routes returned 200 with no errors — the mock
panel confirmed unchanged. (Both dev servers were found down partway through this session's
verification — unrelated to this change — and were restarted cleanly before the final checks
above.)

Not committed in either repo — ready for review.

Not committed — ready for review.
