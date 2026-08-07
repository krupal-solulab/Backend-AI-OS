# PRD: Admin Panel (v1)
## Cross-Platform Administration — Coverline OS

**Status:** Draft for engineering scoping
**Owner:** [Product]
**Last updated:** 2026-08-06
**Grounded in:** the current state of `Backend-AI-OS` and `Insurance OS` only — every capability marked "exists" below was verified directly in code before writing this PRD; nothing here is copied from another product. Every capability marked "new" does not exist anywhere in either repo today.

---

## 1. Problem Statement

Coverline OS today has 10 real, working E&S broker workflows and one working integration surface (Gmail/Sheets/Drive/Calendar connect-disconnect), but **no platform-administration surface at all**. Concretely, right now:

- There is no way to see or manage tenants or users. The `Role` enum already has three values (`junior`, `senior`, `admin`), but there is no `admin` user seeded anywhere, and no endpoint to list tenants, list users, create a user, or change a user's role.
- There is no way to view or change the handful of settings that currently only live in a `.env` file and require a server restart to change: the junior approval cap, the Nango connector mode (mock vs. live), the Gmail inbox search query, Quote Comparison's ranking weights, and Carrier Appetite Intelligence's signal threshold.
- There is no cross-workflow activity or audit view. Each of the 10 workflows has its own review queue, scoped to itself — there is no single place to see "what happened across the platform today." The frontend's own Agent Communication activity tab says so directly: its log is *"session-local only — not persisted or read from Backend-AI-OS's audit module."*
- The existing **Settings** page's "Organization" and "Team" panels are 100% hardcoded placeholder UI — fake org fields, four made-up names with made-up titles — wired to nothing. The one real thing on that page is the Integrations panel (Connect/Disconnect Gmail/Sheets/Drive/Calendar), built earlier in this project.
- Nowhere in the frontend does the UI hide or show anything based on the logged-in user's role. Every action button renders for every role today; role enforcement is 100% server-side (a junior gets a 403 toast on a senior-only action). There is no precedent anywhere in this codebase for role-gated navigation or routes.

**Goal of v1:** give an `admin` user one dedicated, role-gated section of the app to: manage tenants and users (including role changes), view and edit the settings above without a redeploy, see real integration-connection status, and see a real cross-workflow activity/audit trail. Replace the two fake Settings panels (Organization, Team) with real data.

**Explicitly not the goal of v1:**
- Real authentication (password/credential login). Login stays exactly what it is today — an email looked up against the `User` table, no password. This PRD does not change that.
- Billing, plans, or subscription management — nothing like this exists today and none is proposed here.
- Self-service tenant signup.
- A granular, per-permission ACL system beyond the existing three-role model.
- A real-time/streaming activity feed — a manual-refresh read view is sufficient for v1.
- Editing a workflow's actual decision logic/rules — only the settings already identified as configuration (§5.4) are in scope; rule engines and pipeline code are not admin-editable.

---

## 2. Scope of v1

### 2.1 In scope
- **AP-01** Admin-only nav section + route guard (net new — no role-gated UI exists today)
- **AP-02** Tenant list + detail view, including the per-tenant `junior_premium_cap` override field that already exists on the `Tenant` table but has no UI or endpoint touching it today
- **AP-03** User list per tenant, role change, new-user creation
- **AP-04** Platform settings viewer + editor for the six currently env-only tunables (§5.4)
- **AP-05** Integrations oversight — an admin-facing wrapper around the already-real, already-tenant-scoped connect/disconnect/list-connections endpoints
- **AP-06** Cross-workflow activity/audit log viewer — new endpoint wrapping the existing, currently-unused `AuditService.query`
- **AP-07** Replace the fake Organization/Team Settings panels with real `Tenant`/`User` data (or fold them into the new Admin section — see §8)

### 2.2 Explicitly out of scope for v1
- Password/credential auth of any kind
- Billing/subscription/plan management
- Self-service tenant signup or onboarding flow
- A permission system finer-grained than junior/senior/admin
- Real-time/streaming activity feed (polling/manual refresh is enough)
- True cross-tenant "platform super-admin" (see the architectural note in §9 — the current login model resolves exactly one tenant per identity; viewing a *different* tenant than the one you logged into is a bigger change than this PRD assumes by default)
- Any change to workflow business/decision logic

