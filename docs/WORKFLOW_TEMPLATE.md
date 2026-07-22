# Workflow Template — build ANY workflow the same way

Every workflow follows the same 7-step pipeline and the same folder shape. Copy this recipe; you never invent new plumbing.

## The pipeline interface (in `core/common`)
```ts
export interface WorkflowPipeline<Out> {
  ingest(ctx: Ctx, input: WorkflowInput): Promise<RawBundle>;     // Nango email/docs or a record id
  extract(ctx: Ctx, raw: RawBundle): Promise<ExtractedModel>;     // via ExtractionService (shared)
  validate(ctx: Ctx, data: ExtractedModel): Promise<RuleResult[]>;// via RulesEngine (validation rule sets)
  decide(ctx: Ctx, data: ExtractedModel): Promise<Decision>;      // vertical Decision Core
  draft(ctx: Ctx, decision: Decision): Promise<Draft>;            // via LlmService (grounded, cited)
  package(ctx: Ctx, ...): Promise<Out>;                           // typed OutputPackage
  // step 8+ (queue → human → write-back → audit) handled by shared services
}
```
Most steps just **call shared services**. The only real per-workflow code is `decide` (which rule sets to run + how to map results) and `package` (its output shape).

## Folder to create
```
verticals/<vertical>/workflows/<workflow>/
  <workflow>.module.ts       # NestJS module
  <workflow>.controller.ts   # routes under /api/<vertical>/<workflow>
  <workflow>.service.ts      # implements WorkflowPipeline
  <workflow>.schema.ts       # Zod + TS types for this workflow's output
  <workflow>.eval.spec.ts    # runs against fixtures (Definition of Done)
```

## Steps to build
1. **Scaffold** the folder from the shape above.
2. **Define the output schema** (`*.schema.ts`) matching the FE screen's data needs.
3. **Implement the pipeline service** — inject the shared services; write only `decide()` + `package()`, reuse the rest.
4. **Wire rule sets** — reference the validation rule sets + (MGA) appetite / (E&S) matching config via the RulesEngine. Author rules in the FE Rules Console; load fixtures' `Validation_Rules_*.md` for tests.
5. **Expose the controller** — `GET /api/<vertical>/<workflow>` list, `GET /:id` detail, action endpoints (`POST /:id/approve|override|escalate|send|issue`) → all call `ReviewQueueService` + `AuditService`.
6. **Register** in your vertical module (one line).
7. **Load fixtures** — point at `Workflow_<N>/test_dataset` (DATA_AND_FIXTURES.md); the loader turns each `submission_XX/` into a `Submission` + `Document[]`.
8. **Write the eval** — assert recommendations/flags match the expected outcomes in the dataset's rules doc. This is your Definition of Done.

## Human-action + audit (shared — you just call it)
```ts
await reviewQueue.enqueue(outputPackage);
// on a human action from the FE:
await reviewQueue.act(itemId, action, user);      // enforces RBAC/authority limit
await audit.record({ actor:'human', who, what, workflow, ctx }); // feeds Governance & Portfolio
```
Role gating (junior authority cap, escalate, senior-only issue) is enforced by `auth` + `review-queue` — don't re-implement it per workflow.

## Definition of Done for a workflow
- [ ] Pipeline runs end-to-end on its `Workflow_<N>` fixtures.
- [ ] Output schema matches the FE screen.
- [ ] Validation + decision results match the dataset's expected outcomes (`*.eval.spec.ts` passes).
- [ ] Human actions enforce RBAC and write to the audit log.
- [ ] No files edited outside your workflow folder + your vertical module.
