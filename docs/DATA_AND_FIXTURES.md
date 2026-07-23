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
| … | E&S workflows continue (agree numbering with the E&S dev) |
> Keep this table in sync as datasets are added.

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

## `.env`
```
TEST_DATA_ROOT="D:\INDUSTRY AI OS bac\test data"
```
