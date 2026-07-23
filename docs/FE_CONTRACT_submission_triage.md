# FE ↔ BE Contract — MGA Submission Triage

Wiring spec for `MGA-FE` against the Phase-2 backend. Endpoints, **real** sample responses
(generated from actual Workflow_1 runs — not hand-written), field provenance, and a
FE-field → API-path mapping table with any gaps flagged.

Base URL (dev): `http://localhost:4000`  ·  All routes are tenant/role scoped by headers.

## Auth headers (Phase-0 stub — every request)
```
x-tenant-id: demo-mga
x-user-id:   demo-mga-junior          # or demo-mga-senior
x-role:      junior                    # junior | senior | admin
```
`vertical` is resolved from the tenant row (fallback header `x-vertical: MGA`).

## Endpoints
| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET` | `/api/mga/submission-triage` | Inbox list — one row per triaged submission (`Submission[]`) | headers |
| `GET` | `/api/mga/submission-triage/{submission_id}` | Detail — `TriageDetail` | headers |
| `POST` | `/api/mga/submission-triage/{submission_id}/act` | Human action | headers + body |
| `POST` | `/api/mga/submission-triage/run?message_id=submission_01` | Dev/ingestion trigger: run pipeline + persist (prod: the Arq ingestion job calls the same service) | headers |

`act` body: `{ "action": "approve" | "send" | "escalate", "amount"?: number, "note"?: string }`
- `approve` — enforces `JUNIOR_PREMIUM_CAP` (junior over cap → `403`).
- `send` — human-triggered request-info to the broker (there is **no** auto-send anywhere).
- `escalate` — any role.
Response: `{ "id": "<review_item_id>", "status": "approved|sent|escalated|..." }`.

`{submission_id}` is the `id` from the list row / detail (the persisted submission id).

---

## Sample 1 — `GET /{id}` · PROCEED (submission_01, Riverside Landscaping)
Real response (abridged: `fields[]` shows 6 of 39 — identical shape throughout):
```json
{
  "recommendation": "PROCEED",
  "confidence": 1.0,
  "hardRulePassed": true,
  "failedRules": [],
  "processing": "ready",
  "rulesVersion": "mga.submission_triage.validation v1",
  "meta": { "received": ["acord","email","financials","loss_run"], "lowConfidence": [],
            "timestamp": "2026-07-23T10:53:27.770777+00:00" },
  "docs": [
    { "name": "acord_application.txt", "kind": "acord", "pages": 1, "fields": 16, "confidence": 1.0, "classified": true },
    { "name": "loss_run.txt", "kind": "loss_run", "pages": 1, "fields": 13, "confidence": 1.0, "classified": true }
  ],
  "fields": [
    { "key": "acord.named_insured", "label": "Named Insured", "value": "Riverside Landscaping LLC", "required": true, "confidence": 1.0, "source": "acord_application.txt:line 3" },
    { "key": "acord.fein", "label": "Fein", "value": "57-1122334", "required": false, "confidence": 1.0, "source": "acord_application.txt:line 5" },
    { "key": "acord.class_code", "label": "Class Code", "value": "97047 - Landscaping/Gardening Services", "required": false, "confidence": 1.0, "source": "acord_application.txt:line 6" },
    { "key": "acord.stated_annual_revenue", "label": "Stated Annual Revenue", "value": "$2,450,000", "required": false, "confidence": 1.0, "source": "acord_application.txt:line 15" },
    { "key": "loss_run.total_incurred", "label": "Total Incurred", "value": "2850.0", "required": false, "confidence": 1.0, "source": "loss_run.txt" },
    { "key": "loss_run.total_incurred_period", "label": "Total Incurred Period", "value": "5yr", "required": false, "confidence": 1.0, "source": "loss_run.txt" }
  ],
  "loss": { "totalIncurred": "$2,850", "totalPaid": "$2,850", "openClaims": 0, "years": 5, "required": 5, "trend": "improving" },
  "consistency": [
    { "label": "Revenue consistency", "detail": "Application $2,450,000 vs financials $2,410,000 (2%)", "status": "ok" }
  ],
  "missingInfo": [],
  "factors": [
    { "name": "Class code", "value": "97047 - Landscaping/Gardening Services", "weight": 3 },
    { "name": "Annual revenue", "value": "$2,450,000", "weight": 2 },
    { "name": "Loss trend", "value": "improving", "weight": 3 },
    { "name": "Loss ratio", "value": "15%", "weight": 2 }
  ],
  "narrative": "[mock:gpt-4o] Draft grounded in provided facts. ...",
  "citations": [
    "acord_application.txt:line 3", "acord_application.txt:line 6",
    "acord_application.txt:line 15", "acord_application.txt:line 16",
    "loss_run.txt", "financial_statement.txt:line 7"
  ],
  "appetite": [
    { "rule": "EC-01", "pass": true, "hard": false, "detail": "Extraction confidence acceptable" },
    { "rule": "HR-01", "pass": true, "hard": true, "detail": "Class code 97047 is acceptable" },
    { "rule": "HR-02", "pass": true, "hard": true, "detail": "No hard-severity breach" },
    { "rule": "CC-01", "pass": true, "hard": false, "detail": "Revenue variance 2%" },
    { "rule": "TR-01", "pass": true, "hard": false, "detail": "Lead time 36 business day(s)" },
    { "rule": "LT-02", "pass": true, "hard": false, "detail": "Loss severity trend: improving" }
  ],
  "activity": [
    { "at": "2026-07-23T10:53:27.770777+00:00", "who": "system (AI)", "what": "Auto-triaged -> PROCEED", "ctx": null, "conf": "100%" }
  ]
}
```
> **Narrative note:** `narrative` here is the offline **mock** LLM echo (no `OPENAI_API_KEY` set).
> With a real key it becomes a grounded 2–3 sentence summary; `citations` are already real and
> trace only to provided facts.

## Sample 2 — `GET /{id}` · REQUEST_INFO with citations (submission_09, Meridian Self Storage)
Real decision arrays (missing financials + SOV gaps + SOV-vs-limit):
```json
{
  "recommendation": "REQUEST_INFO",
  "confidence": 1.0,
  "hardRulePassed": true,
  "failedRules": ["CR-02", "CR-03", "CR-04"],
  "processing": "ready",
  "meta": { "received": ["acord","email","loss_run","sov"], "lowConfidence": [], "timestamp": "..." },
  "loss": { "totalIncurred": "$41,000", "totalPaid": "$41,000", "openClaims": 0, "years": 5, "required": 5, "trend": "improving" },
  "missingInfo": [
    { "item": "Financial statement", "reason": "No financial statement was provided with the submission.", "severity": "required" },
    { "item": "SOV business income", "reason": "Location(s) [5] missing business income.", "severity": "required" }
  ],
  "consistency": [
    { "label": "SOV vs requested limit", "detail": "SOV totals $14,280,000 against a $12,500,000 blanket limit", "status": "fail" }
  ],
  "citations": [
    "acord_application.txt:line 3", "acord_application.txt:line 6",
    "acord_application.txt:line 15", "acord_application.txt:line 16", "loss_run.txt"
  ]
}
```
> Citations trace only to documents actually present — note **no** `financial_statement.txt`
> citation appears (the financials were never provided), which is exactly why `CR-02` fires.

Also relevant (not shown): **submission_03** → `recommendation: "DECLINE"`, `hardRulePassed: false`,
`failedRules: ["HR-01","HR-02"]`, `citations: []` (narrative suppressed before the LLM, FR-23);
**submission_08** → `recommendation: "REQUEST_INFO"`, `failedRules: ["EC-01"]`, non-empty
`meta.lowConfidence`, and a `factors[]` entry `{ "name": "Manual review", ... }`.

## Sample 3 — `GET /` list rows (`Submission[]`)
```json
[
  { "id": "169fcf60-...", "insured": "Riverside Landscaping LLC", "industry": "Landscaping/Gardening Services",
    "state": "South Carolina", "tiv": "—", "premium": "$18,400", "score": 100,
    "appetite": "In appetite", "recommendation": "PROCEED", "status": "pending", "received": "Mon, 13 Jul 2026 09:14:22 -0500" },
  { "id": "797a7bfd-...", "insured": "Apex Roofing & Restoration LLC", "industry": "Roofing - All Types (steep slope and low slope)",
    "state": "South Carolina, Georgia", "tiv": "—", "premium": "$54,000", "score": 0,
    "appetite": "Out of appetite", "recommendation": "DECLINE", "status": "pending", "received": "..." },
  { "id": "c1586af9-...", "insured": "Meridian Self Storage Holdings LLC", "industry": "Self Storage Facilities",
    "state": "Alabama, Mississippi", "tiv": "$14,280,000", "premium": "$68,400", "score": 55,
    "appetite": "Needs info", "recommendation": "REQUEST_INFO", "status": "pending", "received": "..." }
]
```

---

## Field provenance (where each value comes from)
| Response field | Source in the pipeline |
|---|---|
| `recommendation` | `Decision.outcome` (Appetite Engine) |
| `confidence` | min extraction confidence over value fields (`Decision.details.extraction_confidence`) |
| `hardRulePassed` / `failedRules` | `Decision.details.hard_rule_passed` / `.failed_rules` |
| `processing` | pipeline state (sync `run` → `"ready"`) |
| `rulesVersion` | published `RuleVersion` key+version |
| `meta.received` | `documents.<kind>.present` flags from extraction |
| `meta.lowConfidence` | `Decision.details.low_confidence_fields` (EC-01 heuristic) |
| `docs[]` | per raw document from the ConnectorService + extraction field counts |
| `fields[]` | `ExtractedModel.fields` (scalars); `source` = `Citation.filename:locator`; `required` = field appears in a `required` validation rule |
| `loss.*` | canonical `loss_run.total_incurred` / `total_paid` / `open_claims` / `total_incurred_period`; `trend` = `Decision.details.trend` |
| `consistency[]` | `Decision.details.consistency` (CC-01, CC-02, CR-04) |
| `missingInfo[]` | `Decision.details.missing_info` (CR-01, CR-02, CR-03) |
| `factors[]` | `Decision.details.factors` |
| `narrative` | `LLMService.draft().text` (suppressed for DECLINE/manual) |
| `citations[]` | `LLMService.draft().citations` (grounded to provided facts) |
| `appetite[]` | `Decision.details.appetite` (all HR/CR/CC/TR/LT/EC results) |
| `activity[]` | `AuditEntry` records for the submission |
| list `Submission.*` | `acord.named_insured/class_code/states_of_operation/prior_premium`, `sov.total_insurable_value`, `Decision.outcome/score`, `ReviewItem.status` |

## FE field → API path mapping (`TriageDetail` / `Submission`)
| FE type.field | API JSON path | Status |
|---|---|---|
| `TriageDetail.recommendation` | `recommendation` | ✅ |
| `TriageDetail.confidence` | `confidence` | ✅ |
| `TriageDetail.hardRulePassed` | `hardRulePassed` | ✅ |
| `TriageDetail.failedRules` | `failedRules` | ✅ |
| `TriageDetail.processing` | `processing` | ✅ (see GAP-1) |
| `TriageDetail.rulesVersion` | `rulesVersion` | ✅ |
| `TriageDetail.meta.{received,lowConfidence,timestamp}` | `meta.*` | ✅ |
| `TriageDoc.{name,kind,pages,fields,confidence,classified}` | `docs[].*` | ✅ (see GAP-2: `pages`) |
| `ExtractedField.{key,label,value,required,confidence,source}` | `fields[].*` | ✅ |
| `LossMetrics.{totalIncurred,totalPaid,openClaims,years,required,trend}` | `loss.*` | ✅ |
| `ConsistencyCheck.{label,detail,status}` | `consistency[].*` | ✅ |
| `MissingItem.{item,reason,severity}` | `missingInfo[].*` | ✅ |
| `RiskFactor.{name,value,weight}` | `factors[].*` | ✅ |
| `TriageDetail.narrative` | `narrative` | ✅ |
| `TriageDetail.citations` | `citations` | ✅ |
| `AppetiteResult.{rule,pass,hard,detail}` | `appetite[].*` | ✅ (`pass` key emitted) |
| `ActivityEntry.{at,who,what,ctx,conf}` | `activity[].*` | ✅ (`ctx` null when N/A) |
| `Submission.{insured,industry,state,tiv,premium,score,appetite,recommendation,status,received}` | list `[].*` | ✅ |

### Gaps / notes (catch before wiring)
- **GAP-1 `processing`:** the sync `run` endpoint always returns `"ready"`; `"queued"`/`"extracting"`
  are only meaningful once triage is driven by the async Arq ingestion job, and `"error"` is
  currently surfaced as an HTTP error status rather than a body with `processing:"error"`. FE should
  treat a non-200 as the error state for now.
- **GAP-2 `docs[].pages`:** fixtures are single `.txt` files → always `1`. Real multi-page
  PDFs will populate this once OCR ingestion lands.
- **`confidence` vs list `score`:** `TriageDetail` has **no** `score` field (matches the FE type);
  the appetite **score** (0–100) is on the list `Submission.score` only. `TriageDetail.confidence`
  is the 0–1 extraction confidence. Manual-review (submission_08) → list `score: null`.
- **`narrative`** is the mock echo until `OPENAI_API_KEY` is set (see Sample 1 note).
- No missing/renamed fields otherwise — the API was built to this exact `TriageDetail`/`Submission`
  shape, so wiring should be a mechanical swap of the FE's `mocks.ts` for these endpoints.