### 2.3 Success criteria (must hit before expanding scope)
- An `admin`-role user can log in and see an "Admin" section that a `junior` or `senior` user cannot see or navigate to directly (route-guarded, not just hidden-but-reachable).
- An admin can change a real user's role and have it take effect on that user's *next* login with zero backend code change or restart.
- Every one of the six settings in §5.4 can be viewed and changed from the UI, and the new value is honored by the relevant workflow on its very next real run — no `.env` edit, no restart.
- An admin can see, in one screen, the Nango connection status (connected/disconnected) for Gmail/Sheets/Drive/Calendar for their tenant, without re-deriving it from the existing per-workflow Settings page.
- An admin can see a real, persisted list of recent actions (who did what, on which workflow, when) — not the session-local, page-refresh-loses-it log that exists today.

---

## 3. Users & Personas

| Persona | Role in this panel |
|---|---|
| **Platform Admin** (primary user) | `Role.ADMIN` — manages tenant settings, users/roles, integrations, and reviews the audit trail. **Note:** no admin user is seeded today (`core/seed.py` seeds only junior/senior per tenant) — seeding at least one real admin user is a prerequisite, not a v1 feature to build. |
| **Brokerage Principal / Senior** (secondary, read-leaning) | May want visibility into settings and the audit trail without needing user/tenant management — v1 default is admin-only for the whole section (see open question in §9 on whether senior gets read-only access). |
| **Junior Broker** | No access to this panel at all — not even read-only. |

---

## 4. End-to-End Flow

```
1. Admin logs in via the existing email-lookup login (manager.s@gmail.com-
   style flow) — no change to login itself. Requires a real admin user to
   exist (see seeding gap, §9).
2. AppShell's sidebar shows a new "Admin" section — ONLY when
   identity.role === "admin". Every other role sees today's nav, unchanged.
3. Admin lands on an Overview: tenant identity, connector status summary,
   and the most recent N audit entries.
4. Admin -> Users: sees every User row for their tenant, can change a
   role or create a new user.
5. Admin -> Settings: sees the 6 tunables in §5.4 with their current
   value and source (env default vs. an admin override, if any exist).
   Editing writes an override that takes effect on the next real request
   that reads that setting — no restart.
6. Admin -> Integrations: same real connect/disconnect flow already
   built, presented in the admin context instead of (or in addition to)
   today's per-user Settings page.
7. Admin -> Activity: a real, persisted, filterable list of audit
   entries across all 10 workflows for their tenant.
```

---

## 5. Functional Requirements

### 5.1 Admin-Only Navigation & Route Guard (AP-01)
- **New**, both sides. Backend: a `require_role(Role.ADMIN)` FastAPI dependency already exists as a *utility* (`core/auth/policy.py`) but is used on exactly one endpoint anywhere in the codebase today — every new admin route in this PRD must use it.
- Frontend: **no existing precedent** for role-conditional rendering. Needs a new pattern — e.g. a `useIdentity()` read of `getIdentity()?.role` gating both the sidebar's "Admin" section and a route-level guard in the admin route file(s) (mirroring how `app.tsx` already guards on "is anyone logged in" — extend that pattern to "is this user an admin," not just "is someone logged in").
- Acceptance: a junior/senior identity hitting an admin API route directly gets a 403, not a 404 or a silent success; a junior/senior identity navigating to an admin URL directly gets redirected, not shown a broken page.

