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

## E&S Package Assembly (Phase 3 — second E&S workflow)

Consumes a Market Matching decision directly (carrier selection +
requirements) and assembles a carrier-specific submission package — document
completeness, grounded supplemental-form auto-fill, a tailored cover letter,
and a `READY`/`READY_WITH_GAP`/`BLOCKED` status per carrier. No new
extraction, no re-matching. Routes under `/api/es/package-assembly`; lives
entirely in `verticals/es/workflows/package_assembly/` — see
`docs/DATA_AND_FIXTURES.md`'s Workflow_11 note for why its fixtures aren't
`submission_*/*.txt` like every other workflow's.

```powershell
python src/core/seed.py        # demo-es tenant
uvicorn main:app --app-dir src --reload --port 4000
```
```powershell
$h = @{ "x-tenant-id"="demo-es"; "x-user-id"="demo-es-junior"; "x-role"="junior" }
# assemble a package for one scenario/carrier (omit carrier_id to fan out
# across every carrier the broker selected):
Invoke-RestMethod -Method Post -Headers $h -ContentType application/json `
  -Body '{"scenario_ref":"scenario_04"}' "http://localhost:4000/api/es/package-assembly/run"
Invoke-RestMethod -Headers $h "http://localhost:4000/api/es/package-assembly"        # list
Invoke-RestMethod -Headers $h "http://localhost:4000/api/es/package-assembly/<id>"   # detail
```
The FE wiring spec (endpoints + real sample JSON + field mapping) is in
[docs/FE_CONTRACT_package_assembly.md](docs/FE_CONTRACT_package_assembly.md).

### Run the eval (all 6 real Workflow_11 scenarios, incl. the mandatory Scenario 04 release gate)
```powershell
pytest tests/test_es_package_assembly.py -v
```

## E&S Retail Agent Communication Copilot (Phase 3 — third E&S workflow)

Drafts a retail-agent-facing email from the structured output of Market
Matching or Package Assembly (or a manually-logged quote/bind entry) — six
communication types, tone-calibrated per the RA-TN rules, with a compliance
gate on the highest-sensitivity type (No Market Found). No extraction, no
rules-engine checks — the lightest build in the vertical so far. Routes under
`/api/es/agent-communication`; lives entirely in
`verticals/es/workflows/agent_communication/` — see
`docs/DATA_AND_FIXTURES.md`'s Workflow_12 note for its `trigger_*` fixture
shape.

```powershell
python src/core/seed.py        # demo-es tenant
uvicorn main:app --app-dir src --reload --port 4000
```
```powershell
$h = @{ "x-tenant-id"="demo-es"; "x-user-id"="demo-es-junior"; "x-role"="junior" }
$sr = @{ "x-tenant-id"="demo-es"; "x-user-id"="demo-es-senior"; "x-role"="senior" }
# draft a communication from a trigger object (post the whole trigger_XX/
# trigger_input.json's contents as the "trigger" field):
Invoke-RestMethod -Method Post -Headers $h -ContentType application/json `
  -Body (Get-Content "path\to\trigger_04\trigger_input.json" -Raw | %{ "{`"trigger`":$_}" }) `
  "http://localhost:4000/api/es/agent-communication/run"
