# Test Data & Validation Rules

Every workflow ships with a **test dataset** and its **validation rules** in the same place. Devs run their workflow against these fixtures — they are the source of truth for "does it work."

## Location convention
```
TEST_DATA_ROOT = D:\INDUSTRY AI OS bac\test data
  Workflow_<N>\test_dataset\
    README.md                          # what the dataset covers
    Validation_Rules_Test_Dataset.md   # the rules + expected outcomes for this workflow
    submission_01\ … submission_10\    # one folder per sample case
```

### Confirmed layout for Workflow_1 (MGA Submission Triage)
```
Workflow_1/test_dataset/
  README.md
  Validation_Rules_Test_Dataset.md
  submission_01/
    acord_application.txt
    email.txt
    financial_statement.txt
    loss_run.txt
  submission_02/ ...  (through submission_10)
```
Each `submission_XX/` = one broker submission: the cover **email** + its **attachments** (ACORD application, loss run, financials). Later workflows add SOV, endorsement request, renewal questionnaire, etc.

> **Document sets are VARIABLE — do not assume exactly 4 docs.** In the real Workflow_1 data, most submissions have 4 (`acord_application`, `email`, `financial_statement`, `loss_run`) but some differ: `submission_06` adds `sov_report` (5 docs) and `submission_09` includes `sov_report` but has **no** `financial_statement` (4 docs, different mix). Submissions may therefore have **2–5+ docs**. Two consequences:
> - The fixtures loader stays **tolerant**: it loads whatever `.txt` files exist and classifies each by filename — it never requires a fixed set. `sov` (`sov_report` → SOV) is a **supported document kind**.
> - "Required document present" is a **data-driven VALIDATION RULE in Phase 1** (a `required`/`crossDoc` check in the Rules Engine → surfaces as **missing-info / `REQUEST_INFO`**), **NOT** loader logic. The loader reports what's there; the rules decide what's missing.

## Mapping: workflow → dataset number
| N | Vertical · Workflow |
|---|---|
| 1 | MGA · Submission Triage |
| 2 | MGA · Renewal Management |
| 3 | MGA · Bordereau |
| 4 | MGA · Broker Communication |
| 5 | MGA · Quoting & Rating |
| 6 | MGA · Endorsement |
| 7 | MGA · Bind Order & Issuance |
| 8 | MGA · Appetite Governance |
| 9 | MGA · Portfolio |
| 10 | E&S · Market Matching |
| 11 | E&S · Package Assembly |
| 12 | E&S · Retail Agent Communication |
| 13 | E&S · Quote Comparison & Recommendation |
| 14 | E&S · Binder & Policy Issuance Coordination |
| … | Further E&S workflows continue from 15 |
> Keep this table in sync as datasets are added.

### Workflow_10 (E&S Market Matching) layout note
This dataset additionally ships a `carrier_profiles/` folder (6 JSON carrier
appetite profiles — not `.txt` submission documents). `src/fixtures/loader.py`
ignores it (its glob only matches `submission_*`); it's loaded separately by
`verticals/es/decision_core/carrier_profiles.py`, which is E&S-owned, not
shared fixtures code. It also ships `RULE_ENGINE_INTERPRETATION_GUIDE.md`
alongside `Validation_Rules_Test_Dataset.md` (the former is the detailed
rule-by-rule spec; the latter consolidates the expected-outcome table per
convention).

### Workflow_11 (E&S Package Assembly) layout note
This dataset does NOT follow the `submission_XX/*.txt` shape at all —
`src/fixtures/loader.py` doesn't apply here (its glob matches `submission_*`,
not `scenario_*`, and it turns `.txt` files into `Document` rows, which this
workflow doesn't ingest). Each `scenario_XX/` folder ships a
`market_matching_output.json` (the previous workflow's decision output —
carrier selection, requirements, upstream missing-info/diligent-search) and
an `expected_package_manifest.txt` (the human-readable acceptance spec for
that scenario). Loaded by
`verticals/es/workflows/package_assembly/scenario_loader.py`, which is
E&S-owned, not shared fixtures code — same precedent as Workflow_10's
`carrier_profiles/`. This workflow also resolves the underlying Workflow_10
submission (by matching `named_insured`) to re-derive field-level extracted
data the scenario JSON doesn't inline — see that folder's
`Validation_Rules_Test_Dataset.md` for why.

