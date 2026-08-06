# FE Contract — E&S Carrier Appetite Intelligence Tracking Copilot

`/api/es/carrier-appetite-intelligence` — the ninth and LAST E&S workflow
in Phase 3 (only Phase 4's Pipeline & Carrier Reporting remains after
this). This PRD's own Section 0 calls it the highest scope-creep risk in
the vertical at every prior mention — the v1 here is deliberately narrow:
aggregate signals already logged by Quote Comparison/Renewal Remarketing,
distinguish genuine class-level appetite shifts from normal
account-specific variance, and auto-update exactly two metadata fields.
**This workflow should be quiet almost all the time** — 3 of its 4 test
scenarios produce no suggestion at all. See `docs/WORKFLOW_TEMPLATE.md`,
`docs/PHASES_AND_STAGES.md` (Phase 3), and this workflow's
`RULE_ENGINE_INTERPRETATION_GUIDE.md` (in `Workflow_18/test_dataset`) for
the full CI-01..CI-05 rule spec.

Auth: Phase-0 header stub — every request needs `x-tenant-id`, `x-user-id`,
`x-role` (`junior|senior|admin`).

---

## A note on scope before anything else

**No mutable Carrier Appetite Profile store or profile-editing interface
exists anywhere in this codebase.** Market Matching's `CarrierProfile` is
a frozen dataclass loaded fresh from read-only `carrier_profiles/*.json`
fixtures — there's no DB table and no write path. CI-03's "automatic
metadata refresh" and FR-6's "reuses the existing... management
interface" both assume infrastructure this codebase doesn't have yet.
Per the approved plan, this workflow **computes and records** the
would-be refresh in its own `OutputPackage.payload` — it never mutates
Market Matching's data, and never will, since building that write
capability now would be exactly the kind of scope expansion Section 0
warns against. This is a stated limitation, not a hidden gap.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/es/carrier-appetite-intelligence/run` | Evaluate one carrier/class combination's already-logged signals |
| GET | `/api/es/carrier-appetite-intelligence` | List every evaluation this tenant has run (including suppressed ones — this is how §2.3's suppression-rate health metric stays observable) |
| GET | `/api/es/carrier-appetite-intelligence/{item_id}` | One evaluation's full detail |
| POST | `/api/es/carrier-appetite-intelligence/{item_id}/approve` | Approve a `GENUINE_INCONSISTENCY` suggestion (standard `APPROVE` action — records approval only, does not itself change any profile data) |
| POST | `/api/es/carrier-appetite-intelligence/{item_id}/dismiss` | Dismiss a suggestion (workflow-owned — `ReviewAction` has no `DISMISS` value, same pattern as Agent Communication's `discard`) |

No real-time or scheduled trigger — per FR-1's periodic-batch framing,
this is the 5th instance in this vertical of deferring new Arq/cron
infrastructure. A broker/admin (or a real future scheduler) calls `/run`
per carrier/class combination on demand.

### `POST /run` request
```json
{ "scenario_ref": "scenario_02" }
```

### Response — the three outcomes (real, live-verified samples against `TEST_DATA_ROOT/Workflow_18`)

**SUPPRESSED — low volume** (Scenario 01 — Palmetto/Roofing, 1 data point):
```json
{
  "payload": {
    "pattern_type": "INSUFFICIENT_SIGNAL",
    "status": "SUPPRESSED",
    "suggested_action": null,
    "metadata_refresh": null,
    "evidence": [{ "submission_id": "SUB-A", "outcome": "declined", "reason_scope": null }]
  }
}
```
A single data point never produces a suggestion, regardless of
consistency direction.

**SUGGESTION GENERATED** (Scenario 02 — Meridian/Landscaping, genuine class-level pattern):
```json
{
  "payload": {
    "pattern_type": "GENUINE_INCONSISTENCY",
    "status": "PENDING_REVIEW",
    "suggested_action": "...Recommend reviewing whether landscaping should be removed from Meridian's accepted list or confidence downgraded...",
    "metadata_refresh": null,
    "evidence": [
      { "submission_id": "SUB-B4", "outcome": "quoted", "reason_scope": null },
      { "submission_id": "SUB-B1", "outcome": "declined", "stated_reason": "class no longer written", "reason_scope": "class_level" },
      { "submission_id": "SUB-B2", "outcome": "declined", "stated_reason": "class no longer written", "reason_scope": "class_level" },
      { "submission_id": "SUB-B3", "outcome": "declined", "stated_reason": "no reason given", "reason_scope": "unstated" }
    ]
  }
}
```
Note `SUB-B3`'s `reason_scope: "unstated"` — it does NOT count as
class-level evidence, but the other two do, and that's sufficient. This
is a **suggestion only**; approving it never edits any accepted/excluded
class list.

**SUPPRESSED — release-gate scenario** (Scenario 03 — Ironclad/Roofing, account-specific decline):
```json
{
  "payload": {
    "pattern_type": "INSUFFICIENT_SIGNAL",
    "status": "SUPPRESSED",
    "suggested_action": null,
    "evidence": [
      { "submission_id": "SUB-C1", "outcome": "quoted", "reason_scope": null },
      { "submission_id": "SUB-C2", "outcome": "declined", "stated_reason": "severity exceeded ceiling for this specific account", "reason_scope": "account_specific" },
      { "submission_id": "SUB-C3", "outcome": "quoted", "reason_scope": null }
    ]
  }
}
```
**Verified live: the one inconsistent outcome's `reason_scope` is
`"account_specific"`, contributing zero toward class-level evidence — it
is never scored like Scenario 02's genuine pattern**, even though both
scenarios have an inconsistent outcome. This is the single most important
behavior in this workflow.

**METADATA REFRESH ONLY** (Scenario 04 — Coastal Mutual/Habitational, 4/4 consistent):
```json
{
  "payload": {
    "pattern_type": "CONFIRMED_CONSISTENT",
    "status": "METADATA_AUTO_UPDATED",
    "suggested_action": null,
    "metadata_refresh": { "appetite_confidence": "high", "appetite_last_updated": "2026-07-27" }
  }
}
```
`metadata_refresh` contains **exactly these two keys, always** —
`class_codes_accepted`/`excluded`/`premium_band`/`severity_ceiling` are
never present here and no code path in this workflow can write them
(FR-4, architecturally enforced by having no such write path at all).
`appetite_confidence` was pulled from Coastal Mutual's REAL Workflow_10
profile (already `"high"`) and reaffirmed; `appetite_last_updated` was
refreshed to the evaluation date.

---

## FE field → API field map

| FE screen element | API field |
|---|---|
| Pattern-type badge | `pattern_type` (`CONFIRMED_CONSISTENT` \| `GENUINE_INCONSISTENCY` \| `INSUFFICIENT_SIGNAL`) |
| Review status | `status` (`SUPPRESSED` \| `METADATA_AUTO_UPDATED` \| `PENDING_REVIEW` \| `APPROVED` \| `DISMISSED`) |
| Evidence grounding (FR-5) | `evidence[]` — every claim traces to a listed `submission_id`/`outcome`/`date` |
| Class-level vs. account-specific distinction (CI-02, the core judgment call) | `evidence[].reason_scope` |
| Suggested profile change (human-reviewed only) | `suggested_action` |
| Metadata-only auto-refresh (CI-03's one write path) | `metadata_refresh.appetite_confidence` / `.appetite_last_updated` |
| Suppression-rate health metric (§2.3) | computed client-side / in reporting by grouping `GET` list results by `pattern_type` |
| One-click actions | `approve` (`GENUINE_INCONSISTENCY` only) / `dismiss` |

## Known v1 simplifications
- **No real Carrier Appetite Profile write-back** — `metadata_refresh` and
  `suggested_action` are computed and recorded, never applied to any
  actual profile store, since none exists yet in this codebase (see "A
  note on scope" above).
- **FR-6's "reuses the existing... management interface"** is satisfied
  by the standard `ReviewQueueService`/`AuditService` mechanism (same as
  every other workflow) — the closest existing analog, since Market
  Matching never built a dedicated profile-editing interface either.
- **No new scheduled-job infrastructure** — the 5th instance of this
  vertical deferring real Arq/cron infra; `/run` is on-demand per
  carrier/class scenario_ref.
- **Thresholds are placeholders** (`min_total_outcomes=3`,
  `min_class_level_inconsistent=2`, a 3-outcome recency window) — verified
  by hand to reproduce all 4 real dataset scenarios exactly, but not
  derived from real operational data (per this project's "every threshold
  is a placeholder" convention).
- **No new Agent Communication trigger, no cross-workflow re-invocation**
  — re-scanned all 6 FRs; none applies here.

## Verified against real data
Every field above is captured from an actual live run against the real
Workflow_18 dataset (all 4 scenarios), including confirming that
Scenario 04's `metadata_refresh.appetite_confidence` genuinely pulls from
Coastal Mutual's real Workflow_10 `carrier_profiles/carrier_04_coastal_mutual.json`
fixture (`"high"`), not an invented value.
