# FE Contract — E&S Binder & Policy Issuance Coordination Copilot

`/api/es/binder-issuance` — the fifth E&S workflow, closing the full
placement lifecycle (submission → match → package → quote → compare →
**bind and issue**). Coordinates the bind request, tracks pre-bind
subjectivity clearance, reconciles the carrier's bind confirmation and
issued policy document against what was actually agreed (never trusting
either just because it's the carrier's official output), monitors policy
issuance timelines, and tracks post-bind ongoing obligations. See
`docs/WORKFLOW_TEMPLATE.md`, `docs/PHASES_AND_STAGES.md` (Phase 3), and this
workflow's `RULE_ENGINE_INTERPRETATION_GUIDE.md` (in `Workflow_14/test_dataset`)
for the full BI-01..BI-07 rule spec.

Auth: Phase-0 header stub — every request needs `x-tenant-id`, `x-user-id`,
`x-role` (`junior|senior|admin`).

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/es/binder-issuance/run` | Run coordination for one scenario/submission |
| GET | `/api/es/binder-issuance` | List this tenant's coordination review items |
| GET | `/api/es/binder-issuance/{item_id}` | One record's full detail — **issuance overdue status and obligation reminders are recomputed against today's date on every call** |
| POST | `/api/es/binder-issuance/{item_id}/resolve-confirmation-discrepancy` | Broker resolves a BI-03 bind-confirmation discrepancy — required before Placement Confirmation can fire |
| POST | `/api/es/binder-issuance/{item_id}/resolve-policy-discrepancy` | Broker resolves a BI-05 issued-policy discrepancy — required before Policy Documents Delivered can fire |
| POST | `/api/es/binder-issuance/{item_id}/escalate` | Escalate to principal (reuses the standard `ESCALATE` action) |

### `POST /run` request
```json
{ "scenario_ref": "scenario_03", "as_of": "2027-08-26" }
```
`as_of` is optional (ISO date) — a fixture/test-determinism override for
BI-04's "current date" reference; production omits it.

### Response — BLOCKED sample (real, Scenario 02)
```json
{
  "payload": {
    "bind_order_status": "BLOCKED",
    "pre_bind_subjectivities": [
      { "description": "satisfactory loss control inspection prior to binding, must be scheduled within 10 days", "materiality": "material", "lifecycle_stage": "PRE_BIND", "status": "open" },
      { "description": "current SOV confirmed", "materiality": "routine", "lifecycle_stage": "PRE_BIND", "status": "cleared" }
    ],
    "downstream_triggers_fired": { "placement_confirmation": false, "policy_documents_delivered": false }
  }
}
```
A `BLOCKED` bind never reaches confirmation reconciliation at all — the FE
must show WHICH specific subjectivity is blocking (FR-5), not a generic
"not ready" status.

### Response — DISCREPANCY_FLAGGED sample (real, Scenario 03 — **the single most important test case in this workflow**)
```json
{
  "payload": {
    "bind_order_status": "SENT",
    "carrier_confirmation": {
      "binder_number": "ICS-2027-51203",
      "reconciliation_status": "DISCREPANCY_FLAGGED",
      "discrepancy_detail": [
        { "field": "deductible (all perils)", "requested_or_bound": "$2,500", "confirmed_or_issued": "$5,000" },
        { "field": "effective_date", "requested_or_bound": "2027-09-01", "confirmed_or_issued": "2027-09-03" }
      ]
    },
    "downstream_triggers_fired": { "placement_confirmation": false, "policy_documents_delivered": false }
  }
}
```
A binder number existing does **not** mean clean — this must render as a
visually/positionally distinct discrepancy banner (FR-22), never folded
into routine status. `POST /resolve-confirmation-discrepancy` with
`{"resolution": "accept_carrier_version"}` or `{"resolution": "flag_carrier_error"}`
resolves it — verified live: this correctly flips
`reconciliation_status` to `BROKER_RESOLVED`, sets
`downstream_triggers_fired.placement_confirmation` to `true`, and creates a
new `PLACEMENT_CONFIRMATION` item in Agent Communication's list.

### Response — issued-policy MATERIAL discrepancy (real, Scenario 06 — **highest-value check in the entire workflow**)
```json
{
  "payload": {
    "issued_policy_reconciliation": {
      "status": "POLICY_DISCREPANCY_FLAGGED",
      "discrepancy_detail": [
        { "field": "deductible (wind/hail)", "requested_or_bound": "$50,000", "confirmed_or_issued": "$100,000" }
      ]
    },
    "downstream_triggers_fired": { "policy_documents_delivered": false }
  }
}
```
Every other field (premium, property limit, GL limits, all-perils
deductible, effective date) matched exactly — only wind/hail doubled. This
is field-by-field, never a holistic "looks about right" comparison (FR-15).
`POST /resolve-policy-discrepancy` releases `POLICY_DOCUMENTS_DELIVERED` the
same way the confirmation endpoint releases `PLACEMENT_CONFIRMATION`.

### Response — overdue issuance alert (real, Scenario 05)
```json
{
  "payload": {
    "policy_issuance": {
      "carrier_stated_timeline_days": 30,
      "expected_by_date": "2027-09-04",
      "documents_received": false,
      "overdue_alert_fired": true
    }
  }
}
```
Recomputed live on every `GET` against the real current date — a bind that
looked fine on day one correctly flips to overdue as time passes, with no
`/run` re-triggered.

### Response — post-bind ongoing obligation (real, Scenario 04)
```json
{
  "payload": {
    "post_bind_ongoing_obligations": [
      { "description": "updated loss control report within 60 days of binding", "due_date": "2027-09-29", "status": "open", "reminder_due": false }
    ],
    "downstream_triggers_fired": { "placement_confirmation": true }
  }
}
```
The SAME kind of requirement as Scenario 02's blocker (a loss control item)
does **not** block here, because it's classified `POST_BIND_ONGOING`
instead of `PRE_BIND` — inherited as-is from Quote Comparison's own
classification, never re-judged. `reminder_due` flips `true` at 15 and 5
days before `due_date`, recomputed live the same way `overdue_alert_fired`
is.

---

## FE field → API field map

| FE screen element | API field |
|---|---|
| Unified bind status view (FR-21) | the whole `BindCoordinationPayload` — pre-bind checklist, confirmation status, issuance monitoring, ongoing obligations all in one view, not fragmented |
| Blocking subjectivity (specific, not generic) | `pre_bind_subjectivities[]` where `status === "open"` and `materiality === "material"` |
| Discrepancy banner (visually distinct, FR-22) | `carrier_confirmation.reconciliation_status === "DISCREPANCY_FLAGGED"` / `issued_policy_reconciliation.status === "POLICY_DISCREPANCY_FLAGGED"` |
| Side-by-side discrepancy values | `discrepancy_detail[]` (`requested_or_bound` vs `confirmed_or_issued`) |
| Overdue issuance alert | `policy_issuance.overdue_alert_fired` |
| Ongoing obligation tracker + reminders | `post_bind_ongoing_obligations[]` |
| Resolution actions (FR-23) | `resolve-confirmation-discrepancy` / `resolve-policy-discrepancy` (body: `resolution`) / `escalate` |

## Known v1 simplifications
- **Scheduled monitoring deferred** — BI-04/BI-07 recompute at READ TIME
  (every `GET`), not via a real background scheduler, same call made for
  Quote Comparison's QC-07 (this is now the third instance; FR-26 flags
  shared infra as overdue — not built this pass, flagged honestly).
- **`bind_record`-stage scenarios (05/06) are treated as already-confirmed-clean**
  — since no BI-03 discrepancy data is modeled at that later stage in this
  dataset, `reconciliation_status` defaults to `CLEAN` and
  `placement_confirmation` may fire even for a "later observation" run.
  This is a real v1 modeling simplification, not a bug — flagged here so
  it isn't a surprise.
- **`carrier_id` may be `null`** on parsed (email/declarations-page)
  terms — same v1 simplification as Quote Comparison's own contract.

## Verified against real data
Every field above is captured from an actual live run against the real
Workflow_14 dataset (Scenarios 02, 03, 05, 06) — not a hypothetical shape.
The full discrepancy → resolve → downstream-trigger-release flow was
exercised live end to end for BOTH the confirmation and issued-policy
stages, not just asserted in pytest.
