# FE Contract — E&S Package Assembly

`/api/es/package-assembly` — the second E&S workflow. Consumes a Market
Matching decision directly (carrier selection + requirements); assembles a
carrier-specific submission package for broker review. See
`docs/WORKFLOW_TEMPLATE.md` and `docs/PHASES_AND_STAGES.md` (Phase 3) for
where this sits in the delivery plan, and this workflow's
`RULE_ENGINE_INTERPRETATION_GUIDE.md` (in `Workflow_11/test_dataset`) for the
full PA-01..PA-07 rule spec.

Auth: Phase-0 header stub — every request needs `x-tenant-id`, `x-user-id`,
`x-role` (`junior|senior|admin`).

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/es/package-assembly/run` | Assemble one package per selected carrier (fan-out — see below) |
| GET | `/api/es/package-assembly` | List this tenant's package review items |
| GET | `/api/es/package-assembly/{item_id}` | One package's full detail |
| POST | `/api/es/package-assembly/{item_id}/approve` | Broker approves (blocked by a 409 if the package is `BLOCKED` — see FR-10) |
| POST | `/api/es/package-assembly/{item_id}/edit` | Logs a broker edit (FR-21) — no status change |
| POST | `/api/es/package-assembly/{item_id}/send` | Marks as sent (manual log only — this workflow never transmits anything) |

### `POST /run` request

```json
{ "scenario_ref": "scenario_03", "carrier_id": "CAR-03" }
```
`carrier_id` is optional — omit it to assemble packages for **every** carrier
the broker selected in that Market Matching output (FR-2/FR-23: independent,
parallel assembly, never one shared result reused across carriers). The
response is always a **list**, even for a single-carrier scenario.

`scenario_ref` is a Workflow_11 fixture reference today (`scenario_01`..
`scenario_06`); in production this becomes a real Market Matching decision
id — the FE-facing shape is unaffected either way.

### `POST /run` response (real sample — Scenario 04, Oakwood → Coastal Mutual)

```json
[
  {
    "id": "a5e4c2f8-f6b7-4978-8a40-f2b51da24b14",
    "submission_id": "SUB-3303",
    "carrier_id": "CAR-04",
    "status": "pending",
    "payload": {
      "package_id": "4b11e804-2a21-4d21-8bd8-9f96bad8b3a2",
      "submission_id": "SUB-3303",
      "carrier_id": "CAR-04",
      "carrier_name": "Coastal Mutual Specialty",
      "status": "READY",
      "document_checklist": [
        { "document_type": "ACORD 125", "included": true, "source": "ACORD 125" },
        { "document_type": "ACORD 140", "included": true, "source": "ACORD 140" },
        { "document_type": "5-year loss run", "included": true, "source": "5-year loss run" },
        { "document_type": "current financials", "included": true, "source": "current financials" },
        { "document_type": "Statement of Values (SOV)", "included": true, "source": "Statement of Values (SOV)" }
      ],
      "supplemental_form_fields": [
        { "field_name": "year_built", "value": "2010", "auto_filled": true, "source_citation": "sov_report.txt" },
        { "field_name": "construction_type", "value": "Masonry/Steel Frame", "auto_filled": true, "source_citation": "sov_report.txt#line 9" },
        { "field_name": "sprinklered", "value": "Yes", "auto_filled": true, "source_citation": "sov_report.txt" },
        { "field_name": "total_insurable_value", "value": "18000000.0", "auto_filled": true, "source_citation": "sov_report.txt" },
        { "field_name": "unit_count_estimate_from_TIV_and_class", "value": null, "auto_filled": false, "source_citation": null }
      ],
      "diligent_search_attached": false,
      "cover_letter": {
        "body": "...(grounded, cited draft text)...",
        "citations": []
      },
      "blocking_items": [],
      "gap_items_disclosed": [],
      "status_log": [
        { "action": "generated", "timestamp": "2026-07-24T07:28:52.877333+00:00", "user": "demo-es-junior" }
      ]
    }
  }
]
```

Note the last supplemental field: `unit_count_estimate_from_TIV_and_class` is
**never** auto-filled, even though the carrier's own profile metadata lists
it as fillable — there is no direct extracted source for it (PA-02's hard
grounding boundary, verified against the real underlying SOV document, which
has no unit-count line). The FE must render `auto_filled: false` fields as
manual-entry inputs, never as pre-populated-but-editable.

### `GET /package-assembly` (list) response shape
```json
[{ "id": "...", "submission_id": "SUB-3303", "status": "pending" }]
```

### `GET /package-assembly/{item_id}` (detail) response shape
Same shape as one entry of the `/run` response above, with `payload` present
once persisted (`null` if the item somehow has no output package).

### Action responses
```json
{ "id": "...", "submission_id": "SUB-3303", "status": "approved" }
```
`approve`/`send` on a `BLOCKED` package → **HTTP 409**, not a normal status
transition (FR-10 — never present the same one-click affordance a
READY/READY_WITH_GAP package gets).

---

## FE field → API field map

| FE screen element | API field |
|---|---|
| Package status badge | `payload.status` (`READY` \| `READY_WITH_GAP` \| `BLOCKED`) |
| "N items block this package" | `payload.blocking_items[].item` / `.reason` |
| "Disclosed gap" note | `payload.gap_items_disclosed[].item` |
| Document checklist | `payload.document_checklist[]` (`document_type`, `included`, `source`) |
| Supplemental form — auto-filled fields | `payload.supplemental_form_fields[]` where `auto_filled: true`; show `source_citation` as a grounding tooltip |
| Supplemental form — manual-entry fields | same array where `auto_filled: false` — render as an empty input, never pre-filled |
| Diligent-search attachment indicator | `payload.diligent_search_attached` |
| Cover letter draft | `payload.cover_letter.body` (+ `citations`) |
| Activity/audit trail | `payload.status_log[]` |
| One-click actions | `approve` / `edit` / `send` endpoints above |

---

## Verified against real data
Every field above is captured from an actual live run against the real
Workflow_11 dataset (Scenario 04) and the real underlying Workflow_10
Oakwood Apartment Homes SOV document — not a hypothetical shape.
