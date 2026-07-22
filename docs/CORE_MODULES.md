# Core Modules (shared — build once)

Every workflow, in either vertical, is assembled from these. Each exposes a **stable interface** (frozen in Phase 1) so workflow code depends on the interface, never the implementation.

| Module | Responsibility | Key interface (simplified) | Used by |
|---|---|---|---|
| **tenancy** | Resolve tenant + `vertical` from the request; scope all data | `getContext() → { tenantId, vertical, userId }` | everything |
| **auth** | RBAC roles (`junior`/`senior`/`admin`), authority limits, guards | `@Roles()`, `can(user, action, amount?)` | all controllers |
| **ingestion** | Pull email + attachments via Nango; scheduled triggers; send mail | `fetchInbox()`, `getAttachments()`, `sendEmail()` | triage, renewal, comms |
| **documents** | Store/retrieve raw docs + metadata; classification confidence | `save()`, `get()`, `listForSubmission()` | extraction, all detail views |
| **extraction** | Classify each doc; extract a **cited** structured field model | `classify(doc)`, `extract(doc) → ExtractedField[]` | all workflows |
| **rules-engine** | Generic, versioned rule evaluator. Runs **validation** rules (per document) and **appetite/matching** rules (config-driven). Check types: `required · regex · min · max · compare · crossDoc` | `evaluate(ruleSet, data) → Result[]`; `publish/rollback(version)` | triage, renewal, endorsement, governance |
| **llm** | Claude wrapper: grounded generation, citation-enforced, model-tier routing | `draft(prompt, facts) → { text, citations }` | narratives, comms, summaries |
| **review-queue** | Items awaiting a human; role-based actions; status | `enqueue(item)`, `act(itemId, action, user)` | every workflow output |
| **audit** | Immutable decision/audit log (AI + human), the source for Governance & Portfolio | `record(entry)`, `query(filter)` | every decision; #8/#9 read it |
| **reporting** | Aggregation/rollup framework over audit + entities | `rollup(dimension, period)` | portfolio, pipeline, governance |
| **jobs** | BullMQ queues + workers; error queue for failed processing | `enqueueJob()`, worker registration | ingestion, extraction |
| **common** | Shared DTOs / entities / interfaces (the **contracts**) | types only | everything |

## The Rules Engine is the key shared trick
Both verticals' "rule" needs — validation (is the doc complete/consistent?), MGA appetite (do we want it?), and E&S matching config — run on the **same generic evaluator** driven by **data (rule sets)**, not code. A rule set is JSON with typed checks; the FE Rules Console authors and versions them. So:
- MGA appetite rules = a rule set the evaluator runs on the consolidated data.
- E&S carrier-appetite/matching = rule sets per carrier.
- Validation (ACORD/Financials/Loss Run/SOV/Combined) = per-document rule sets.

The **Decision Core** per vertical is a thin layer that *orchestrates* which rule sets run and how their results map to a recommendation (`PROCEED/REQUEST_INFO/DECLINE` for MGA; ranked carrier matches for E&S).

## LLM contract (non-negotiable)
`LlmService.draft()` must:
- receive **only extracted/structured facts** (never raw docs blindly),
- **cite the source** for each claim,
- **never fabricate** — missing data → "not available in submitted documents".
This is enforced in the wrapper so every workflow inherits it.

## What is NOT shared (per vertical)
- `verticals/mga/decision-core` — **Appetite Engine** (hard-rule pass/fail → in/out of appetite, risk score).
- `verticals/es/decision-core` — **Matching/Ranking Engine** (risk → carrier appetite fit, ranked).
- Each workflow's orchestration + its vertical-specific schema.
