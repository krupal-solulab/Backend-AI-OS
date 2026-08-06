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
> **+ MGA Renewal Management (Workflow_2)** is built the same way (`verticals/mga/renewal_management/`):
> a Renewal Comparison Engine (RN-01..RN-12, thresholds as `RenewalConfig` data; RN-09 reuses the
> Appetite Engine's `AppetiteConfig` data) maps the frozen `Decision` → the FE `RenewalRecommendation`
> in the workflow layer. Routes under `/api/mga/renewal`. Still no `core/common` or `verticals/es` changes.
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
> - **Quote Comparison** (`workflows/quote_comparison/`) — the biggest build in the vertical so
>   far: a genuinely NEW extraction target (unstructured carrier-response emails, not ACORD/
>   loss-run) parsed by a native `quote_parser.py` — deliberately NOT the shared
>   `ExtractionService`, since that parses `Label: value` strictly per line, which breaks on this
>   dataset's own most important field (subjectivity clauses wrapping across lines). QC-01
>   (comparability), QC-02 (subjectivity materiality), QC-06 (mode selection), QC-07 (validity
>   urgency) are entirely native comparison reasoning — offers compared against each other, not a
>   submission against a fixed appetite standard, so none of it fits the generic 6-check engine
>   either. QC-07's validity monitoring is recomputed at READ TIME on every `GET` (no new
>   scheduled-job infra, same deferred-scheduler call made for Agent Communication's
>   `NO_RESPONSE_FOLLOWUP`). Feeds Agent Communication's `QUOTE_TERMS_SUMMARY` — but only when the
>   broker explicitly selects a quote (`/select/{quote_id}`), never automatically from its own
>   `/run`, since the PRD requires the broker's decision to be the trigger (extends
>   `verticals/es/agent_communication_hooks.py`, the same cross-workflow hook Market Matching/
>   Package Assembly already use, one function added, no changes to how the others fire).
>
> - **Binder & Policy Issuance Coordination** (`workflows/binder_issuance/`) — closes the full
>   placement lifecycle. Introduces a THIRD extraction target this vertical (carrier bind
>   confirmation emails, issued-policy declarations pages) parsed by a native `bind_parser.py` —
>   independent of Quote Comparison's `quote_parser.py` (no cross-import; the declarations-page
>   format alone is different enough that a shared implementation wouldn't fully unify them).
>   BI-03/BI-05 (never trust a carrier's own confirmation/issued policy without field-by-field
>   reconciliation) and BI-06 (downstream triggers fire only on a verified-clean state) are the
>   core native logic. BI-04/BI-07's ongoing monitoring reuses the same deferred "recompute at
>   read time" pattern as Quote Comparison's QC-07 (no new scheduler built — this is now the
>   third instance of this deferral, tracked honestly rather than silently repeated). Extends
>   `agent_communication_hooks.py` with a second auto-fire pattern (fires from THIS workflow's
>   own `/run`/resolve actions, like Market Matching/Package Assembly — not Quote Comparison's
>   select-is-the-trigger pattern) and adds a 7th Agent Communication trigger type,
>   `POLICY_DOCUMENTS_DELIVERED` (per that PRD's FR-19, a coordinated extension).
>
> - **Endorsement / Mid-Term Change Processing** (`workflows/endorsement/`) — the first workflow
>   to reuse substantial logic from TWO prior workflows simultaneously rather than extending just
>   one: Market Matching's `CarrierProfile`/`load_carrier_panel` DATA layer (reused directly, since
>   `decision_core` is E&S-vertical-shared infrastructure — a different boundary than the
>   no-cross-import rule between sibling workflow folders) for EP-02's three-outcome appetite
>   recheck, and Binder & Issuance's never-trust-the-carrier-document reconciliation discipline
>   for EP-05 — though EP-05's reconciliation shape is genuinely different (item-level
>   presence/absence checking across a LIST of requested vs. issued items, e.g. two additional
>   insureds where the carrier only processes one, not scalar-field equality). EP-01's
>   classification is type-based first, then materiality within type — never a single flat
>   "how big is this change" score across every type. Extends `agent_communication_hooks.py` with
>   an 8th trigger type, `ENDORSEMENT_CONFIRMED` (per that PRD's FR-16, a coordinated extension
>   like Binder & Issuance's own `POLICY_DOCUMENTS_DELIVERED` addition).
>
> - **Renewal Remarketing** (`workflows/renewal_remarketing/`) — an ORCHESTRATION workflow, not
>   a new capability: RR-05 genuinely re-invokes the existing `MarketMatchingPipeline` directly
>   (a stronger, more explicit reuse mandate than anywhere else in this vertical — the literal
>   pipeline object, not just its data), and RR-06 replicates Quote Comparison's QC-01
>   discipline natively (a small 2-offer comparison, not that engine's full multi-quote logic).
>   RR-01/RR-02's exposure/loss-change detection is deliberately NOT a port from MGA Renewal
>   Management — that workflow was never built in this codebase (confirmed by inspection:
>   `verticals/mga/workflows/` is empty, no Workflow_2 fixture data exists), so this is fresh
>   native logic informed only by the PRD's description. RR-04's four-state trigger decision
>   (`NO_REMARKET`/`LIGHT_REMARKET_CHECK`/`FULL_REMARKET`/`URGENT_REMARKET`) is the workflow's
>   central judgment call and is never collapsed to binary. RR-05's re-invocation is a separate,
>   broker-approval-gated router action, not automatic inside `/run` (matching the PRD's own
>   step sequence) — with a stated limitation: it runs against the original Workflow_10 bind-time
>   submission, since this dataset ships no fresh renewal-time documents.
>
> - **Diligent Search & Compliance Documentation** (`workflows/diligent_search/`) — the highest
>   legal-stakes workflow in the vertical (PRD §8): a wrongly generated affidavit is a potentially
>   fraudulent record, not just a bad recommendation. DS-04's document-generation gate is enforced
>   structurally — `compliance_engine.determine_state` only marks a state `document_eligible` on a
>   confirmed SUFFICIENT determination, so `draft()` never has a partial/best-effort path to
>   bypass. DS-02's central judgment call (FR-7): an unconditional export-list note (Texas, "IS on
>   the export list... not required") auto-resolves to EXEMPT, but a hedged, account-specific note
>   (Florida, "MAY be export-eligible... for large commercial accounts") routes to
>   PENDING_DETERMINATION instead, detected via hedge-language matching rather than trusting
>   `export_list_class: true` alone — never auto-resolved without human/legal review. DS-03
>   distinguishes "evidence gathered and falls short" (INSUFFICIENT, a confirmed gap) from "state
>   confirmed REQUIRED but no evidence submitted for assessment yet" (a NOT_APPLICABLE variant,
>   genuinely different from an EXEMPT state's NOT_APPLICABLE) — collapsing these would either
>   wrongly call an untouched state a confirmed failure or wrongly hide a real one.
>   `retention_period_years` is always `null` — no scenario supplies real per-state legal
>   reference data, and FR-8 calls that a required discovery input, not something to derive from
>   reasoning. No new Agent Communication trigger, scheduled job, or cross-workflow re-invocation
>   — re-scanned all 8 FRs; none applies here.
>
> - **Carrier Appetite Intelligence Tracking** (`workflows/carrier_appetite_intelligence/`) — the
>   LAST E&S workflow in Phase 3, and this PRD's own Section 0 calls it the highest scope-creep
>   risk in the vertical at every prior mention (Market Matching §2.2, Quote Comparison QC-03,
>   Renewal Remarketing §2.2) — the v1 built here stays deliberately narrow: aggregate already-
>   logged signals, distinguish genuine class-level shifts (CI-02) from account-specific variance,
>   and auto-update exactly two metadata fields (CI-03). CI-02's core judgment call is verified by
>   hand against all 4 scenarios: an inconsistent outcome with an ACCOUNT-SPECIFIC stated reason
>   (Scenario 03: "severity exceeded ceiling for this specific account") contributes ZERO toward
>   class-level evidence, regardless of ratio — never scored like Scenario 02's genuine pattern
>   (2 of 3 recent declines explicitly citing "class no longer written"). The central architectural
>   finding: NO mutable Carrier Appetite Profile store or profile-editing interface exists anywhere
>   in this codebase (Market Matching's `CarrierProfile` is a frozen dataclass loaded fresh from
>   read-only JSON) — so CI-03's metadata refresh and CI-04's suggestions are computed and
>   RECORDED in this workflow's own payload, never applied to any real profile, a stated limitation
>   rather than new shared mutable infrastructure. Resolves each carrier's REAL stated profile from
>   Workflow_10's `carrier_profiles/*.json` via `decision_core.carrier_profiles.load_carrier_panel`
>   — E&S-vertical-shared DATA-layer reuse, same precedent as Endorsement's reuse of that module.
>   FR-1's periodic-batch framing continues this vertical's deferred-scheduled-job pattern for the
>   5th time — no new Arq/cron infra, a manual on-demand `/run` per carrier/class combination.
>
> - **Pipeline & Carrier Performance Reporting** (`workflows/pipeline_reporting/`) — the 10th and
>   LAST workflow on the ORIGINAL E&S roadmap. A pure aggregation/reporting layer, structurally
>   different from every prior workflow: its 4 test scenarios each represent a DIFFERENT report
>   kind (clean funnel, carrier hit-rate, funnel-with-a-gap, remarketing value), detected from the
>   scenario's own top-level JSON keys, rather than 4 instances of one shape. PR-06 (data
>   completeness) is this PRD's equivalent of every other workflow's highest-stakes gate — a
>   funnel-stage value that isn't an int is treated as a logging-gap marker (its text becomes the
>   gap's reason), and BOTH that stage's own percentage AND the immediately-following stage's
>   percentage render as explicitly withheld, never interpolated, even though the following stage's
>   raw count may still be independently reliable. `overall_conversion_pct` is withheld entirely
>   whenever ANY gap exists anywhere in the funnel, even if both endpoints are individually known —
>   a clean top-line figure next to a flagged gap would undercut the gap's prominence. PR-02's
>   carrier list sorts by volume, never by hit-rate, so a low-volume carrier's inflated percentage
>   (Vantage's 100% of 4) never visually outranks a higher-volume, more reliable one (Ironclad's
>   63.6% of 22). PR-05 adds a third `remarketing_value` outcome type, `not_remarketed`, beyond the
>   literal 2-value schema, for accounts that were never actually shopped — distinguishing them from
>   both a genuine `$` savings figure and a `$0`-savings `confirmation_value` outcome (a remarket
>   that confirms the incumbent was right, never reported as a failure, per Renewal Remarketing's
>   own RR-04 precedent). PR-04 (revenue attribution) is fully out of scope — no field anywhere,
>   per the PRD's own "do not build this rule from assumption." No live cross-workflow DB
>   aggregation is attempted (every scenario's data is itself a pre-aggregated snapshot this
>   fixture-driven codebase has no live equivalent of); a Phase-1 `core/reporting` module already
>   exists but is a generic `AuditEntry` group-by-count rollup that doesn't match this dataset's
>   shape, so it isn't used here. No `approve`/`escalate` router actions, unlike every prior
>   workflow — a report isn't a determination a human approves or declines.
>
> MGA's `verticals/mga/` is untouched by any of the ten.

## 6. API namespacing (prevents collisions)
```
/api/core/...                       shared (documents, audit, rules, review)
/api/mga/{workflow}/...             e.g. /api/mga/submission-triage
/api/es/{workflow}/...              e.g. /api/es/market-matching, /api/es/package-assembly,
                                     /api/es/agent-communication, /api/es/quote-comparison,
                                     /api/es/binder-issuance, /api/es/endorsement,
                                     /api/es/renewal-remarketing, /api/es/diligent-search,
                                     /api/es/carrier-appetite-intelligence,
                                     /api/es/pipeline-reporting
```
A dev only adds routes under their own `/{vertical}/{workflow}` namespace.

## 7. Connectors (Nango)
All Google/Gmail access goes through a shared `ConnectorService` wrapping Nango — never direct Google SDKs in workflow code. See CONNECTORS_NANGO.md.

## 8. LLM layer
A shared `LLMService` wraps **OpenAI** with a strict "use only provided facts / cite sources" contract, behind an `LLMProvider` interface so the model stays a config choice. Model tiers by task: a fast model for routine drafting/classification, a stronger one for hard reasoning or sensitive drafts. Workflows never call the SDK directly — they call `LLMService`.

## 9. Async processing
Ingestion + extraction run as **Arq jobs on Redis** (async-native; chosen over Celery) — email arrival → queue → process → output ready — so the API stays responsive. Every run is tracked in the `job_run` table; failed processing is marked `error` and lands in a visible, **queryable error queue** (`JobRunService.errors`), never dropped. Because the DB records job state, the error queue is inspectable even when Redis is down.
