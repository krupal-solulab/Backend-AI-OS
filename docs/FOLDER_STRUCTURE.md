# Folder Structure

```
Insurance_BE/
├─ README.md
├─ .env.example
├─ docs/                         # all the docs in this folder
├─ prisma/
│   └─ schema.prisma             # shared base tables + per-vertical tables
├─ src/
│   ├─ main.ts
│   ├─ app.module.ts             # wires core + both verticals
│   │
│   ├─ core/                     # ─────── SHARED · BUILD ONCE ───────
│   │   ├─ common/               # DTOs, interfaces, CONTRACTS (frozen early)
│   │   ├─ tenancy/              # tenant + vertical resolution
│   │   ├─ auth/                 # RBAC, roles, authority limits, guards
│   │   ├─ ingestion/            # Nango connectors: gmail/sheets/drive
│   │   ├─ documents/            # document store + retrieval
│   │   ├─ extraction/           # Extraction Core (classify + extract + cite)
│   │   ├─ rules-engine/         # generic evaluator (validation + appetite/matching, config-driven)
│   │   ├─ llm/                  # Claude wrapper (grounded, cited)
│   │   ├─ review-queue/         # human review items + actions
│   │   ├─ audit/                # decision/audit log (feeds Governance & Portfolio)
│   │   ├─ reporting/            # aggregation/rollup framework
│   │   └─ jobs/                 # BullMQ queues + workers
│   │
│   ├─ verticals/
│   │   ├─ mga/
│   │   │   ├─ mga.module.ts     # registers MGA workflows (MGA dev edits only this)
│   │   │   ├─ decision-core/    # APPETITE engine
│   │   │   └─ workflows/
│   │   │       ├─ submission-triage/     ← Dev A owns this whole folder
│   │   │       │   ├─ submission-triage.module.ts
│   │   │       │   ├─ submission-triage.controller.ts   (/api/mga/submission-triage)
│   │   │       │   ├─ submission-triage.service.ts      (the pipeline)
│   │   │       │   └─ submission-triage.schema.ts       (Zod + types)
│   │   │       ├─ renewal-management/
│   │   │       ├─ quoting-rating/
│   │   │       ├─ endorsement/
│   │   │       ├─ bind-order/
│   │   │       ├─ broker-communication/
│   │   │       ├─ appetite-governance/
│   │   │       ├─ portfolio-reporting/
│   │   │       └─ bordereau/
│   │   └─ es/
│   │       ├─ es.module.ts      # registers E&S workflows (E&S dev edits only this)
│   │       ├─ decision-core/    # MATCHING / RANKING engine
│   │       └─ workflows/
│   │           ├─ market-matching/       ← Dev B owns this whole folder
│   │           ├─ package-assembly/
│   │           ├─ agent-communication/
│   │           ├─ quote-comparison/
│   │           ├─ binder-issuance/
│   │           ├─ endorsement/
│   │           ├─ renewal-remarketing/
│   │           ├─ diligent-search/
│   │           ├─ carrier-appetite-intel/
│   │           └─ pipeline-reporting/
│   │
│   └─ fixtures/                 # loads test data (see DATA_AND_FIXTURES.md)
│       └─ fixtures.service.ts
└─ test/
```

## Ownership rule (why there are no merge conflicts)
- **Each workflow = one folder** under `verticals/{vertical}/workflows/{workflow}/`. A dev works **only inside their folder**.
- The only shared file a dev touches to "register" a workflow is their **own** `mga.module.ts` or `es.module.ts` (different files for different verticals → no conflict).
- `core/*` is stabilized in Phase 1 and changed rarely afterward (via the lead / PR review).
- **MGA dev never edits `es/`; E&S dev never edits `mga/`; neither edits the other's vertical module.**
