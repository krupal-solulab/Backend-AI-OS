# Insurance OS — Backend (`Insurance_BE`)

Shared backend for **two insurance verticals** that reuse one platform:

- **MGA** (underwriting on delegated authority) — powers `MGA-FE`.
- **Wholesale E&S broker** (placing risk with carriers) — powers `Insurance-FE`.

**Core idea:** build the expensive, common engine **once** (extraction, ingestion, rules, LLM, audit, reporting, auth), then add a **thin per-vertical layer** (decision core + workflow modules). One multi-tenant deployment; a `vertical` flag selects the right rules/schemas/workflows.

This lets **two developers work in parallel with zero conflict** — one on MGA Submission Triage, one on the E&S first workflow — because each owns a separate module folder and everyone depends on frozen shared interfaces.

---

## Read these docs in order
| Doc | What it covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Tech stack, request flow, multi-tenancy, high-level design |
| [docs/FOLDER_STRUCTURE.md](docs/FOLDER_STRUCTURE.md) | Where everything lives (and who owns what) |
| [docs/CORE_MODULES.md](docs/CORE_MODULES.md) | The shared modules + their interfaces (build once) |
| [docs/PHASES_AND_STAGES.md](docs/PHASES_AND_STAGES.md) | The full delivery plan, phase by phase |
| [docs/PARALLEL_WORK.md](docs/PARALLEL_WORK.md) | The no-conflict rules so 2+ devs work simultaneously |
| [docs/WORKFLOW_TEMPLATE.md](docs/WORKFLOW_TEMPLATE.md) | Step-by-step recipe to build ANY workflow |
| [docs/DATA_AND_FIXTURES.md](docs/DATA_AND_FIXTURES.md) | Test-data + validation-rules convention |
| [docs/CONNECTORS_NANGO.md](docs/CONNECTORS_NANGO.md) | Gmail / Sheets / Drive via Nango |

## Tech stack (proposed)
- **Node.js + TypeScript**, **NestJS** (modular monolith — one NestJS *module* per workflow = clean ownership boundaries).
- **PostgreSQL + Prisma** (data), **Redis + BullMQ** (async ingestion/processing jobs).
- **Anthropic Claude** for the LLM layer (drafting/narrative, citation-enforced).
- **Nango** for all Google/Gmail connectors (read mail + attachments, send mail, Sheets/Drive read-write).
- **Zod** for schema validation, **pnpm** workspace.

> NestJS is chosen specifically because its module system makes parallel work safe — see PARALLEL_WORK.md. Fastify + a manual module layout is a fine alternative if the team prefers.

## One-minute mental model
```
Email/docs ─► Ingestion (Nango) ─► Extraction Core ─► Rules Engine (validation)
      ─► Decision Core (per vertical: appetite | matching) ─► LLM (draft, cited)
      ─► Output Package ─► Review Queue ─► Human approves ─► write-back + Audit
```
Everything left of "Decision Core" is **shared**. The Decision Core and the workflow orchestration are **per vertical**.
