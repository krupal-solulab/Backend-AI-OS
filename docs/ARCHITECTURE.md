# Architecture

## 1. Principles
1. **One platform, two verticals.** Multi-tenant. Every tenant has a `vertical` = `MGA` | `ES`. The vertical selects which decision core, rules, schemas, and workflows apply.
2. **Shared core built once.** Extraction, ingestion, rules engine, LLM, review queue, audit, reporting, auth — all vertical-agnostic. ~50% of the backend.
3. **Thin vertical layer.** Only the *decision core* (MGA appetite vs E&S matching/ranking) and the *workflow orchestration* differ.
4. **Human-in-the-loop, always.** Nothing binds, sends, or writes back to a system of record without an explicit human action. AI drafts + recommends; a person approves.
5. **Everything is grounded + audited.** Every AI claim cites a source; every decision is written to the audit log.

## 2. Modular monolith
A single deployable NestJS app made of independent modules. This gives us microservice-style **ownership boundaries** (each workflow is its own module) without the ops cost. Split into services later only if needed.

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

## 6. API namespacing (prevents collisions)
```
/api/core/...                       shared (documents, audit, rules, review)
/api/mga/{workflow}/...             e.g. /api/mga/submission-triage
/api/es/{workflow}/...              e.g. /api/es/market-matching
```
A dev only adds routes under their own `/{vertical}/{workflow}` namespace.

## 7. Connectors (Nango)
All Google/Gmail access goes through a shared `ConnectorService` wrapping Nango — never direct Google SDKs in workflow code. See CONNECTORS_NANGO.md.

## 8. LLM layer
A shared `LlmService` wraps Anthropic Claude with a strict "use only provided facts / cite sources" contract. Model tiers by task: a fast model (Sonnet/Haiku class) for routine drafting/classification, a stronger model (Opus class) for hard reasoning or sensitive drafts. Workflows never call the SDK directly — they call `LlmService`.

## 9. Async processing
Ingestion + extraction run as **BullMQ jobs** (email arrival → queue → process → output ready), so the API stays responsive and failed processing lands in a visible **error queue**, never dropped.
