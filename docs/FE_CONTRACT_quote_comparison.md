# FE Contract — E&S Quote Comparison & Recommendation Copilot

`/api/es/quote-comparison` — the fourth E&S workflow. Ingests carrier
response emails (quotes and declinations) for a submission already shopped
via Market Matching/Package Assembly, normalizes terms for genuine
comparability, classifies subjectivities by materiality, tracks quote
validity windows, and produces either a single recommendation or an explicit
multi-option trade-off — feeding directly into Agent Communication's
Quote/Terms Summary once the broker selects a quote. See
`docs/WORKFLOW_TEMPLATE.md`, `docs/PHASES_AND_STAGES.md` (Phase 3), and this
workflow's `RULE_ENGINE_INTERPRETATION_GUIDE.md` (in `Workflow_13/test_dataset`)
for the full QC-01..QC-07 rule spec.

Auth: Phase-0 header stub — every request needs `x-tenant-id`, `x-user-id`,
`x-role` (`junior|senior|admin`).

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/es/quote-comparison/run` | Run comparison/recommendation for one scenario/submission's carrier responses |
| GET | `/api/es/quote-comparison` | List this tenant's comparison review items |
| GET | `/api/es/quote-comparison/{item_id}` | One comparison's full detail — **urgency is recomputed against today's date on every call** (see below) |
| POST | `/api/es/quote-comparison/{item_id}/select/{quote_id}` | Broker marks which quote to present to the retail agent — **this is what fires the Agent Communication handoff**, not `/run` |
| POST | `/api/es/quote-comparison/{item_id}/request-revised-terms` | Logs a "requesting revised terms" broker decision — no status change beyond `payload.status` |
| POST | `/api/es/quote-comparison/{item_id}/mark-lapsed` | Logs a "no action, quote will lapse" broker decision |

### `POST /run` request
```json
{ "scenario_ref": "scenario_02", "as_of": "2027-07-28" }
```
`as_of` is optional (ISO date) — a fixture/test-determinism override for
QC-07's "current date" reference; production omits it and the server uses
the real current date.

### `POST /run` response — MULTI_OPTION sample (real, Scenario 02)
```json
{
  "id": "647b5e8d-...",
  "submission_id": "scenario_02",
  "status": "pending",
  "payload": {
    "named_insured": "Oakwood Apartment Homes",
    "quotes": [
      {
        "quote_id": "bd9afaf4-...", "carrier_name": "Harbor Specialty Property",
        "response_type": "QUOTE", "premium": 74000.0,
        "limits": "Property: $18,000,000 blanket",
        "deductibles": { "all_perils": "$25,000", "wind_hail": "$100,000" },
        "subjectivities": [{ "description": "current SOV confirmed at binding", "materiality": "routine" }],
        "quote_valid_through": "08/12/2027"
      },
      {
        "quote_id": "b84a0c66-...", "carrier_name": "Coastal Mutual Specialty",
        "premium": 81500.0, "deductibles": { "all_perils": "$10,000", "wind_hail": "$50,000" }
      }
    ],
    "comparability_assessment": {
      "directly_comparable": false,
      "material_differences": ["deductible (all perils)", "deductible (wind/hail)"]
    },
    "output_mode": "MULTI_OPTION",
    "recommendation": { "primary_quote_id": null, "reasoning": { "summary": "Quotes are not directly comparable on premium alone — material differences in deductible (all perils), deductible (wind/hail). Presenting as an explicit trade-off rather than a single winner." } },
    "urgency_flags": [],
    "selected_quote_id": null,
    "status": "PENDING_REVIEW"
  }
}
```
Note: Harbor is ~9% cheaper but carries materially higher deductibles — the
FE must never render this as "cheaper option" alone; `material_differences`
and both quotes' full terms must be shown side by side (FR-8/QC-01).

### `POST /run` response — SINGLE_QUOTE_URGENT sample (real, Scenario 06)
```json
{
  "payload": {
    "output_mode": "SINGLE_QUOTE_URGENT",
    "urgency_flags": [
      { "quote_id": "...", "flag_type": "validity_window", "detail": "3 day(s) remaining until quote_valid_through (08/02/2027)" },
      { "quote_id": "...", "flag_type": "dependency_unresolved", "detail": "primary carrier binding confirmed — resolution status not trackable in v1 (no upstream bind-status signal exists yet)" }
    ]
  }
}
```
Per FR-22: a `SINGLE_QUOTE_URGENT` item must be visually/positionally
distinct from routine comparison output — a broker should never have to
open a "comparison" to discover a single expiring quote.

### `GET /{item_id}` — read-time urgency recompute
Unlike every other field, `output_mode` (when `SINGLE_QUOTE_ROUTINE`/
`SINGLE_QUOTE_URGENT`) and `urgency_flags` are **recomputed against the
real current date on every GET**, not just frozen from ingestion time — no
scheduled background job exists yet (deferred per the approved plan, same
call made for Agent Communication's `NO_RESPONSE_FOLLOWUP`); the FE gets
correct urgency simply by re-fetching. A quote that showed
`SINGLE_QUOTE_ROUTINE` yesterday can show `SINGLE_QUOTE_URGENT` today with
no `/run` re-triggered.

### `POST /{item_id}/select/{quote_id}`
```json
{ "id": "...", "status": "approved", "payload": { "selected_quote_id": "bd9afaf4-...", "status": "PRESENTED", "...": "..." } }
```
This is the **one and only** trigger for the Agent Communication handoff
(FR-20) — verified live: selecting a quote here creates a new pending item
at `GET /api/es/agent-communication` with `trigger_type: "QUOTE_TERMS_SUMMARY"`.
`/run` itself never fires it, even for `SINGLE_RECOMMENDATION`/
`SINGLE_QUOTE_*` modes — per FR-23's own ordering ("broker marks which
quote(s) to present... which feeds FR-20's downstream trigger"), the
broker's action is the trigger, not the system's recommendation by itself.

---

## FE field → API field map

| FE screen element | API field |
|---|---|
| Side-by-side comparison table (FR-21) | `payload.quotes[]` — every field, not just premium |
| "Not directly comparable" banner | `payload.comparability_assessment.directly_comparable` (false) + `.material_differences` |
| Mode badge | `payload.output_mode` |
| Subjectivity tiering | `payload.quotes[].subjectivities[]` — split by `materiality` |
| Urgent single-quote alert (must be visually distinct, FR-22) | `payload.output_mode === "SINGLE_QUOTE_URGENT"` + `payload.urgency_flags[]` |
| Declination display | `quotes[]` where `response_type === "DECLINATION"` — `declination_reason` + `declination_appetite_consistency` (log-only, QC-03; not actioned) |
| "Present this quote" action | `select/{quote_id}` |
| "Request revised terms" / "Let it lapse" | `request-revised-terms` / `mark-lapsed` |

## Known v1 simplifications
- **`carrier_id` is always `null`** — carrier identity is resolved by name
  only (email domain → display name); no cross-reference to Market
  Matching's `CAR-0N` ids was built in v1 since nothing downstream needs it.
- **Multi-line endorsement description text may be truncated** (e.g. "...
  scheduled basis only" without a wrapped `"(per-project, not blanket)"`
  continuation) — the classification (`basis`) is still correct since the
  distinguishing keyword is always on the first line; only the raw display
  text is shortened.
- **QC-03 is log-only** — `declination_appetite_consistency` is captured but
  nothing acts on it (per the PRD's own explicit v1 scope boundary; feeds a
  future Carrier Appetite Intelligence workflow).

## Verified against real data
Every field above is captured from an actual live run against the real
Workflow_13 dataset (Scenarios 02 and 06), including the full
select → Agent Communication handoff, exercised live end to end (not just
asserted in pytest).
