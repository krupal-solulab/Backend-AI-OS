# FE Wiring Contract — MGA Renewal Management (`/api/mga/renewal`)

Live contract for wiring the MGA-FE **RenewalManagement** screen to the backend. Shapes
match the FE's locked `RenewalDetail` / list types. Sample JSON below is generated from
**real Workflow_2 runs** (not hand-written).

## Auth headers (demo stub — same as Triage)
Every request carries:
```
x-tenant-id: demo-mga
x-user-id:   demo-mga-senior      # or demo-mga-junior
x-role:      senior               # junior | senior | admin
```

## Endpoints
| Method | Path | Purpose |
|---|---|---|
| GET  | `/api/mga/renewal` | inbox list (`RenewalRow[]`) |
| POST | `/api/mga/renewal/run?message_id=renewal_01` | run+persist one case (dev/ingestion trigger; no bulk endpoint yet) |
| GET  | `/api/mga/renewal/{submission_id}` | `RenewalDetail` |
| POST | `/api/mga/renewal/{submission_id}/act` | body `{ "action": "approve"｜"send"｜"escalate", "amount?": number }` |

`send` = human-triggered broker outreach (RN-12 draft) — **never auto-sent**; senior/admin only (junior → 403).

## Recommendation mapping (backend `DecisionOutcome` → FE `RenewalRecommendation`)
Done in the workflow layer; `core/common` `Decision` stays frozen (PROCEED/REQUEST_INFO/DECLINE).
- **PROCEED + no non-timing change-flags → `RENEW_AS_IS`**
- **PROCEED + change-flags (RN-01/02/03/04/05/06/08) → `RENEW_WITH_CHANGES`**
- **DECLINE (RN-09 hard) → `NON_RENEW`** (`appetiteDrift` set, `hardRulePassed=false`)
- **REQUEST_INFO (missing docs / RN-12 no questionnaire) →** recommendation `RENEW_WITH_CHANGES` **with `needsInfo=true` + `missingInfo[]`** (the FE enum has no request-info value; render the missing-info state).
- Timing-only (RN-11) stays `RENEW_AS_IS` with `timing.lapseRisk=true`.

## Sample — list row (`GET /api/mga/renewal`), real
```json
[
  { "id": "e4447e1d-…", "insured": "Riverside Landscaping LLC", "recommendation": "RENEW_AS_IS",
    "score": 100, "retention": "neutral", "daysToExpiration": 57, "lapseRisk": false,
    "status": "pending", "received": "Mon, 06 Jul 2027 09:00:00 -0500" },
  { "id": "50bd0de8-…", "insured": "Golden Gate Auto Repair Inc.", "recommendation": "RENEW_WITH_CHANGES",
    "score": 70, "retention": "at-risk", "daysToExpiration": 57, "lapseRisk": false,
    "status": "pending", "received": "…" }
]
```

## Sample — `RENEW_WITH_CHANGES` detail (renewal_03, real, trimmed)
```json
{
  "recommendation": "RENEW_WITH_CHANGES", "confidence": 1.0, "processing": "ready",
  "priorSource": "PAS", "rulesVersion": "mga.renewal_management.validation v1",
  "rulesVersionAtBinding": "mga.renewal_management.validation v1", "hardRulePassed": true,
  "appetite": [ { "rule": "RN-09/HR-01 excluded class", "pass": true, "hard": true, "detail": "Class 84300" } ],
  "appetiteDrift": null,
  "comparison": [ { "label": "Stated revenue", "prior": "$2,180,000", "current": "$2,250,000",
                    "change": "+3%", "direction": "unfavorable", "strong": false } ],
  "changeFlags": [
    { "category": "loss", "label": "Loss deterioration", "detail": "2 new claim(s) in expiring term; largest open $145,000", "direction": "unfavorable" },
    { "category": "loss", "label": "Frequency trend break", "detail": "Zero claims in prior years, then 2 in the expiring term", "direction": "unfavorable" } ],
  "lossChanges": [
    { "type": "new_claim", "description": "2 new claim(s) in expiring term; largest open $145,000", "direction": "unfavorable", "source": "loss_run" },
    { "type": "trend", "description": "Trend break: claim-free history → new claim(s) this term", "direction": "unfavorable", "source": null } ],
  "timing": { "daysToExpiration": 57, "lapseRisk": false, "noSubmission": false },
  "changes": [ { "item": "Loss deterioration", "reason": "2 new claim(s) …", "source": "RN-06" },
               { "item": "Frequency trend break", "reason": "Zero claims …", "source": "RN-08" } ],
  "narrative": "…grounded, cited renewal summary…",
  "citations": [ "prior_policy_snapshot.txt:line 2", "renewal_questionnaire.txt:line 6", "loss_run.txt:line 29" ],
  "broker": { "name": "Priya Nair", "agency": "Vantagebrokerage", "tenure": "—", "note": "" },
  "activity": [ { "at": "2027-…", "who": "system (AI)", "what": "Renewal compared → RENEW_WITH_CHANGES", "conf": "100%" } ],
  "needsInfo": false, "missingInfo": [], "retention": "at-risk"
}
```

