# FE Contract — E&S Endorsement / Mid-Term Change Processing Copilot

`/api/es/endorsement` — the sixth E&S workflow. Classifies mid-term change
requests on already-bound policies by type and materiality, rechecks
carrier appetite when a change touches class/state/severity exposure
(three-outcome model — within/outside/genuinely unknown, never collapsing
the third into either of the first two), flags premium impact and
proration inputs without asserting a figure the carrier hasn't confirmed,
and reconciles the carrier's issued endorsement against the original
request item by item. Reuses Market Matching's Carrier Appetite Profile
data and Binder & Policy Issuance's never-trust-the-carrier-document
discipline. See `docs/WORKFLOW_TEMPLATE.md`, `docs/PHASES_AND_STAGES.md`
(Phase 3), and this workflow's `RULE_ENGINE_INTERPRETATION_GUIDE.md` (in
`Workflow_15/test_dataset`) for the full EP-01..EP-06 rule spec.

Auth: Phase-0 header stub — every request needs `x-tenant-id`, `x-user-id`,
`x-role` (`junior|senior|admin`).

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/es/endorsement/run` | Run classification (pre-issuance) or reconciliation (post-issuance) for one scenario |
| GET | `/api/es/endorsement` | List this tenant's endorsement review items |
| GET | `/api/es/endorsement/{item_id}` | One request's full detail |
| POST | `/api/es/endorsement/{item_id}/resolve-discrepancy` | Broker resolves an EP-05 partial-fulfillment discrepancy — required before the endorsement-confirmed trigger can fire |
| POST | `/api/es/endorsement/{item_id}/send` | Send the request (routine or underwriting-review framing — both use this same action; **senior/admin only**, project-wide rule) |
| POST | `/api/es/endorsement/{item_id}/escalate` | Escalate an appetite-unknown case to the carrier's underwriting team (anyone may escalate) |

### `POST /run` request
```json
{ "scenario_ref": "scenario_03" }
```

### Response — APPETITE_UNKNOWN sample (real, Scenario 03 — **the single most important judgment call in this workflow**)
```json
{
  "payload": {
    "classification": "UNDERWRITING_REVIEW_REQUIRED",
    "appetite_recheck": {
      "applicable": true,
      "outcome": "APPETITE_UNKNOWN",
      "detail": "this class/use appears on NEITHER the carrier's accepted nor excluded list — an absent entry is not a confirmed exclusion (same principle as Market Matching's MM-04) — must be confirmed directly with the carrier, never auto-processed or auto-rejected."
    }
  }
}
```
An absent class is NOT the same as an excluded one — this must render as
an explicit open question, never a quiet "processed" or "rejected" status.

### Response — item-level DISCREPANCY sample (real, Scenario 05)
```json
{
  "payload": {
    "requested_items": ["Midstate Distribution Co.", "Harborline Logistics"],
    "carrier_response": {
      "issued_items": ["Midstate Distribution Co."],
      "reconciliation_status": "DISCREPANCY_FLAGGED",
      "discrepancy_detail": [{ "requested_item": "Harborline Logistics", "issued_item": null }]
    },
    "downstream_trigger_fired": false
  }
}
```
The carrier only processed HALF of a two-part request — this is an
item-level check, not a holistic "was something issued" flag; a partial
fulfillment must never read as fully reconciled. Verified live:
`POST /resolve-discrepancy` correctly flips `reconciliation_status` to
`BROKER_RESOLVED`, `downstream_trigger_fired` to `true`, and creates a new
`ENDORSEMENT_CONFIRMED` item in Agent Communication's list.

### Response — material-but-in-appetite sample (real, Scenario 04)
```json
{
  "payload": {
    "classification": "UNDERWRITING_REVIEW_REQUIRED",
    "appetite_recheck": {
      "outcome": "WITHIN_APPETITE",
      "state_licensing_clarification_needed": true
    },
    "premium_impact": {
      "premium_bearing": true,
      "proration_inputs": { "days_elapsed": 47, "days_remaining": 319, "term_total_days": 366 }
    }
  }
}
```
A $12.5M location addition — material enough to require review, but a
class the carrier already knows and wants, distinct from Scenario 03's
appetite-unknown case. `state_licensing_clarification_needed` is a
separate, additive flag (not folded into the three-outcome model) —
the new location's state wasn't specified, so it must be confirmed before
proceeding, never assumed same-state as the existing insured location.

---

## FE field → API field map

| FE screen element | API field |
|---|---|
| Classification badge | `classification` (`ROUTINE` \| `UNDERWRITING_REVIEW_REQUIRED`) |
| Appetite-unknown banner (visually distinct, FR-18) | `appetite_recheck.outcome === "APPETITE_UNKNOWN"` |
| State-licensing clarification flag | `appetite_recheck.state_licensing_clarification_needed` |
| Premium/proration display | `premium_impact.premium_bearing` + `.proration_inputs` (never present these as a confirmed dollar figure — inputs only) |
| Drafted request | `drafted_request.body` |
| Reconciliation discrepancy (visually distinct, FR-18) | `carrier_response.reconciliation_status === "DISCREPANCY_FLAGGED"` + `.discrepancy_detail[]` |
| One-click actions (FR-19) | `send` / `escalate` / `resolve-discrepancy` |

## Known v1 simplifications
- **"Request acknowledged" trigger not built** — re-scanned every FR; only
  FR-16 mandates something new (`ENDORSEMENT_CONFIRMED`). Section 2.2's
  mention is a scope-boundary description, not a build requirement.
- **`carrier_id` appetite data**: prefers `bound_policy_context.json`'s own
  embedded `carrier_accepted_classes`/`carrier_excluded_classes` when
  present; falls back to the real Workflow_10 `CarrierProfile` panel
  otherwise (not every scenario embeds them — verified, not assumed).
- **Class/appetite matching is keyword-overlap based**, not true NLU — a
  documented heuristic, same caveat class as every other native rule in
  this project.

## Verified against real data
Every field above is captured from an actual live run against the real
Workflow_15 dataset (Scenarios 03 and 05), including the full
discrepancy → resolve → downstream-trigger-release flow, exercised live
end to end (not just asserted in pytest).