Invoke-RestMethod -Headers $h "http://localhost:4000/api/es/agent-communication"        # list
Invoke-RestMethod -Headers $h "http://localhost:4000/api/es/agent-communication/<id>"   # detail
# No Market Found drafts are compliance-gated (senior/admin only to clear):
Invoke-RestMethod -Method Post -Headers $sr "http://localhost:4000/api/es/agent-communication/<id>/compliance-clear"
```
The FE wiring spec (endpoints + real sample JSON + field mapping) is in
[docs/FE_CONTRACT_agent_communication.md](docs/FE_CONTRACT_agent_communication.md).

### Run the eval (all 6 real Workflow_12 triggers, incl. FR-5/FR-12 dedup + the compliance-gate flow)
```powershell
pytest tests/test_es_agent_communication.py -v
```

## E&S Quote Comparison & Recommendation Copilot (Phase 3 — fourth E&S workflow)

Ingests carrier response emails (quotes and declinations) for a submission
already shopped via Market Matching/Package Assembly, normalizes terms for
genuine comparability (never a flat premium comparison), classifies
subjectivities by materiality, tracks quote validity windows with proactive
urgency, and produces a single recommendation or an explicit multi-option
trade-off. Feeds Agent Communication's Quote/Terms Summary once the broker
selects a quote. Routes under `/api/es/quote-comparison`; lives entirely in
`verticals/es/workflows/quote_comparison/` — see `docs/DATA_AND_FIXTURES.md`'s
Workflow_13 note for its raw-carrier-email fixture shape (a bigger build
than Package Assembly/Agent Communication — genuinely new extraction target
and comparison logic, not a reuse of any prior workflow's pattern).

```powershell
python src/core/seed.py        # demo-es tenant
uvicorn main:app --app-dir src --reload --port 4000
```
```powershell
$h = @{ "x-tenant-id"="demo-es"; "x-user-id"="demo-es-junior"; "x-role"="junior" }
Invoke-RestMethod -Method Post -Headers $h -ContentType application/json `
  -Body '{"scenario_ref":"scenario_02"}' "http://localhost:4000/api/es/quote-comparison/run"
Invoke-RestMethod -Headers $h "http://localhost:4000/api/es/quote-comparison"        # list
Invoke-RestMethod -Headers $h "http://localhost:4000/api/es/quote-comparison/<id>"   # detail (urgency recomputed live)
# broker selects a quote -> fires Agent Communication's QUOTE_TERMS_SUMMARY automatically:
Invoke-RestMethod -Method Post -Headers $h "http://localhost:4000/api/es/quote-comparison/<id>/select/<quote_id>"
```
The FE wiring spec (endpoints + real sample JSON + field mapping) is in
[docs/FE_CONTRACT_quote_comparison.md](docs/FE_CONTRACT_quote_comparison.md).

### Run the eval (all 6 real Workflow_13 scenarios, incl. the FR-20 handoff + read-time urgency recompute)
```powershell
pytest tests/test_es_quote_comparison.py -v
```

## E&S Binder & Policy Issuance Coordination Copilot (Phase 3 — fifth E&S workflow)

Closes the full placement lifecycle (submission → match → package → quote
→ compare → **bind and issue**). Coordinates the bind request, blocks it
while a material pre-bind subjectivity remains open, reconciles the
carrier's bind confirmation and the issued policy document against what
was actually agreed (a binder number or an official policy document is
data to verify, never trusted just because it's the carrier's own
output), monitors policy issuance timelines against the carrier's own
stated deadline, and tracks post-bind ongoing obligations. Feeds Agent
Communication's Placement Confirmation and the new Policy Documents
Delivered trigger — but only on a verified-clean reconciliation, never on
an unresolved discrepancy. Routes under `/api/es/binder-issuance`; lives
entirely in `verticals/es/workflows/binder_issuance/` — see
`docs/DATA_AND_FIXTURES.md`'s Workflow_14 note for its mixed pre-bind/
post-issuance fixture shape.

```powershell
python src/core/seed.py        # demo-es tenant
uvicorn main:app --app-dir src --reload --port 4000
```
```powershell
$h = @{ "x-tenant-id"="demo-es"; "x-user-id"="demo-es-junior"; "x-role"="junior" }
Invoke-RestMethod -Method Post -Headers $h -ContentType application/json `
  -Body '{"scenario_ref":"scenario_03"}' "http://localhost:4000/api/es/binder-issuance/run"
Invoke-RestMethod -Headers $h "http://localhost:4000/api/es/binder-issuance"        # list
Invoke-RestMethod -Headers $h "http://localhost:4000/api/es/binder-issuance/<id>"   # detail (issuance/reminders recomputed live)
# broker resolves a flagged bind-confirmation discrepancy -> releases Placement Confirmation:
Invoke-RestMethod -Method Post -Headers $h -ContentType application/json `
  -Body '{"resolution":"accept_carrier_version"}' `
  "http://localhost:4000/api/es/binder-issuance/<id>/resolve-confirmation-discrepancy"
