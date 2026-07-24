# FE Contract — E&S Retail Agent Communication Copilot

`/api/es/agent-communication` — the third E&S workflow. Drafts a
retail-agent-facing email from the structured output of Market Matching or
Package Assembly (or a manually-logged quote/bind entry) — never sends
anything (human-in-the-loop, FR-20, no exceptions). See
`docs/WORKFLOW_TEMPLATE.md` and `docs/PHASES_AND_STAGES.md` (Phase 3) for
where this sits in the delivery plan, and
`Data sets/Workflow 3/TONE_FRAMING_RULES_GUIDE.md` for the full RA-TN tone
rule spec this workflow's drafts are calibrated against.

Auth: Phase-0 header stub — every request needs `x-tenant-id`, `x-user-id`,
`x-role` (`junior|senior|admin`).

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/es/agent-communication/run` | Draft one communication from a trigger object |
| GET | `/api/es/agent-communication` | List this tenant's drafted-communication review items |
| GET | `/api/es/agent-communication/{item_id}` | One draft's full detail |
| POST | `/api/es/agent-communication/{item_id}/approve` | Broker approves (blocked by a 409 if `requires_compliance_review` — see below) |
| POST | `/api/es/agent-communication/{item_id}/edit` | Logs a broker edit (FR-17) — no status change |
| POST | `/api/es/agent-communication/{item_id}/send` | Marks as sent (manual log only — this workflow never transmits anything, FR-20) |
| POST | `/api/es/agent-communication/{item_id}/discard` | Marks the draft discarded (FR-15's third action) |
| POST | `/api/es/agent-communication/{item_id}/compliance-clear` | **Senior/admin only.** Clears the compliance-review gate on a No Market Found draft |

### Automatic vs. manual triggers (2026-07-24)

`POST /run` above is still the only entry point, but as of `verticals/es/agent_communication_hooks.py`,
three of the six trigger types now fire it **automatically** — no FE action or manual `/run` call
needed for these:

| Trigger type | Status | Fires from |
|---|---|---|
| `NO_MARKET_FOUND` | **Automatic** | `market_matching`'s own `/run`, when the decision is a true zero-match |
| `SUBMISSION_ACKNOWLEDGMENT` | **Automatic** (per carrier) | `package_assembly`'s own `/run`, when a carrier's package is `READY` |
| `MISSING_INFO_REQUEST` | **Automatic** (per carrier) | `package_assembly`'s own `/run`, when a carrier's package is `BLOCKED` or `READY_WITH_GAP` |
| `NO_RESPONSE_FOLLOWUP` | **Manual only, permanently deferred** | Needs elapsed-time-vs-acceptance-window monitoring (FR-11) — an Arq periodic job, not built yet. Still fireable manually. |
| `QUOTE_TERMS_SUMMARY` | **Manual only, by design** | FR-2 — no automated source until Quote Comparison exists |
| `PLACEMENT_CONFIRMATION` | **Manual only, by design** | FR-2 — no automated source until Quote Comparison exists |

**What this means for the FE:** nothing — `GET /api/es/agent-communication` already lists every
draft for the tenant regardless of how it was created, and the response shape is identical either
way. The FE's existing `FIXTURE_TRIGGERS` buttons remain the only way to fire the 3 manual-only
types (and still work for the 3 automatic types too, subject to the same FR-5 dedup an auto-fire
would hit). There is currently no field distinguishing "auto-fired" from "manually-fired" in the
response — not added, since nothing in the UI depends on that distinction today.

**Known v1 gap:** auto-fired drafts don't have `named_insured` (see `docs/STATUS.md`'s Phase 4
entry for why) — their subject line falls back to `"Submission - ..."` instead of naming the
insured. Manually-fired drafts (via the FE's `FIXTURE_TRIGGERS`, which embed the full fixture
object including `named_insured`) are unaffected.

**Submission Acknowledgment is per-carrier, not combined.** A submission with multiple carriers
approached will produce one acknowledgment draft per carrier as each is packaged, not the single
combined draft the PRD's Trigger 01 sample shows — see `docs/STATUS.md` for why that's an
explicit v1 scope decision, not an oversight.

### `POST /run` request

```json
{ "trigger": { "trigger_type": "PLACEMENT_CONFIRMATION", "submission_id": "SUB-3303", "...": "..." } }
```
`trigger` is the triggering object as-is — a Market Matching / Package
Assembly output object, or a manually-logged entry (FR-2, for the two types
that don't yet have an automated source: Quote/Terms Summary and Placement
Confirmation, since Quote Comparison isn't built yet). Today this is one of
the six `Workflow_12` fixture shapes (`trigger_01`..`trigger_06`); in
production it's whatever object the upstream workflow actually produces —
the FE-facing response shape is unaffected either way.

Two safeguards enforced server-side on this endpoint, not left as FE
conventions:
- **FR-5** — POSTing the same `(submission_id, trigger_type, carrier)` again
  while an unresolved draft already exists returns that **same** item
  (`"deduplicated": true`) instead of creating a second one.
- **FR-12** — a second `NO_RESPONSE_FOLLOWUP` for the same original request
  returns **HTTP 409** (at most one follow-up, ever).

### `POST /run` response (real sample — Trigger 04, Oakwood bound with Coastal Mutual)

```json
{
  "id": "df1b89b2-cfbc-4876-90f3-ebb6ea524c7f",
  "submission_id": "SUB-3303",
  "status": "pending",
  "deduplicated": false,
  "payload": {
    "draft_id": "2d13d152-df49-425d-8dca-36c31f7024d7",
    "trigger_type": "PLACEMENT_CONFIRMATION",
    "source_workflow": "Manual broker input (Quote Comparison workflow not yet built)",
    "source_record_id": "SUB-3303",
    "submission_id": "SUB-3303",
    "named_insured": "Oakwood Apartment Homes LP",
    "carrier_name": "Coastal Mutual Specialty",
    "retail_agent_name": "James Foster",
    "retail_agency": "Crestview Agency",
    "subject_line": "Oakwood Apartment Homes LP - Bound with Coastal Mutual Specialty",
    "body": "...(grounded, cited draft text)...",
    "requires_compliance_review": false,
    "carrier_names_disclosed": true,
    "grounding_citations": [
      { "claim": "Oakwood Apartment Homes LP", "source_field": "named_insured" },
      { "claim": "Coastal Mutual Specialty", "source_field": "carrier_name" },
      { "claim": "{'premium': '$81,500', ...}", "source_field": "bound_terms" }
    ],
    "status": "DRAFT",
    "edit_distance_from_original": null,
    "generated_timestamp": "2026-07-24T10:47:17.644863+00:00",
    "sent_timestamp": null
  }
}
```

### Compliance-gated sample (Trigger 03, No Market Found)

```json
{
  "id": "66e7c580-210a-4605-90f0-7502b8e91316",
  "submission_id": "SUB-3306",
  "status": "pending",
  "payload": {
    "trigger_type": "NO_MARKET_FOUND",
    "carrier_name": null,
    "requires_compliance_review": true,
    "carrier_names_disclosed": false,
    "status": "UNDER_COMPLIANCE_REVIEW",
    "...": "..."
  }
}
```
`carrier_name` is `null` and `carrier_names_disclosed` is `false` here — this
is the ONE trigger type that must never name a specific carrier (RA-TN-06,
the carrier-name-disclosure question is an **unresolved compliance decision**,
not a product default; see the PRD's Section 6/9). `approve`/`send` on this
draft return **HTTP 409** until `POST /{item_id}/compliance-clear` runs (senior/
admin only — a junior attempt gets **HTTP 403**).

### `GET /agent-communication` (list) response shape
```json
[{ "id": "...", "submission_id": "SUB-3303", "status": "pending" }]
```

### `GET /agent-communication/{item_id}` (detail) response shape
Same shape as the `/run` response above, with `payload` present once
persisted (`null` if the item somehow has no output package).

### Action responses
```json
{ "id": "...", "submission_id": "SUB-3303", "status": "approved" }
```
`approve`/`send` also update `payload.status` to `APPROVED`/`SENT` (and stamp
`sent_timestamp` on send) so the FE can read a single field for the draft's
lifecycle state without cross-referencing the outer `status`.

---

## FE field → API field map

| FE screen element | API field |
|---|---|
| Draft subject/body | `payload.subject_line` / `payload.body` |
| Compliance-review banner (non-dismissable until cleared) | `payload.requires_compliance_review` — show a persistent banner, not a dismiss-once alert, per FR-8 |
| "Carrier names shown in this draft" indicator | `payload.carrier_names_disclosed` |
| Grounding / "why this claim" tooltip | `payload.grounding_citations[]` (`claim`, `source_field`) |
| Draft lifecycle badge | `payload.status` (`DRAFT` \| `UNDER_COMPLIANCE_REVIEW` \| `APPROVED` \| `SENT` \| `DISCARDED`) |
| One-click actions | `approve` / `edit` / `send` / `discard` endpoints above |
| Compliance sign-off action (senior/admin view only) | `compliance-clear` endpoint |
| Duplicate-draft indicator | `deduplicated` on the `/run` response |

---

## Known v1 simplifications (flagged for the FE, not hidden)
- **`edit_distance_from_original`** is always `null` in v1 — computing a real
  edit distance needs the FE to submit the edited body back, which isn't
  built yet (FR-17 logs the edit action itself via the `edit` endpoint, but
  doesn't diff text). Follow-on work, not a bug.
- **`discard`/`compliance-clear`** are workflow-owned endpoints, not
  `ReviewAction` values — `core/common`'s frozen enum has no "discarded" or
  "compliance sign-off" value, so both flip the workflow's own
  `payload.status`/`payload.requires_compliance_review` fields directly
  instead. Functionally equivalent for the FE; just don't expect them to show
  up in a generic `ReviewAction` picker.
- **Subject-line reuse for follow-ups (FR-10)** looks up the actual prior
  draft's subject line in this workflow's own history first; if none exists
  (e.g. the original was never run through this system), it falls back to a
  deterministic `"Re: {named_insured}[- carrier] Follow-up"` reconstruction.

---

## Verified against real data
Every field above is captured from an actual live run against the real
Workflow_12 dataset (Triggers 03 and 04) — not a hypothetical shape. The
compliance-gate flow (`approve` → 409 → junior `compliance-clear` → 403 →
senior `compliance-clear` → 200 → `approve` → 200) was exercised live end to
end, not just asserted in pytest.
