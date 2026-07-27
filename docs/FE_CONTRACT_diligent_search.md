# FE Contract — E&S Diligent Search & Compliance Documentation Copilot

`/api/es/diligent-search` — the eighth E&S workflow, and the **highest
legal-stakes workflow in this vertical** (PRD §8: a wrongly generated
affidavit is a potentially fraudulent record, not just a bad
recommendation). Given a submission's state(s) of operation, determines
per-state diligent-search requirement, checks export-list/exemption
eligibility, verifies declination evidence meets a strict written-evidence
bar, and generates a compliant document **only** when that bar is met —
never partially, never best-effort. See `docs/WORKFLOW_TEMPLATE.md`,
`docs/PHASES_AND_STAGES.md` (Phase 3), and this workflow's
`RULE_ENGINE_INTERPRETATION_GUIDE.md` (in `Workflow_17/test_dataset`) for
the full DS-01..DS-05 rule spec.

Auth: Phase-0 header stub — every request needs `x-tenant-id`, `x-user-id`,
`x-role` (`junior|senior|admin`).

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/es/diligent-search/run` | Run the per-state compliance determination for one scenario |
| GET | `/api/es/diligent-search` | List this tenant's compliance review items |
| GET | `/api/es/diligent-search/{item_id}` | One review's full detail |
| POST | `/api/es/diligent-search/{item_id}/approve` | Approve a resolved determination (standard `APPROVE` action) |
| POST | `/api/es/diligent-search/{item_id}/escalate` | Escalate an ambiguous `PENDING_DETERMINATION` state to compliance/legal (FR-7; standard `ESCALATE` action) |

No new workflow-owned action endpoint was needed — unlike several prior
workflows, the PRD's own end-to-end step 6 ("broker reviews the
checklist, gathers missing evidence, escalates ambiguous determinations")
is fully covered by the existing `APPROVE`/`ESCALATE` actions.

### `POST /run` request
```json
{ "scenario_ref": "scenario_03" }
```

### Response — the four outcomes (real, live-verified samples against `TEST_DATA_ROOT/Workflow_17`)

**READY** (Scenario 01 — GreenLeaf Cultivation, Oregon, 3/3 written declinations):
```json
{
  "payload": {
    "overall_status": "COMPLETE",
    "state_determinations": [{
      "state": "Oregon",
      "requirement_status": "REQUIRED",
      "sufficiency_status": "SUFFICIENT",
      "gap_detail": null,
      "document_generated": true,
      "generated_document_text": "Draft grounded in provided facts. Draft a diligent-search affidavit for this state, listing ONLY the admitted carriers, dates, and written-evidence confirmation given in the facts below...",
      "declinations_on_file": [
        { "carrier": "Admitted Carrier A", "date": "2027-07-10", "written_evidence": true },
        { "carrier": "Admitted Carrier B", "date": "2027-07-11", "written_evidence": true },
        { "carrier": "Admitted Carrier C", "date": "2027-07-12", "written_evidence": true }
      ]
    }]
  }
}
```

**EXEMPT** (Scenario 02 — Ridgeline Amusement Park, Texas, unconditional export-list language):
```json
{
  "payload": {
    "overall_status": "COMPLETE",
    "state_determinations": [{
      "state": "Texas",
      "requirement_status": "EXEMPT",
      "exemption_basis": "Amusement park liability is on Texas's export list - diligent search not required for this class",
      "sufficiency_status": "NOT_APPLICABLE",
      "document_generated": false,
      "generated_document_text": null
    }]
  }
}
```
The exemption is its own explicit, distinctly-logged determination —
never indistinguishable in the API shape from "no diligent search on
file."

**BLOCKED — release-gate scenario** (Scenario 03 — Pinecrest Demolition, Florida, verbal-only decline):
```json
{
  "payload": {
    "overall_status": "BLOCKED",
    "state_determinations": [{
      "state": "Florida",
      "requirement_status": "REQUIRED",
      "sufficiency_status": "INSUFFICIENT",
      "gap_detail": "need 1 more admitted decline(s) with written evidence; upgrade verbal-only decline(s) from Admitted Carrier B to written if possible",
      "document_generated": false,
      "generated_document_text": null
    }]
  }
}
```
**Verified live: `generated_document_text` is `null` and `document_generated`
is `false` — no document text is ever produced when evidence is
insufficient.** This is the single most important behavior in this
workflow (PRD §2.3/§8's zero-tolerance success criterion).

**PARTIAL — multi-state checklist** (Scenario 04 — Continental Freight, 8 states):
```json
{
  "payload": {
    "overall_status": "PARTIAL",
    "state_determinations": [
      { "state": "TN", "requirement_status": "REQUIRED", "sufficiency_status": "NOT_APPLICABLE",
        "gap_detail": "Requirement confirmed; declination evidence not yet submitted for assessment." },
      { "state": "GA", "requirement_status": "REQUIRED", "sufficiency_status": "NOT_APPLICABLE",
        "gap_detail": "Requirement confirmed; declination evidence not yet submitted for assessment." },
      { "state": "FL", "requirement_status": "PENDING_DETERMINATION",
        "gap_detail": "Export-list eligibility is account-specific and unconfirmed: 'trucking excess casualty may be export-eligible in FL for large commercial accounts'. Flagged for human/legal review per FR-7 — not auto-resolved to exempt." },
      { "state": "NC", "requirement_status": "PENDING_DETERMINATION",
        "gap_detail": "State requirement and export-list status not yet checked for this state." },
      { "state": "SC", "requirement_status": "PENDING_DETERMINATION" },
      { "state": "VA", "requirement_status": "PENDING_DETERMINATION" },
      { "state": "AL", "requirement_status": "PENDING_DETERMINATION" },
      { "state": "MS", "requirement_status": "PENDING_DETERMINATION" }
    ]
  }
}
```
All 8 states render individually — never a single collapsed verdict.
Florida is the core FR-7 judgment call: `export_list_class: true` in the
input data alone did **not** auto-resolve it to `EXEMPT`, because its
`export_list_note` is hedged and account-size-dependent ("may be...for
large commercial accounts") rather than unconditional like Texas's.

---

## FE field → API field map

| FE screen element | API field |
|---|---|
| Per-state checklist (FR-6 — never a single collapsed status) | `state_determinations[]` |
| Requirement badge | `state_determinations[].requirement_status` (`REQUIRED` \| `EXEMPT` \| `PENDING_DETERMINATION`) |
| Exemption basis (FR-2 — must be explicit, never blank) | `state_determinations[].exemption_basis` |
| Evidence sufficiency badge | `state_determinations[].sufficiency_status` (`SUFFICIENT` \| `INSUFFICIENT` \| `NOT_APPLICABLE`) |
| Exact gap to close (FR-3) | `state_determinations[].gap_detail` |
| Document-ready flag (FR-4 gate) | `state_determinations[].document_generated` |
| Generated affidavit text | `state_determinations[].generated_document_text` |
| Overall record status | `overall_status` (`COMPLETE` \| `PARTIAL` \| `BLOCKED`) |
| Ambiguous-determination escalation (FR-7) | `escalate` action |

## Known v1 simplifications
- **`retention_period_years` is always `null`.** No scenario's input data
  supplies real state-specific retention reference data, and FR-8 calls
  this "a required discovery input, not something to derive from general
  reasoning" — v1 honestly reports "not yet sourced" rather than
  inventing plausible-sounding year counts.
- **`generated_document_text` is an additive field beyond PRD §7's literal
  schema** (which only has `document_generated: boolean`) — added so the
  FE has actual grounded document content to show, not just a flag.
- **No new Agent Communication trigger, no scheduled job, no
  cross-workflow re-invocation** — re-scanned all 8 FRs; none mandates
  any of these (unlike Binder & Issuance/Endorsement's new triggers or
  Renewal Remarketing's re-invocation).
- **No new workflow-owned router action** — `APPROVE`/`ESCALATE` already
  cover every broker action this PRD describes.

## Verified against real data
Every field above is captured from an actual live run against the real
Workflow_17 dataset (all 4 scenarios), booted via `uvicorn` and exercised
with `curl` — including confirming, live, that Scenario 03 (the release
gate) produces zero document text.