```
The FE wiring spec (endpoints + real sample JSON + field mapping) is in
[docs/FE_CONTRACT_binder_issuance.md](docs/FE_CONTRACT_binder_issuance.md).

### Run the eval (all 6 real Workflow_14 scenarios, incl. the discrepancy-resolution handoff + read-time recompute)
```powershell
pytest tests/test_es_binder_issuance.py -v
```

## E&S Endorsement / Mid-Term Change Processing Copilot (Phase 3 — sixth E&S workflow)

Classifies mid-term change requests on already-bound policies by type and
materiality (never a single flat "how big" score — certain types are
always routine, certain types are never purely routine regardless of
size), rechecks carrier appetite for changes touching class/state/severity
exposure with an explicit three-outcome model (within/outside/genuinely
unknown — an absent class is never assumed excluded, same discipline as
Market Matching's MM-04), flags premium impact and proration inputs
without asserting an unconfirmed figure, and reconciles the carrier's
issued endorsement item-by-item for multi-part requests. Reuses Market
Matching's Carrier Appetite Profile data and Binder & Policy Issuance's
never-trust-the-carrier-document discipline. Routes under
`/api/es/endorsement`; lives entirely in
`verticals/es/workflows/endorsement/` — see `docs/DATA_AND_FIXTURES.md`'s
Workflow_15 note for its mixed pre-issuance/reconciliation fixture shape.

```powershell
python src/core/seed.py        # demo-es tenant
uvicorn main:app --app-dir src --reload --port 4000
```
```powershell
$h = @{ "x-tenant-id"="demo-es"; "x-user-id"="demo-es-junior"; "x-role"="junior" }
Invoke-RestMethod -Method Post -Headers $h -ContentType application/json `
  -Body '{"scenario_ref":"scenario_03"}' "http://localhost:4000/api/es/endorsement/run"
Invoke-RestMethod -Headers $h "http://localhost:4000/api/es/endorsement"        # list
Invoke-RestMethod -Headers $h "http://localhost:4000/api/es/endorsement/<id>"   # detail
# broker resolves a flagged item-level reconciliation discrepancy -> releases ENDORSEMENT_CONFIRMED:
Invoke-RestMethod -Method Post -Headers $h -ContentType application/json `
  -Body '{"resolution":"flag_carrier_error"}' `
  "http://localhost:4000/api/es/endorsement/<id>/resolve-discrepancy"
```
The FE wiring spec (endpoints + real sample JSON + field mapping) is in
[docs/FE_CONTRACT_endorsement.md](docs/FE_CONTRACT_endorsement.md).

### Run the eval (all 6 real Workflow_15 scenarios, incl. the appetite-unknown gate + item-level reconciliation handoff)
```powershell
pytest tests/test_es_endorsement.py -v
```

## E&S Renewal Remarketing Copilot (Phase 3 — seventh E&S workflow, last one gated on bound-policy data)

Given a bound policy approaching renewal, detects exposure and loss-history
changes, checks incumbent responsiveness and appetite, and produces a
**graduated, four-state** remarket recommendation
(`NO_REMARKET`/`LIGHT_REMARKET_CHECK`/`FULL_REMARKET`/`URGENT_REMARKET`) —
never a binary yes/no (Scenario 03's whole point: favorable change + a
size-band shift earns a lightweight check, not a full shop). An
orchestration workflow — genuinely re-invokes the existing Market Matching
engine rather than a separate ranking implementation, and reuses Quote
Comparison's term-normalization discipline for post-remarket comparisons.
Routes under `/api/es/renewal-remarketing`; lives entirely in
`verticals/es/workflows/renewal_remarketing/` — see
`docs/DATA_AND_FIXTURES.md`'s Workflow_16 note (and its explicit
clarification that the MGA "Renewal Management" mapping-table row is a
separate, never-built slot — don't confuse the two).

```powershell
python src/core/seed.py        # demo-es tenant
uvicorn main:app --app-dir src --reload --port 4000
```
```powershell
$h = @{ "x-tenant-id"="demo-es"; "x-user-id"="demo-es-junior"; "x-role"="junior" }
Invoke-RestMethod -Method Post -Headers $h -ContentType application/json `
  -Body '{"scenario_ref":"scenario_02"}' "http://localhost:4000/api/es/renewal-remarketing/run"
