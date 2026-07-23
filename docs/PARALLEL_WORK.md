# Parallel Work — how 2+ devs build without conflict

The goal: once the shared core exists, **Dev A builds MGA Submission Triage** and **Dev B builds the E&S first workflow at the same time**, and they never block or merge-conflict each other. Same rules apply to every later workflow.

## The 5 rules
1. **Contracts are frozen first (Phase 0.4).** The pipeline interface + shared DTOs live in `core/common` and are agreed before workflow work starts. Everyone codes against them. Changing a contract = a deliberate, reviewed event, not an ad-hoc edit.
2. **One folder per workflow, one owner.** A workflow lives entirely in `verticals/{vertical}/workflows/{workflow}/`. You only edit files inside your folder.
3. **Register in your own vertical router only.** To mount a workflow you add one `include_router(...)` line to **your** `verticals/mga/router.py` *or* `verticals/es/router.py`. Different files → no conflict between the two verticals. (Within a vertical, keep the includes ordered to minimize same-file churn.)
4. **Never cross verticals.** MGA dev doesn't touch `verticals/es/**`; E&S dev doesn't touch `verticals/mga/**`. Neither edits `core/**` (that's the lead's, post-Phase-1).
5. **Depend on interfaces, not implementations.** Get shared capability via DI (`ExtractionService`, `RulesEngine`, `LlmService`, `ConnectorService`, `AuditService`, `ReviewQueueService`). If the core lacks something, request a contract addition — don't fork the core inside a workflow.

## What each dev touches for a new workflow
```
✎  verticals/<vertical>/workflows/<workflow>/**        (new package — yours)
✎  verticals/<vertical>/router.py                      (one include_router line — your vertical only)
✎  models + Alembic migration  → only if you need new tables (namespace them, own migration)
✎  fixtures + eval for your workflow
✗  core/**            (don't)
✗  the other vertical  (don't)
```

## Migrations without stepping on each other
- Prefer **additive** migrations; name them `NNN_<vertical>_<workflow>_<change>`.
- Two devs adding tables the same day = two separate migration files → no conflict. Coordinate only if editing a shared base table (rare; route through the lead).

## Branching
- `main` protected; PR + review.
- Branch per workflow: `feat/mga-submission-triage`, `feat/es-market-matching`.
- CI runs typecheck + that workflow's eval against fixtures.

## Suggested split for the two verticals
| Dev | Owns | Starts with |
|---|---|---|
| **Dev A (MGA)** | `verticals/mga/**` | `decision-core` (appetite) + `submission-triage` |
| **Dev B (E&S)** | `verticals/es/**` | `decision-core` (matching) + `market-matching` |
| **Lead / shared** | `core/**`, contracts, Nango, CI | Phases 0–1, then reviews contract changes |

After Phase 1, A and B are fully independent — they can ship their 2nd, 3rd, … workflows on their own cadence.
