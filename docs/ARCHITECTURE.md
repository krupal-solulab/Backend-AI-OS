# Architecture

## 1. Principles
1. **One platform, two verticals.** Multi-tenant. Every tenant has a `vertical` = `MGA` | `ES`. The vertical selects which decision core, rules, schemas, and workflows apply.
2. **Shared core built once.** Extraction, ingestion, rules engine, LLM, review queue, audit, reporting, auth — all vertical-agnostic. ~50% of the backend.
3. **Thin vertical layer.** Only the *decision core* (MGA appetite vs E&S matching/ranking) and the *workflow orchestration* differ.
4. **Human-in-the-loop, always.** Nothing binds, sends, or writes back to a system of record without an explicit human action. AI drafts + recommends; a person approves.
5. **Everything is grounded + audited.** Every AI claim cites a source; every decision is written to the audit log.

## 2. Modular monolith
A single deployable FastAPI app made of independent packages (one router per workflow). This gives us microservice-style **ownership boundaries** (each workflow is its own package) without the ops cost. Split into services later only if needed.

```
AppModule
 ├─ core/*          (shared modules — build once)
 ├─ verticals/mga/* (MGA decision core + workflow modules)
 └─ verticals/es/*  (E&S decision core + workflow modules)
```

## 3. Request / processing flow (the pipeline every workflow follows)
```
1. Ingestion      Nango pulls a broker email + attachments (or a scheduled trigger)
2. Classification Each attachment typed (ACORD / loss run / financials / SOV / other)
3. Extraction     Structured, cited data model per document (Extraction Core)
4. Consistency    Cross-document checks (Rules Engine · "Combined" rules)
5. Rules          Validation rules (per document) + then Decision Core:
                    • MGA  → Appetite Engine (hard rules → in/out of appetite)
                    • E&S  → Matching/Ranking Engine (which carriers fit)
6. LLM            Grounded narrative / recommendation / draft (citation-enforced)
7. Output Package Recommendation + flags + missing-info + citations (typed)
8. Review Queue   Surfaced to a human with role-based actions
9. Human action   Approve / override / escalate / send / issue
10. Write-back    PAS / Sheets (via Nango) — only after approval
11. Audit + Eval  Decision logged; AI-vs-human captured for the feedback loop
```
Steps 1–4, 6–11 are **shared**. Step 5's Decision Core is **per vertical**.

## 4. Multi-tenancy & auth
- **Tenant** row + **vertical** enum on it. Every request is tenant-scoped.
- **RBAC roles** (shared): `junior`, `senior`, `admin`, plus vertical-neutral `compliance`/`ops` if needed. Authority limits (e.g. junior premium cap) are policy config per tenant.
- A `VerticalGuard`/`RolesGuard` gates routes; workflows only mount for their vertical's tenants.

## 5. Data model (shared base, extended per vertical)
Shared entities (in `core/common`):
`Tenant`, `User`, `Submission`, `Document`, `ExtractedField`, `RuleSet`/`RuleVersion`, `Decision`, `OutputPackage`, `ReviewItem`, `AuditEntry`, `Connection` (Nango).
Vertical entities extend/reference these (e.g. MGA `AppetiteResult`, `Quote`, `BindOrder`, `Bordereau`; E&S `Market`, `QuoteComparison`, `DiligentSearch`).

> **Phase 2 status:** the MGA Decision Core (Appetite Engine) + Submission Triage workflow are
> built (`verticals/mga/`). The Appetite Engine maps validation `RuleResult[]` + the extracted
> model → the frozen `Decision`. Simple validation is data (6-check rule sets via the shared
> engine); compound appetite logic (excluded class, compound severity, cross-doc variance/
> disclosure, timing, loss-trend, extraction-confidence → manual review) lives in the decision
> core with thresholds as data. `verticals/es/` is untouched.
>
> **Phase 2B/3 status (E&S):** two workflows built in `verticals/es/`, both under the same
> Option-A pattern — data-driven checks through the shared rules engine where they genuinely
> fit, compound cross-field logic native in the workflow's own code otherwise:
> - **Market Matching** (`workflows/market_matching/`) — the Matching/Ranking decision core;
>   one `RuleSet` per carrier (premium band, loss-run years, required docs) through the shared
>   engine; semantic class-scope matching, severity hard/soft, and the weighted composite score
>   are native.
> - **Package Assembly** (`workflows/package_assembly/`) — consumes Market Matching's output
>   directly (no re-matching); PA-01..PA-07 (document completeness, the auto-fill grounding
>   boundary, three-state package status, carrier-tailored cover letters) are entirely native —
>   nothing here fits the generic 6-check engine. Notably, it re-derives the underlying
>   submission's `ExtractedModel` via the same shared `ExtractionService` (a documented,
>   deliberate exception to "no re-extraction" — see `submission_resolver.py` — because Market
>   Matching doesn't persist `ExtractedField` rows anywhere queryable yet).
> - **Retail Agent Communication** (`workflows/agent_communication/`) — the lightest build yet:
>   no extraction, no rules-engine checks. Its input is already the structured output of Market
>   Matching / Package Assembly (or a manually-logged trigger, FR-2); the real logic is
>   deterministic RA-TN tone/framing selection + a compliance gate (native in `drafting.py`),
>   with the LLM only turning already-decided facts + framing instructions into prose (never
>   picking the framing itself). Two frozen-enum gaps (no "discarded" `ReviewStatus`, no
>   "compliance sign-off" `ReviewAction`) are worked around with dedicated, workflow-owned
>   router endpoints (`/discard`, `/compliance-clear`) that flip the workflow's own
>   `payload.status`/`payload.requires_compliance_review` fields instead of extending the
>   frozen enums — see `router.py`'s module docstring.
>
> MGA's `verticals/mga/` is untouched by any of the three.

## 6. API namespacing (prevents collisions)
```
/api/core/...                       shared (documents, audit, rules, review)
/api/mga/{workflow}/...             e.g. /api/mga/submission-triage
/api/es/{workflow}/...              e.g. /api/es/market-matching, /api/es/package-assembly,
                                     /api/es/agent-communication
```
A dev only adds routes under their own `/{vertical}/{workflow}` namespace.

## 7. Connectors (Nango)
All Google/Gmail access goes through a shared `ConnectorService` wrapping Nango — never direct Google SDKs in workflow code. See CONNECTORS_NANGO.md.

## 8. LLM layer
A shared `LLMService` wraps **OpenAI** with a strict "use only provided facts / cite sources" contract, behind an `LLMProvider` interface so the model stays a config choice. Model tiers by task: a fast model for routine drafting/classification, a stronger one for hard reasoning or sensitive drafts. Workflows never call the SDK directly — they call `LLMService`.

## 9. Async processing
Ingestion + extraction run as **Arq jobs on Redis** (async-native; chosen over Celery) — email arrival → queue → process → output ready — so the API stays responsive. Every run is tracked in the `job_run` table; failed processing is marked `error` and lands in a visible, **queryable error queue** (`JobRunService.errors`), never dropped. Because the DB records job state, the error queue is inspectable even when Redis is down.