## Sample — `NON_RENEW` (renewal_04, appetite drift, real)
```json
{ "recommendation": "NON_RENEW", "hardRulePassed": false,
  "appetiteDrift": "was in appetite at binding; current appetite rules exclude class 96065 (appetite drift, RN-10) → NON_RENEW",
  "appetite": [ { "rule": "RN-09/HR-01 excluded class", "pass": false, "hard": true, "detail": "Class 96065" } ],
  "narrative": "Non-renewal: … Narrative suppressed pending underwriter confirmation…", "citations": [], "retention": "at-risk" }
```

## Field → source map (`RenewalDetail`)
| FE field | Source |
|---|---|
| `recommendation` | mapped from `Decision.outcome` + change-flags (see mapping above) |
| `confidence` | min extraction confidence (`Decision.details.extraction_confidence`) |
| `processing` | `"ready"` (or `"error"` on pipeline failure) |
| `priorSource` | `"PAS"` when `prior_policy_snapshot` present, else `"manual_queue"` |
| `rulesVersion` | resolved published `RuleVersion` label |
| `rulesVersionAtBinding` | GAP — no prior-term rule-version record; placeholder label (drift cases note pre-revision) |
| `hardRulePassed` | RN-09 appetite recheck (false → NON_RENEW) |
| `appetite[]` | RN-09 recheck results (reuses MGA `AppetiteConfig` data) |
| `appetiteDrift` | RN-10 narrative note when RN-09 excludes the class |
| `comparison[]` | prior_policy vs renewal_questionnaire deltas (revenue, employees, states) |
| `changeFlags[]` | RN-01..RN-08 fired flags |
| `lossChanges[]` | RN-06/07/08 loss deltas from the updated loss run |
| `timing.*` | RN-11: business days from `email.date` → `renewal_questionnaire.effective_date_requested` |
| `changes[]` | non-timing change-flags as "what's changing & why" (FR-22) |
| `narrative` / `citations[]` | grounded LLM draft over cited extracted facts |
| `broker.name` | parsed from `email.from` |
| `broker.agency` | approximated from the email domain (GAP — not authoritative) |
| `broker.tenure` | **GAP** — not in the data → `"—"` |
| `broker.note` | **GAP** — not in the data → `""` |
| `activity[]` | audit entries (AI triage + human actions) |
| `needsInfo` / `missingInfo[]` | REQUEST_INFO surface (additive; the FE enum has no request-info value) |
| `retention` | favorable / neutral / at-risk (additive signal) |
| `priorPremium` (detail + list) | **now surfaced** from `prior_policy.expiring_premium` (`"$…"`) |
| `lossRatio` (detail + list) | **now surfaced** — 5yr incurred / (prior premium × 5), e.g. `"177%"` |
| `indicated` / `premiumChange` (`change`) | **GAP** `"—"` — workflow computes no re-rate yet |

Detail also carries a `Premium` row in `comparison[]` (prior → indicated) when a prior premium exists.

## GAPs (render gracefully; confirm before relying on them)
- `indicated` / `change` (premium re-rate + delta) — **GAP** `"—"` until the workflow computes an
  indicated renewal premium (a future re-rate step). `priorPremium` and `lossRatio` are now real.
- `broker.tenure`, `broker.note` — not provided by the data (`"—"` / `""`).
- `broker.agency` — derived from the email domain, not an authoritative agency name.
- `rulesVersionAtBinding` — no stored prior-term rule version; placeholder.
- `RenewalRow` (list shape) — modeled from screen needs; confirm against the FE `renewals`
  mock if it diverges (e.g. broker/effective columns).