### 5.2 Tenant Management (AP-02)
- **New** endpoints; the `Tenant` table itself already has everything v1 needs: `id`, `name`, `vertical`, `junior_premium_cap` (nullable per-tenant override of the global setting — exists in the schema today with **zero** current readers/writers).
- `GET /api/core/admin/tenant` — the admin's own tenant detail (id, name, vertical, junior_premium_cap).
- `PATCH /api/core/admin/tenant` — update `name` and/or `junior_premium_cap`. Setting `junior_premium_cap` to a real value here is the FIRST real consumer of that column.
- Acceptance: setting a tenant-level cap of, say, $75,000 causes a junior at that tenant to be blocked/escalated on a $100,000 approval on their very next real review-queue action, with no code change (this exercises `can_approve`'s existing but never-yet-triggered per-tenant-cap branch — confirm during implementation whether that branch already reads `Tenant.junior_premium_cap` or whether it needs to; note this as a build-time check, not an assumption).

### 5.3 User Management (AP-03)
- **New** endpoints. The `User` table has `id`, `tenant_id`, `email`, `name`, `role`, `created_at` — **no active/disabled status field exists today.** If v1 wants "deactivate a user" (not listed as required above), that's a new column, called out explicitly so it isn't assumed silently.
- `GET /api/core/admin/users` — every user for the admin's tenant.
- `POST /api/core/admin/users` — create a user (email, name, role) — reuses the exact lookup shape `core/auth/router.py`'s login already expects, so a newly-created user can log in immediately with no other change.
- `PATCH /api/core/admin/users/{user_id}` — change `role` (and/or `name`).
- Acceptance: promoting a junior to senior via this endpoint, then having that person log out and back in, changes their real authority on the next senior-only action (OVERRIDE/SEND/ISSUE) — proving the role change round-trips through the real login + header-stub flow, not just the DB row.

### 5.4 Platform Settings (AP-04)
The six settings below are **real, currently-working, currently env-only** tunables — nothing here is invented; each is already commented in `core/config.py` as intentionally configurable:

| Setting | Current default | Used by |
|---|---|---|
| `junior_premium_cap` | 150,000 | Global fallback approval cap (see AP-02 for the per-tenant override) |
| `connectors_mode` | `"mock"` | Whether every workflow's live-inbox/live-run paths use fixtures or the real Nango connectors |
| `nango_inbox_query` | `"in:inbox newer_than:30d subject:submission"` | Market Matching's live-inbox Gmail search |
| `quote_rank_price_weight` / `quote_rank_subjectivity_penalty` | `1.0` / `0.0` | Quote Comparison's recommendation ranking |
| `carrier_appetite_min_total_outcomes` | `3` | Carrier Appetite Intelligence's signal-suppression threshold |

- **New**, both an endpoint and a storage decision (see §7 — recommend a small DB-backed override table rather than rewriting `.env` at runtime).
- `GET /api/core/admin/settings` — current effective value of each of the six, and whether it's the env default or an admin override.
- `PATCH /api/core/admin/settings` — set/clear an override for one or more of the six.
- Acceptance: changing `carrier_appetite_min_total_outcomes` from 3 to 5 via this endpoint changes Carrier Appetite Intelligence's real suppression behavior on its very next run, with no restart — this is the same behavior already proven manually earlier in this project by editing `.env` and restarting; v1 just makes that a live, no-restart admin action.

### 5.5 Integrations Oversight (AP-05)
- **Mostly reuse.** `GET /api/core/integrations/connections` (and connect-session/confirm/disconnect) already exist, are already tenant-scoped via `ctx.tenant_id`, and need no backend change.
- New: an admin-context presentation of the same data — likely the literal same panel component already built, rendered inside the new Admin section instead of (or in addition to) today's per-user Settings page.
- Acceptance: no regression to the existing connect/disconnect flow anywhere it's used today (Settings page).

### 5.6 Cross-Workflow Activity / Audit Log (AP-06)
- **New endpoint**, reusing an **existing but currently unexposed** service. `core/audit/service.py`'s `AuditService.query` already supports filtering by workflow/actor within one tenant — it is a real, working, tested method with **zero HTTP routes calling it today**.
- `GET /api/core/admin/audit?workflow=&actor=&limit=` — thin wrapper over the existing `query` method.
- Acceptance: an action taken in ANY of the 10 workflows (e.g. Binder Issuance's "escalate," Quote Comparison's "select") shows up in this admin view without the admin having had to visit that workflow's own page.

### 5.7 Real Organization/Team Panels (AP-07)
- Replace `Foundation.tsx`'s hardcoded `Organization` panel (currently 4 fake static fields) with real `Tenant` data from AP-02.
- Replace the hardcoded `Team` panel (currently 4 made-up names/titles) with the real user list from AP-03.
- These can either stay on the existing Settings page (now real) or move under the new Admin section — a UX call, not a data call; either way the data source changes from "hardcoded" to "real," which is the actual requirement.

### 5.8 Non-Functional Requirements
- Every new admin endpoint enforces `require_role(Role.ADMIN)` server-side — never trust a frontend-only guard (matches this codebase's existing, consistent principle that authority is enforced server-side, demonstrated everywhere in the 10 workflows already).
- No new endpoint here ever returns another tenant's data — every query stays scoped to `ctx.tenant_id`, matching the existing Integrations pattern exactly (see §9 for why true cross-tenant admin is explicitly a bigger, separate decision).
- Settings overrides (§5.4) must be readable by the *exact same* `get_settings()`/`Settings` object every workflow already calls — no workflow should need its own special-cased "check for an override" logic; the override should be transparent underneath the existing settings access pattern (see §7).

---

## 6. Authorization Model

No new role is introduced — `Role.ADMIN` already exists in `core/common/enums.py` and is already treated as "senior-or-above" everywhere authority is checked today (`can_approve`, the review queue's `_SENIOR_ACTIONS` gate). This PRD's only authorization work is:

1. Apply the existing `require_role(Role.ADMIN)` dependency to every new endpoint in §5.2–§5.6 (today it's a built utility with exactly one real usage anywhere in the codebase — this PRD is what actually exercises it).
2. Build the **frontend's first-ever** role-conditional UI: hide the Admin nav section and guard its routes for anyone whose `identity.role !== "admin"`.
3. Seed at least one real admin user (`core/seed.py` currently seeds none) — a prerequisite, called out again here because it blocks *every* acceptance test above.

---

## 7. Data Schema Changes

| Table | Change | Why |
|---|---|---|
| `Tenant` | **None needed** — `junior_premium_cap` already exists, unused | AP-02 is this column's first real consumer |
| `User` | **None required for v1's listed scope.** A `is_active: bool` column would be needed *only if* user deactivation is wanted — flagged, not assumed, since it's not in this PRD's stated scope | Avoids silently expanding scope |
| *(new)* `PlatformSetting` | **New table**: `id`, `tenant_id`, `key`, `value` (string; caller casts), `updated_at`, `updated_by` | Recommended storage for AP-04's overrides. Alternative considered: writing directly to `.env` at runtime — rejected, since it's process-wide (not tenant-scoped), requires file-system write access in prod, and doesn't survive a redeploy cleanly. A DB-backed override table, checked first inside `get_settings()`/a thin wrapper around it, keeps every existing call site (`get_settings().carrier_appetite_min_total_outcomes`, etc.) working unchanged while making the value live-editable. |

---

## 8. System Architecture (Level 2)

**Backend** — one new router, `core/admin/router.py`, mounted at `/api/core/admin` (same mounting pattern as `core/auth/router.py`/`core/integrations/router.py` in `main.py`), every route behind `require_role(Role.ADMIN)`:
- `GET/PATCH /tenant`
- `GET/POST /users`, `PATCH /users/{user_id}`
- `GET/PATCH /settings`
- `GET /audit`

New service-layer piece: a small settings-override lookup (backing the new `PlatformSetting` table) that `get_settings()` or its callers consult — the exact integration point (wrap `get_settings()` itself vs. a separate `get_effective_setting(key)` helper used only by the 6 tunables) is an implementation decision for engineering, not fixed by this PRD.

**Frontend** — new routes under `Insurance OS/src/routes/` (e.g. `app.admin.tsx` as a layout + child routes, following the same `app.foundation.$slug.tsx`-style pattern already used for grouped sub-pages), a new `lib/api/admin.ts` client (same shape as every existing `lib/api/*.ts` file), and one new `AppShell.tsx` nav entry, conditionally rendered on `identity.role === "admin"`.

---

## 9. Risks & Open Questions

- **Biggest open question — is "cross-tenant" ever really needed?** The current login/`Ctx` model resolves to exactly *one* `tenant_id` per logged-in identity (via the real email lookup). An admin, as designed here, manages **their own tenant only** — this matches every existing endpoint's scoping (Integrations, etc.) with zero new auth work. A true "platform super-admin who can see/manage every tenant, including ones they didn't log into" is a materially bigger change (a new auth concept, since nothing today lets one identity address a different tenant's data) and is **explicitly out of scope for v1** unless you tell me otherwise before implementation starts.
- **No admin user exists today.** `core/seed.py` must be extended with a real admin-role user before any of this can be tested end-to-end — small, but a real prerequisite, not a v1 line item to skip.
- **Settings storage decision (§7)** needs a yes/no before implementation: DB-backed override table (recommended) vs. some other approach.
- **Whether `Tenant.junior_premium_cap` is actually read anywhere yet** needs a direct code check at implementation time — this PRD assumes it exists on the schema (confirmed) but does not assume `can_approve`'s current logic already branches on it; if it doesn't, that's a small addition inside AP-02's acceptance work, not a separate PRD.
- **Whether `senior` gets any read-only visibility** into settings/audit (vs. `admin`-only) is a product call not yet made — v1 as scoped here is admin-only for the entire section.

---

## 10. Rollout Plan

**Phase 1 (read-first):** AP-05 (integrations, pure reuse) and AP-06 (audit viewer) — lowest risk, no write paths, proves the Admin nav/route-guard pattern (AP-01) end-to-end before anything mutates data.

**Phase 2 (management):** AP-02 (tenant) and AP-03 (users/roles) — real CRUD, needs the admin-user-seeding prerequisite resolved first (§9).

**Phase 3 (settings):** AP-04 — depends on the §7 storage decision being made; also replace the fake panels (AP-07) once real tenant/user data is available from Phase 2.
