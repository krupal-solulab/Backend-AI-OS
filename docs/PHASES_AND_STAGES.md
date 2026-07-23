# Phases & Stages

Delivery is sequenced so the **shared core lands first**, then **two developers build workflows in parallel** (MGA + E&S) with no conflict. Phases are milestones; stages are the tasks inside.

---

## Phase 0 — Foundations *(shared · unlocks parallel work)*
**Goal:** repo + skeleton so two devs can start immediately.
- **0.1** Repo, uv (or Poetry), FastAPI skeleton, ruff + mypy, CI.
- **0.2** Postgres + SQLModel/SQLAlchemy + Alembic; base schema (`Tenant` w/ `vertical`, `User`, `Submission`, `Document`, `RuleSet`/`RuleVersion`, `Decision`, `AuditEntry`, `ReviewItem`, `Connection`).
- **0.3** `tenancy` + `auth` (RBAC roles, authority limits, guards).
- **0.4** **Freeze `core/common` contracts** (interfaces/DTOs) — the pipeline interface every workflow implements. *This is the gate that lets everyone work in parallel.*
- **0.5** `fixtures` loader wired to `TEST_DATA_ROOT` (see DATA_AND_FIXTURES.md).
- **0.6** Nango account + `ingestion`/`ConnectorService` skeleton (mock mode ok).
**Exit:** `fastapi dev` (uvicorn) boots; a dummy `/api/core/health` works; contracts merged.

## Phase 1 — Shared Core *(build once · the big investment)*
**Goal:** the whole engine every workflow reuses.
- **1.1** `extraction` — classification + extraction to a cited field model (start with `.txt` fixtures, add OCR later).
- **1.2** `rules-engine` — generic evaluator + versioning (publish/rollback) + the 6 check types.
- **1.3** `llm` — OpenAI wrapper behind `LLMService` (grounded, cited, model-tier routing).
- **1.4** `documents`, `review-queue`, `audit`, `reporting` frameworks.
- **1.5** `jobs` — Celery ingestion→extraction pipeline + error queue.
- **1.6** `ingestion` real Nango: read inbox, fetch attachments, send mail.
**Exit:** a fixture email can flow ingest → extract → rules → (stub decision) → review item → audit, end to end.

## Phase 2 — First workflows, IN PARALLEL *(proves the model)*
Two devs, two folders, zero overlap.
- **2A (MGA dev)** `verticals/mga/decision-core` (Appetite Engine) + `workflows/submission-triage`. Loads Workflow_1 fixtures + validation rules.
- **2B (E&S dev)** `verticals/es/decision-core` (Matching/Ranking) + `workflows/market-matching`. Loads its own fixtures.
- Each: implement the pipeline (WORKFLOW_TEMPLATE.md), expose `/api/{vertical}/{workflow}`, wire to its FE screen, add an eval against fixtures.
**Exit:** both first workflows run against real test data and drive their FE screens.

## Phase 3 — Expand each vertical *(same template, still parallel)*
Each subsequent workflow is an independent module — devs keep adding without touching each other or the core. Recommended order per the MGA roadmap's leverage:
- **MGA:** Renewal Management → Broker Communication → Endorsement → Quoting & Rating → Bind Order.
- **E&S:** Package Assembly → Agent Communication → Quote Comparison → Binder & Issuance → Endorsement → Renewal Remarketing → Diligent Search → Carrier Appetite Intel.
- Communication + Reporting workflows reuse the LLM/reporting frameworks almost entirely.

## Phase 4 — Governance, reporting & feedback *(needs decision history)*
- **4.1** MGA **Appetite Governance & Audit** + **Portfolio** and E&S **Pipeline & Carrier Reporting** — read the shared `audit`/`reporting`. Low new build.
- **4.2** **Eval/feedback loop** — capture AI-recommendation vs human-decision; agreement-rate dashboards.
- **4.3** Appetite-drift detection over time (rule-version diffs).

## Phase 5 — Write-back, hardening & scale
- **5.1** PAS/CRM write-back (the "real integration") — MGA Bind, E&S Binder; Sheets fallback via Nango.
- **5.2** Bordereau (MGA) scheduled generation + carrier-format templates.
- **5.3** Security/compliance hardening (encryption, retention, SOC 2 track), observability, rate limits.
- **5.4** Optional: extract a hot module into its own service if load demands.

---

### Dependency summary
```
Phase 0 (contracts) ─► Phase 1 (core) ─► Phase 2 (first workflows, parallel)
                                     └─► Phase 3 (more workflows, parallel)
Phase 4 (reporting/eval) needs Phases 2–3 to have produced decision history.
Phase 5 (write-back) needs a design partner's PAS.
```
**The only hard serialization:** Phase 0 → Phase 1 → then everything else fans out per developer.

---

## Definition of Done — Phase 0 + 1 (foundation handoff)
The foundation is built by one dev, pushed to Git, then pulled and verified by the team before anyone starts a workflow. It's "done" when all of the below pass.

**Phase 0 — Foundations**
- [ ] App boots (`fastapi dev` / uvicorn); `/api/core/health` responds.
- [ ] Postgres + SQLModel/Alembic with base tables: `Tenant` (with `vertical`), `User`, `Submission`, `Document`, `RuleSet`/`RuleVersion`, `Decision`, `OutputPackage`, `ReviewItem`, `AuditEntry`, `Connection`.
- [ ] `tenancy` + `auth` working: roles `junior`/`senior`/`admin`, authority limit, a guard that scopes by tenant + vertical.
- [ ] **Contracts in `core/common`** — `WorkflowPipeline` interface + shared DTOs — exist and compile.
- [ ] Fixtures loader reads `TEST_DATA_ROOT` and turns `Workflow_1/submission_XX/` into `Submission` + `Document[]`.
- [ ] Folder skeleton matches `FOLDER_STRUCTURE.md` (empty `verticals/mga` + `verticals/es`).

**Phase 1 — Shared Core**
- [ ] Modules present + interfaced: `extraction`, `rules-engine` (6 check types + publish/rollback versioning), `llm` (OpenAI wrapper: grounded, cited, tier-routed), `documents`, `review-queue`, `audit`, `reporting`, `jobs`, `ingestion` (Nango + `mock` mode).
- [ ] **Smoke test:** a fixture email → ingest → extract → rules → (stub decision) → review item → audit entry, end to end.

## Handoff & verification flow
```
Teammate builds Phase 0 + 1  →  push to Git
        │
You pull  →  uv sync && fastapi dev  →  run the DoD checklist + smoke test
        │
30-min CONTRACT REVIEW together  →  confirm core/common is generic for BOTH
        │   verticals (appetite AND matching), not just MGA  →  lock contracts
        ▼
Split: you → verticals/mga/submission-triage · teammate → verticals/es/market-matching  (parallel)
```
**Verify especially:** (1) the shared **contracts** are vertical-agnostic; (2) you can add a folder under `verticals/mga/workflows/` and get a route **without touching `core/`**. If both hold, the parallel model is safe to start.