Invoke-RestMethod -Headers $h "http://localhost:4000/api/es/renewal-remarketing"        # list
Invoke-RestMethod -Headers $h "http://localhost:4000/api/es/renewal-remarketing/<id>"   # detail
# broker approves a light/full remarket -> genuinely re-invokes Market Matching:
Invoke-RestMethod -Method Post -Headers $h "http://localhost:4000/api/es/renewal-remarketing/<id>/initiate-remarket"
```
The FE wiring spec (endpoints + real sample JSON + field mapping) is in
[docs/FE_CONTRACT_renewal_remarketing.md](docs/FE_CONTRACT_renewal_remarketing.md).

### Run the eval (all 6 real Workflow_16 scenarios, incl. the four-state distinction + genuine Market Matching re-invocation)
```powershell
pytest tests/test_es_renewal_remarketing.py -v
```

## E&S Diligent Search & Compliance Documentation Copilot (Phase 3 — eighth E&S workflow, highest legal stakes in the vertical)

Given a submission's state(s) of operation, determines per-state
diligent-search requirement, checks export-list/exemption eligibility,
verifies declination evidence against a strict written-evidence bar, and
generates a compliant document **only** when that bar is met — a wrongly
generated affidavit is a potentially fraudulent record (PRD §8), so
document generation is fully gated on evidence sufficiency, never
partial/best-effort. A hedged, account-specific export-list note (e.g.
"may be export-eligible... for large commercial accounts") never
auto-resolves to an exemption — it's flagged `PENDING_DETERMINATION` for
human/legal review (FR-7), distinct from an unconditional export-list
exemption, which is EXEMPT and explicitly, distinctly logged (never
indistinguishable from missing documentation). Multi-state risks render as
a genuine per-state checklist, never a single collapsed verdict. Routes
under `/api/es/diligent-search`; lives entirely in
`verticals/es/workflows/diligent_search/` — see `docs/DATA_AND_FIXTURES.md`'s
Workflow_17 note.

```powershell
python src/core/seed.py        # demo-es tenant
uvicorn main:app --app-dir src --reload --port 4000
```
```powershell
$h = @{ "x-tenant-id"="demo-es"; "x-user-id"="demo-es-junior"; "x-role"="junior" }
Invoke-RestMethod -Method Post -Headers $h -ContentType application/json `
  -Body '{"scenario_ref":"scenario_03"}' "http://localhost:4000/api/es/diligent-search/run"
Invoke-RestMethod -Headers $h "http://localhost:4000/api/es/diligent-search"        # list
Invoke-RestMethod -Headers $h "http://localhost:4000/api/es/diligent-search/<id>"   # detail
# escalate an ambiguous PENDING_DETERMINATION state to compliance/legal:
Invoke-RestMethod -Method Post -Headers $h "http://localhost:4000/api/es/diligent-search/<id>/escalate"
```
The FE wiring spec (endpoints + real sample JSON + field mapping) is in
[docs/FE_CONTRACT_diligent_search.md](docs/FE_CONTRACT_diligent_search.md).

### Run the eval (all 4 real Workflow_17 scenarios — Scenario 03 is the mandatory, non-skippable release gate: zero document text generated when evidence is insufficient)
```powershell
pytest tests/test_es_diligent_search.py -v
```

## E&S Carrier Appetite Intelligence Tracking Copilot (Phase 3 — ninth and LAST E&S workflow in this phase)

