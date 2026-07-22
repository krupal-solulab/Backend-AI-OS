# Connectors — Gmail / Sheets / Drive via Nango

All external Google/Gmail access goes through **Nango**. Workflow code never imports Google SDKs directly — it calls the shared `ConnectorService` (in `core/ingestion`). This keeps auth, tokens, and rate-limits in one place and swappable.

## What we use it for
| Capability | Provider (Nango integration) | Used by |
|---|---|---|
| Read submission mailbox + **fetch attachments** | `google-mail` | Ingestion → Submission Triage, Renewal, Endorsement, Claims |
| **Send** broker/agent emails (on human approve) | `google-mail` | Broker/Agent Communication, requests, notices |
| Detect **replies** (for no-response follow-up) | `google-mail` | Broker/Agent Communication |
| **Sheets** read/write (write-back fallback when no PAS) | `google-sheet` | Bind, Bordereau, Renewal write-back |
| **Drive** (store/fetch documents where used) | `google-drive` | Documents |

## Model
- **One Nango `Connection` per tenant per provider.** We store the Nango `connectionId` on our `Connection` table (tenant-scoped). Tokens live in Nango, not our DB.
- The FE "Integrations" section triggers the Nango OAuth flow; the backend just uses the resulting connection.

## `ConnectorService` interface (in `core/ingestion`)
```ts
interface ConnectorService {
  // Gmail
  fetchInbox(conn, sinceCursor?): Promise<EmailMsg[]>;      // new submission emails
  getAttachments(conn, messageId): Promise<FileBlob[]>;     // → documents module
  sendEmail(conn, { to, subject, body, threadId? }): Promise<SentRef>; // only after human approve
  getThreadReplies(conn, threadId): Promise<EmailMsg[]>;    // reply detection
  // Sheets / Drive
  appendRows(conn, sheetId, rows): Promise<void>;           // write-back fallback
  readRange(conn, sheetId, range): Promise<Row[]>;
  putFile(conn, folderId, blob): Promise<DriveRef>;
}
```
Under the hood each method calls the Nango SDK / proxy for the right integration. Swap providers later without touching workflows.

## Ingestion flow (async)
```
Nango webhook / poll (google-mail) ─► jobs queue
  worker: fetchInbox → new email? → getAttachments → documents.save()
        → create Submission → enqueue extraction job
Failure → error queue (visible), never dropped.
```

## Hard rules
- **No auto-send.** `sendEmail` is only ever called from a human-triggered action (approve & send). There must be **no code path** that sends without a preceding user action (esp. non-renewal notices).
- **Nango only.** No `googleapis` calls inside workflows or the decision cores.
- **Tenant-scoped.** Every connector call carries the tenant's connection; never a global credential.

## Env
```
NANGO_SECRET_KEY=...
NANGO_HOST=https://api.nango.dev        # or self-hosted
NANGO_INTEGRATION_MAIL=google-mail
NANGO_INTEGRATION_SHEET=google-sheet
NANGO_INTEGRATION_DRIVE=google-drive
```
For local dev without live Google, run `ConnectorService` in **mock mode** backed by the fixtures (see DATA_AND_FIXTURES.md) so ingestion works offline.

## Build offline → go live (per workflow, no code change)
Ingestion is the **only** thing that differs between test-data and live. Everything after it (extraction → rules → decision → LLM → review → audit) is identical. So each workflow ships in two steps:

1. **Prove on fixtures** — `CONNECTORS_MODE=mock`. `ConnectorService` serves emails + attachments from `Workflow_<N>/test_dataset`. Build the pipeline and pass the eval against `Validation_Rules_Test_Dataset.md`.
2. **Flip to live** — `CONNECTORS_MODE=live` + connect the tenant's Gmail via Nango. The **same pipeline** now runs on real broker mail. **No workflow code changes** — only the env flag + a Nango connection.

```
mock:  fixtures ──► [ extract → rules → decide → draft → review → audit ]
live:  Nango Gmail ─►[ ......... identical ......... ]
```
Because the two paths share one `ConnectorService` interface, "works on test data" → "works on live mail" is a config switch, not a rewrite. Do this workflow-by-workflow: prove Submission Triage on `Workflow_1`, then take *that* workflow live before moving on.