### Workflow_12 (E&S Retail Agent Communication) layout note
Same non-`submission_XX` precedent as Workflow_11: `src/fixtures/loader.py`
doesn't apply here either (its glob matches `submission_*`, not `trigger_*`).
Each `trigger_XX/` folder ships a `trigger_input.json` (a Market Matching /
Package Assembly output object, or a manually-logged quote/bind entry — this
workflow's actual input shape, per its PRD §1) plus `expected_draft.txt` and
(for triggers 01/02/04/05/06 — not 03) a `tone_notes.txt`. Loaded by
`verticals/es/workflows/agent_communication/trigger_loader.py`, which is
E&S-owned, not shared fixtures code. Unlike Workflow_11, this workflow's live
`POST /run` endpoint accepts a trigger object directly in the request body
(the fixture loader is a test/eval-only convenience, not something the
pipeline itself calls) — this matches the PRD's own framing that ALL six
communication types are triggered by an already-materialized structured
object, not something this workflow re-derives itself. `expected_draft.txt`
is illustrative prose for a human reviewer, not a literal string the eval
suite asserts against verbatim (LLM/mock-LLM phrasing varies) — the eval
instead asserts structural/behavioral properties (correct trigger
classification, correct carrier scoping, the compliance gate, grounded facts
present in the draft).

### Workflow_13 (E&S Quote Comparison & Recommendation) layout note
Unlike Workflow_11/12's single JSON per case, `scenario_XX/` here ships RAW
carrier-response `.txt` files (unstructured email text, non-uniform
filenames — `carrier_response_ironclad.txt`, `carrier_response_alt_market.txt`,
`carrier_response_a.txt`, etc.). `src/fixtures/loader.py` doesn't apply (its
glob matches `submission_*`, not `scenario_*`, and even if it did, its
Key:Value-per-line classifier would mis-parse this dataset's multi-line
subjectivity clauses). `scenario_06` additionally ships a
`system_check_context.json` giving an explicit "as of" reference date for
QC-07's validity check (the only scenario that needs one — every other
scenario's eval test supplies its own "as of" date reflecting roughly when
its responses arrived). Loaded by
`verticals/es/workflows/quote_comparison/scenario_loader.py` (E&S-owned) and
parsed by that workflow's own native `quote_parser.py` — not the shared
`ExtractionService` (see that module's docstring for the specific, evidenced
reasons: wrapped subjectivity lines, prose-only declinations, multi-value
deductible lines).

### Workflow_14 (E&S Binder & Policy Issuance Coordination) layout note
Input shape genuinely varies BY LIFECYCLE STAGE within this one dataset —
`scenario_01/03/04` ship `broker_bind_instruction.json` (structured) +
`carrier_bind_confirmation.txt` (a raw email, same shape family as
Workflow_13's carrier responses); `scenario_02` ships only the instruction
(no confirmation — the bind never got sent, per BI-02's blocking test);
`scenario_05/06` ship `bind_record.json` (a later-stage, already-confirmed
snapshot) + optionally `issued_policy_document_extract.txt` (a declarations
page — NOT an email, no headers at all, a third distinct format).
`src/fixtures/loader.py` doesn't apply for any of this. Loaded by
`verticals/es/workflows/binder_issuance/scenario_loader.py` (E&S-owned) and
parsed by that workflow's own native `bind_parser.py` — independent of
Workflow_13's `quote_parser.py` (no cross-import), per the approved plan:
the declarations-page format alone is different enough that a shared
implementation wouldn't fully unify the two anyway.

## How the loader works (`src/fixtures/loader.py`)
- Reads `TEST_DATA_ROOT` from `.env`.
- `load_workflow(n)` → scans `Workflow_<n>/test_dataset/submission_*`, turns each folder into a `Submission` + `list[Document]` (one `Document` per `.txt`, `kind` inferred from filename: `acord_application`→ACORD, `loss_run`→Loss Run, `financial_statement`→Financials, `email`→Email).
- `load_rules(n)` → reads `Validation_Rules_Test_Dataset.md` (and any `rules.json`) so tests can assert expected pass/fail + recommendation.
- Used by: **dev seed script** (populate a local DB to click through the FE) and **workflow eval tests** (pytest).

## Rules
- **Never hardcode fixtures inside workflow code** — always go through the loader, so swapping datasets doesn't touch code.
- Filenames drive document classification in the loader — keep the `acord_application / loss_run / financial_statement / email / sov / ...` naming consistent across datasets.
- The `Validation_Rules_*.md` in each dataset is the **expected-outcome spec** for that workflow's eval — treat it as the acceptance test.
- Ingestion in production uses **Nango (Gmail)**; fixtures are the offline equivalent so devs don't need a live mailbox to build.
- **Phase 1 consumers:** `MockConnectorService` serves these fixtures offline; `DefaultExtractionService` parses them into a cited field model; and the smoke-test validation rule set lives in `tests/fixtures/ruleset_workflow1.json` (loaded into a `RuleVersion` at test setup — the JSON form of the expected-outcome spec). "Required document present" is enforced there as `required` rules, so submission_09's missing financials surfaces as missing-info / `REQUEST_INFO`.
- **Phase 2 (Submission Triage):** the acceptance test is `verticals/mga/submission_triage/eval_test.py`, which asserts all 10 submissions match the **Expected Recommendation** column of this dataset's README. Validation rules are data (`verticals/mga/rulesets/workflow1_validation.json`); appetite thresholds (excluded class, severity ceiling, variance %, lead-time, confidence floor, etc.) are data in `verticals/mga/decision_core/config.py` — all placeholders from `Validation_Rules_Test_Dataset.md`, to be replaced with the design partner's real appetite guide.

## `.env`
```
TEST_DATA_ROOT="D:\INDUSTRY AI OS bac\test data"
```