Aggregates signals already logged by Quote Comparison/Renewal Remarketing,
distinguishes genuine class-level appetite-shift patterns from normal
account-specific decline variance, and auto-updates exactly two metadata
fields (`appetite_confidence`/`appetite_last_updated`) — never a
substantive field. This PRD's own Section 0 calls it the highest
scope-creep risk in the vertical at every prior mention, so the v1 here
is deliberately narrow and **quiet almost all the time**: 3 of its 4 test
scenarios produce no suggestion at all. No mutable Carrier Appetite
Profile store or profile-editing interface exists anywhere in this
codebase (Market Matching's profiles are read-only JSON), so the
metadata refresh and any suggestion are computed and recorded in this
workflow's own payload — a stated limitation, not hidden. Routes under
`/api/es/carrier-appetite-intelligence`; lives entirely in
`verticals/es/workflows/carrier_appetite_intelligence/` — see
`docs/DATA_AND_FIXTURES.md`'s Workflow_18 note.

```powershell
python src/core/seed.py        # demo-es tenant
uvicorn main:app --app-dir src --reload --port 4000
```
```powershell
$h = @{ "x-tenant-id"="demo-es"; "x-user-id"="demo-es-junior"; "x-role"="junior" }
Invoke-RestMethod -Method Post -Headers $h -ContentType application/json `
  -Body '{"scenario_ref":"scenario_02"}' "http://localhost:4000/api/es/carrier-appetite-intelligence/run"
Invoke-RestMethod -Headers $h "http://localhost:4000/api/es/carrier-appetite-intelligence"        # list
Invoke-RestMethod -Headers $h "http://localhost:4000/api/es/carrier-appetite-intelligence/<id>"   # detail
# dismiss a genuine-inconsistency suggestion the broker declines to act on:
Invoke-RestMethod -Method Post -Headers $h "http://localhost:4000/api/es/carrier-appetite-intelligence/<id>/dismiss"
```
The FE wiring spec (endpoints + real sample JSON + field mapping) is in
[docs/FE_CONTRACT_carrier_appetite_intelligence.md](docs/FE_CONTRACT_carrier_appetite_intelligence.md).

### Run the eval (all 4 real Workflow_18 scenarios — Scenario 03 is the release gate: an account-specific decline reason must never score like Scenario 02's genuine class-level pattern)
```powershell
pytest tests/test_es_carrier_appetite_intelligence.py -v
```

## E&S Pipeline & Carrier Performance Reporting Copilot (Phase 4 — 10th and LAST workflow on the original E&S roadmap)

A pure aggregation/reporting layer over the six prior workflows' logs —
not a new decision/classification workflow. Generates a pipeline funnel
view, carrier hit-rate comparisons, or remarketing value reports
(detected from the scenario's own data shape — unlike every prior
workflow, its 4 test scenarios each test a DIFFERENT report kind, not 4
instances of the same shape). Its entire credibility rests on never
smoothing over a data gap (PR-06 — a direct throughline back to this
project's very first critique, the original landing page's fabricated
dashboard stats) and never presenting a low-volume figure with false
confidence (PR-02). Routes under `/api/es/pipeline-reporting`; lives
entirely in `verticals/es/workflows/pipeline_reporting/` — see
`docs/DATA_AND_FIXTURES.md`'s Workflow_19 note. Unlike every prior
workflow, there's no `approve`/`escalate` action — a report isn't a
determination a human approves or declines.

```powershell
python src/core/seed.py        # demo-es tenant
uvicorn main:app --app-dir src --reload --port 4000
```
```powershell
$h = @{ "x-tenant-id"="demo-es"; "x-user-id"="demo-es-junior"; "x-role"="junior" }
Invoke-RestMethod -Method Post -Headers $h -ContentType application/json `
  -Body '{"scenario_ref":"scenario_03"}' "http://localhost:4000/api/es/pipeline-reporting/run"
Invoke-RestMethod -Headers $h "http://localhost:4000/api/es/pipeline-reporting"        # list
Invoke-RestMethod -Headers $h "http://localhost:4000/api/es/pipeline-reporting/<id>"   # detail
```
The FE wiring spec (endpoints + real sample JSON + field mapping) is in
[docs/FE_CONTRACT_pipeline_reporting.md](docs/FE_CONTRACT_pipeline_reporting.md).

### Run the eval (all 4 real Workflow_19 scenarios — Scenario 03 is the release gate: a logging gap must never be silently interpolated or omitted)
```powershell
pytest tests/test_es_pipeline_reporting.py -v
```

**This completes the original 10-item E&S workflow roadmap.**

