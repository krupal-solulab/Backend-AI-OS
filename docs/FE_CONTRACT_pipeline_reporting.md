# FE Contract — E&S Pipeline & Carrier Performance Reporting Copilot

`/api/es/pipeline-reporting` — the 10th and LAST workflow on the
**original E&S roadmap**. A pure aggregation/reporting layer over the six
prior workflows' logs — not a new decision/classification workflow. Its
entire credibility rests on two rules: PR-06 (never smoothing over a
data gap — this PRD's own risk register calls a single smoothed-over gap
"the direct throughline back to the very first critique in this entire
project," the original landing page's fabricated dashboard stats) and
PR-02 (never presenting a low-volume figure with false confidence). See
`docs/WORKFLOW_TEMPLATE.md`, `docs/PHASES_AND_STAGES.md` (Phase 4), and
this workflow's `RULE_ENGINE_INTERPRETATION_GUIDE.md` (in
`Workflow_19/test_dataset`) for the full PR-01..PR-06 rule spec.

Auth: Phase-0 header stub — every request needs `x-tenant-id`, `x-user-id`,
`x-role` (`junior|senior|admin`).

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/es/pipeline-reporting/run` | Generate one report (funnel, carrier hit-rate, or remarketing value — detected from the scenario's own data shape) |
| GET | `/api/es/pipeline-reporting` | List every report this tenant has generated |
| GET | `/api/es/pipeline-reporting/{item_id}` | One report's full detail |

**No `approve`/`escalate` action endpoints, unlike every prior E&S
workflow.** A report isn't a determination or a draft communication a
human approves or declines — there's nothing consequential being gated
here. Every `/run` call still enqueues a `ReviewItem` for audit/history
visibility (same uniform pattern every prior workflow uses).

No real-time or scheduled trigger — per FR-1's "scheduled or on-demand"
framing, this is the 6th instance in this vertical of deferring new
Arq/cron infrastructure. A principal/broker (or a real future scheduler)
calls `/run` per period/scenario_ref on demand.

### `POST /run` request
```json
{ "scenario_ref": "scenario_03" }
```

### Response — all 4 report shapes (real, live-verified samples against `TEST_DATA_ROOT/Workflow_19`)

**Clean funnel baseline** (Scenario 01 — Q3 2027, complete data):
```json
{
  "payload": {
    "data_completeness": { "status": "COMPLETE", "gaps": [] },
    "overall_conversion_pct": 56.0,
    "funnel": [
      { "stage": "Submissions Received", "count": 84, "pct_of_prior_stage": null },
      { "stage": "Matched to Carrier", "count": 79, "pct_of_prior_stage": 94.0 },
      { "stage": "Packages Assembled", "count": 79, "pct_of_prior_stage": 100.0 },
      { "stage": "Quotes Received", "count": 61, "pct_of_prior_stage": 77.2 },
      { "stage": "Compared & Selected", "count": 58, "pct_of_prior_stage": 95.1 },
      { "stage": "Bound", "count": 47, "pct_of_prior_stage": 81.0 }
    ]
  }
}
```

**Carrier hit-rate — low-volume annotation** (Scenario 02):
```json
{
  "payload": {
    "carrier_performance": [
      { "carrier_name": "Ironclad Casualty Solutions", "submissions_approached": 22, "quote_rate": 86.4, "bind_rate": 73.7, "overall_hit_rate": 63.6, "low_volume_flag": false },
      { "carrier_name": "Vantage Excess Partners", "submissions_approached": 4, "quote_rate": 100.0, "bind_rate": 100.0, "overall_hit_rate": 100.0, "low_volume_flag": true }
    ]
  }
}
```
**Order matters here**: carriers are sorted by `submissions_approached`
DESCENDING, never by hit-rate — Ironclad (the larger, more reliable base)
always renders above Vantage, even though Vantage's raw percentage is
higher. Sorting by rate alone would visually rank the low-volume figure
above the reliable one, which is exactly the false-precision problem
this scenario exists to catch.

**Funnel with a logging gap — release-gate scenario** (Scenario 03):
```json
{
  "payload": {
    "data_completeness": {
      "status": "PARTIAL",
      "gaps": [{ "stage": "Compared & Selected", "reason": "UNKNOWN - Quote Comparison workflow logging gap identified for 2 weeks in August (system migration)" }]
    },
    "overall_conversion_pct": null,
    "funnel": [
      { "stage": "Submissions Received", "count": 84, "pct_of_prior_stage": null },
      { "stage": "Matched to Carrier", "count": 79, "pct_of_prior_stage": 94.0 },
      { "stage": "Packages Assembled", "count": 79, "pct_of_prior_stage": 100.0 },
      { "stage": "Quotes Received", "count": 61, "pct_of_prior_stage": 77.2 },
      { "stage": "Compared & Selected", "count": null, "pct_of_prior_stage": null },
      { "stage": "Bound", "count": 47, "pct_of_prior_stage": null }
    ]
  }
}
```
**Verified live: the gapped stage has both `count` and `pct_of_prior_stage`
as `null` — never interpolated.** `Bound`'s raw count (47) still shows
(bind logging itself is unaffected), but its `pct_of_prior_stage` is ALSO
`null`, since the denominator it needs is the gapped stage —
never silently computed from adjacent stages. `overall_conversion_pct`
is withheld entirely whenever any gap exists anywhere in the funnel, even
though its own two endpoints (84, 47) are both individually known — a
clean top-line figure right next to a flagged gap would undercut the
gap's prominence.

**Remarketing value — confirmation vs. savings vs. not-remarketed** (Scenario 04):
```json
{
  "payload": {
    "remarketing_value": [
      { "account": "Summit Roofing Group", "trigger_level": "full_remarket", "outcome_type": "confirmation_value", "savings_amount": null, "note": "confirmed incumbent remained best option - no switch, no savings, but value was in the confirmation itself" },
      { "account": "Clearpath Bookkeeping (prior 2 cycles)", "trigger_level": "no_remarket", "outcome_type": "not_remarketed", "savings_amount": null, "note": "excluded from savings calc since not shopped, per its own suppression history" }
    ]
  }
}
```
Three genuinely distinct `outcome_type` values (the third,
`not_remarketed`, is additive beyond the literal PRD §7 schema, same
"add a real third state" discipline used throughout this vertical):
Summit Roofing's `$0`-savings, confirmed-incumbent outcome renders as
`confirmation_value` — never as a failure — and Clearpath's
never-actually-shopped account is clearly distinguished from both, rather
than silently dropped from the report.

---

## FE field → API field map

| FE screen element | API field |
|---|---|
| Data-completeness banner (PR-06 — must be prominent, never a footnote) | `data_completeness.status` / `.gaps[]` |
| Funnel stage bar/table | `funnel[]` (a `null` `count`/`pct_of_prior_stage` renders as an explicit "data gap" state, never a zero or omitted row) |
| Top-line conversion figure | `overall_conversion_pct` (`null` whenever any gap exists) |
| Carrier hit-rate table (ordered by volume, not rate) | `carrier_performance[]` |
| Low-volume caution badge | `carrier_performance[].low_volume_flag` |
| Remarketing outcome badge | `remarketing_value[].outcome_type` (`savings_identified` \| `confirmation_value` \| `not_remarketed`) |
| Savings figure (only when genuinely quantified) | `remarketing_value[].savings_amount` |

## Known v1 simplifications
- **No live cross-workflow DB aggregation.** Every scenario's
  `underlying_data.json` is itself a pre-aggregated period snapshot —
  nothing in this fixture-driven codebase has produced real "Q3 2027"
  activity to query. A live `ReviewItem`/`AuditEntry` aggregator across
  all six prior workflows is real, valuable future scope, not something
  this pass builds or can validate against these scenarios.
- **PR-04 (revenue attribution) is fully out of scope** — no field
  anywhere in this schema, per the PRD's own "do not build this rule
  from assumption."
- **`MIN_RELIABLE_VOLUME = 10`** is a placeholder (per this project's
  "every threshold is a placeholder" convention) — validate with a real
  design partner.
- **No new scheduled-job infrastructure** — the 6th instance of this
  vertical deferring real Arq/cron infra.
- **No new Agent Communication trigger, no cross-workflow re-invocation**
  — re-scanned all 6 FRs; none applies here.
- `src/core/reporting/service.py` (a Phase-1 shared module) exists but is
  a generic `AuditEntry` group-by-count rollup — it doesn't match this
  dataset's actual shape and isn't used here.

## Verified against real data
Every field above is captured from an actual live run against the real
Workflow_19 dataset (all 4 scenarios). `overall_conversion_pct` in
Scenario 01 is `56.0`, not the dataset's own illustrative prose figure of
"55.9%" — `47/84 = 55.9524%`, which correctly rounds to `56.0` at one
decimal place; the dataset's hand-written expected-output text is
illustrative prose, not a literal assertion target (same precedent
established for every prior workflow's `expected_output.txt`).

---

**This completes the original 10-item E&S workflow roadmap.**
