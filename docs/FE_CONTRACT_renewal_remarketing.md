# FE Contract — E&S Renewal Remarketing Copilot

`/api/es/renewal-remarketing` — the seventh E&S workflow, and the last one
gated on real bound-policy data. Given a bound policy approaching renewal,
detects exposure and loss-history changes, checks incumbent responsiveness
and appetite, and produces a **graduated, four-state** remarket
recommendation — never a binary yes/no. An ORCHESTRATION workflow: it
genuinely re-invokes the existing Market Matching engine (never a separate
ranking implementation) and reuses Quote Comparison's term-normalization
discipline for post-remarket comparisons. See
`docs/WORKFLOW_TEMPLATE.md`, `docs/PHASES_AND_STAGES.md` (Phase 3), and this
workflow's `RULE_ENGINE_INTERPRETATION_GUIDE.md` (in `Workflow_16/test_dataset`)
for the full RR-01..RR-08 rule spec.

Auth: Phase-0 header stub — every request needs `x-tenant-id`, `x-user-id`,
`x-role` (`junior|senior|admin`).

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/es/renewal-remarketing/run` | Run the remarket-trigger decision (or the post-remarket comparison, for Scenario 05's shape) for one scenario |
| GET | `/api/es/renewal-remarketing` | List this tenant's renewal review items |
| GET | `/api/es/renewal-remarketing/{item_id}` | One review's full detail |
| POST | `/api/es/renewal-remarketing/{item_id}/initiate-remarket` | **Approve light check / Approve full remarket** — genuinely re-invokes `MarketMatchingPipeline` (409 if the decision was `NO_REMARKET`) |
| POST | `/api/es/renewal-remarketing/{item_id}/accept-incumbent` | Accept the incumbent's renewal terms (for `NO_REMARKET` decisions) |
| POST | `/api/es/renewal-remarketing/{item_id}/escalate` | Escalate an urgent remarket (reuses the standard `ESCALATE` action) |

### `POST /run` request
```json
{ "scenario_ref": "scenario_02" }
```

### Response — the four trigger levels (real, live-verified samples)

**FULL_REMARKET** (Scenario 02 — disproportionate pricing driven by continued severity):
```json
{
  "payload": {
    "named_insured": "Summit Roofing Group LLC",
    "loss_history_change": { "new_claims_count": 1, "trend": "worsening" },
    "trigger_decision": {
      "level": "FULL_REMARKET",
      "reasoning": { "summary": "Premium change of 58.5% is disproportionate to 3.8% exposure growth, driven by continued adverse loss activity (1 new claim(s)) — worth confirming no better alternative exists, even if the pricing itself is likely justified." }
    }
  }
}
```

**LIGHT_REMARKET_CHECK** (Scenario 03 — favorable change + size-band shift, distinct from a full shop):
```json
{
  "payload": {
    "named_insured": "Oakwood Apartment Homes LP",
    "exposure_change": { "material": true, "already_endorsed": true, "pct_change": 32.5 },
    "loss_history_change": { "trend": "improving" },
    "trigger_decision": { "level": "LIGHT_REMARKET_CHECK", "reasoning": { "summary": "Exposure growth is known/already explained and loss trend is favorable, but the account's larger size band makes a lightweight comparison check worth doing — distinct in effort from a full remarket campaign." } }
  }
}
```

**URGENT_REMARKET** (Scenario 04 — incumbent silence, a lapse-risk signal independent of pricing/exposure):
```json
{
  "payload": {
    "named_insured": "Delta Electric Services LLC",
    "incumbent_status": { "renewal_terms_received": false, "non_response_flag": true },
    "exposure_change": { "material": false },
    "trigger_decision": { "level": "URGENT_REMARKET", "reasoning": { "summary": "Incumbent has not provided renewal terms despite broker follow-up, with limited time remaining before expiration — a lapse-risk signal, independent of pricing or exposure..." } }
  }
}
```

**NO_REMARKET, history-grounded** (Scenario 06 — must cite the account's OWN history, not a size heuristic):
```json
{
  "payload": {
    "remarketing_history_detail": "This account has been remarketed 2 of the last 2 renewal cycles by broker preference, with no carrier change resulting either time - minimal savings found ($50-100) both times",
    "trigger_decision": { "level": "NO_REMARKET", "reasoning": { "summary": "This account's own remarketing history shows no demonstrated value: ..." } }
  }
}
```

### `POST /{item_id}/initiate-remarket` (real, live-verified)
```json
{ "payload": { "remarket_execution": { "initiated": true, "market_matching_output_id": "4e2c4907-..." } } }
```
This genuinely re-invokes `MarketMatchingPipeline.run()` and creates a real
item visible at `GET /api/es/market-matching` — verified live (item count
went from 8 → 9). **Known v1 limitation, stated plainly**: it re-runs
against the *original* Workflow_10 bind-time submission for the same named
insured — this dataset has no fresh renewal-time ACORD/loss-run documents,
so the re-invocation reflects original bind-time exposure data, not the
updated figures in `renewal_context.json`. `409` if called on a
`NO_REMARKET` decision.

### Post-remarket comparison sample (Scenario 05 — exception-quote flagging)
```json
{
  "payload": {
    "is_comparison_stage": true,
    "remarket_execution": {
      "comparison_output": {
        "directly_comparable": false,
        "material_differences": ["deductible"],
        "incumbent": { "carrier_name": "Ironclad Casualty Solutions", "premium": 187000, "deductible": 5000 },
        "alternative": { "carrier_name": "Palmetto Specialty Underwriters", "premium": 171000, "deductible": 15000, "is_exception_based": true, "exception_detail": "Palmetto's severity ceiling is normally $150,000 and would typically exclude this account... this quote required a manual underwriting exception..." }
      }
    }
  }
}
```
Never default to recommending the lower premium — the FE must show the
deductible difference and the exception-based flag with equal prominence.

---

## FE field → API field map

| FE screen element | API field |
|---|---|
| Trigger level badge (never collapsed to binary, FR-7) | `trigger_decision.level` |
| URGENT visual distinction (FR-8) | `trigger_decision.level === "URGENT_REMARKET"` |
| Reasoning citation (FR-9 — never show a level without why) | `trigger_decision.reasoning.summary` |
| Exposure/loss change detail | `exposure_change` / `loss_history_change` |
| Incumbent non-response flag | `incumbent_status.non_response_flag` |
| Remarketing-history grounding | `remarketing_history_detail` |
| Comparison view (post-remarket) | `remarket_execution.comparison_output` |
| Exception-quote flag | `comparison_output.alternative.is_exception_based` + `.exception_detail` |
| One-click actions (FR-16) | `initiate-remarket` (light/full) / `accept-incumbent` (no-remarket) / `escalate` (urgent) |

## Known v1 simplifications
- **RR-05's re-invocation reflects original bind-time data** — no fresh
  renewal-time documents exist in this dataset (see above).
- **No new scheduled-job infrastructure** — FR-19 flags this as the fourth
  workflow needing ongoing monitoring; this pass continues the deferred
  pattern rather than building shared Arq infra (same call made 3 times
  already in this vertical).
- **`remarketing_history` is read as free text**, not the PRD schema's
  structured list — the actual fixture data is prose, and this workflow
  doesn't build a new accumulating history store; real multi-year
  persistence is a natural production evolution, not testable within a
  fixture-driven pass.
- **No new Agent Communication trigger** — re-scanned every FR; none
  mandates one (unlike Binder & Issuance/Endorsement).

## Verified against real data
Every field above is captured from an actual live run against the real
Workflow_16 dataset (Scenarios 02, 03, 04, 05), including a genuine
`MarketMatchingPipeline` re-invocation confirmed by checking the resulting
item actually exists in Market Matching's own list — not just asserted in
pytest.
