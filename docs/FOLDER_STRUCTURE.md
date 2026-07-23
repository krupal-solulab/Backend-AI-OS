# Folder Structure

```
Insurance_BE/
├─ README.md
├─ .env.example
├─ pyproject.toml               # uv/Poetry deps
├─ alembic.ini
├─ docs/                        # all the docs in this folder
├─ migrations/                  # Alembic migrations (shared base + per-vertical)
├─ src/
│   ├─ main.py                  # FastAPI app; includes core + both vertical routers
│   ├─ core/                    # ─────── SHARED · BUILD ONCE ───────
│   │   ├─ common/              # Pydantic DTOs, Protocols, CONTRACTS (frozen early)
│   │   ├─ db.py                # engine/session (SQLModel/SQLAlchemy)
│   │   ├─ tenancy/             # tenant + vertical resolution (FastAPI dependency)
│   │   ├─ auth/                # RBAC, roles, authority limits, dependencies
│   │   ├─ ingestion/           # Nango connectors: gmail/sheets/drive
│   │   ├─ documents/           # document store + retrieval
│   │   ├─ extraction/          # Extraction Core (classify + extract + cite)
│   │   ├─ rules_engine/        # generic evaluator (validation + appetite/matching, config-driven)
│   │   ├─ llm/                 # OpenAI wrapper behind LLMService (grounded, cited)
│   │   ├─ review_queue/        # human review items + actions
│   │   ├─ audit/               # decision/audit log (feeds Governance & Portfolio)
│   │   ├─ reporting/           # aggregation/rollup framework
│   │   └─ jobs/                # Celery app + workers
│   │
│   ├─ verticals/
│   │   ├─ mga/
│   │   │   ├─ router.py         # includes MGA workflow routers (MGA dev edits only this)
│   │   │   ├─ decision_core/    # APPETITE engine
│   │   │   └─ workflows/
│   │   │       ├─ submission_triage/     ← Dev A owns this whole package
│   │   │       │   ├─ __init__.py
│   │   │       │   ├─ router.py           (/api/mga/submission-triage)
│   │   │       │   ├─ service.py          (the pipeline)
│   │   │       │   └─ schema.py           (Pydantic models)
│   │   │       ├─ renewal_management/
│   │   │       ├─ quoting_rating/
│   │   │       ├─ endorsement/
│   │   │       ├─ bind_order/
│   │   │       ├─ broker_communication/
│   │   │       ├─ appetite_governance/
│   │   │       ├─ portfolio_reporting/
│   │   │       └─ bordereau/
│   │   └─ es/
│   │       ├─ router.py         # includes E&S workflow routers (E&S dev edits only this)
│   │       ├─ decision_core/    # MATCHING / RANKING engine
│   │       └─ workflows/
│   │           ├─ market_matching/       ← Dev B owns this whole package
│   │           ├─ package_assembly/
│   │           ├─ agent_communication/
│   │           ├─ quote_comparison/
│   │           ├─ binder_issuance/
│   │           ├─ endorsement/
│   │           ├─ renewal_remarketing/
│   │           ├─ diligent_search/
│   │           ├─ carrier_appetite_intel/
│   │           └─ pipeline_reporting/
│   │
│   └─ fixtures/                 # loads test data (see DATA_AND_FIXTURES.md)
│       └─ loader.py
└─ tests/
```

## Ownership rule (why there are no merge conflicts)
- **Each workflow = one package** under `verticals/{vertical}/workflows/{workflow}/`. A dev works **only inside their package**.
- The only shared file a dev touches to "register" a workflow is their **own** `verticals/mga/router.py` or `verticals/es/router.py` (`include_router(...)`) — different files for different verticals → no conflict.
- `core/*` is stabilized in Phase 1 and changed rarely afterward (via the lead / PR review).
- **MGA dev never edits `es/`; E&S dev never edits `mga/`; neither edits the other's vertical router.**
