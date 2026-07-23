# Workflow Template — build ANY workflow the same way

Every workflow follows the same 7-step pipeline and the same package shape. Copy this recipe; you never invent new plumbing.

## The pipeline interface (in `core/common`, a Protocol)
```python
class WorkflowPipeline(Protocol[Out]):
    async def ingest(self, ctx: Ctx, inp: WorkflowInput) -> RawBundle: ...          # Nango email/docs or a record id
    async def extract(self, ctx: Ctx, raw: RawBundle) -> ExtractedModel: ...         # via ExtractionService (shared)
    async def validate(self, ctx: Ctx, data: ExtractedModel) -> list[RuleResult]: ...# via RulesEngine (validation rule sets)
    async def decide(self, ctx: Ctx, data: ExtractedModel) -> Decision: ...          # vertical Decision Core
    async def draft(self, ctx: Ctx, decision: Decision) -> Draft: ...                # via LLMService (grounded, cited)
    async def package(self, ctx: Ctx, *args) -> Out: ...                             # typed OutputPackage (Pydantic)
    # steps 8+ (queue → human → write-back → audit) handled by shared services
```
Most steps just **call shared services**. The only real per-workflow code is `decide` (which rule sets to run + how to map results) and `package` (its output shape).

## Package to create
```
verticals/<vertical>/workflows/<workflow>/
  __init__.py
  router.py     # routes under /api/<vertical>/<workflow>
  service.py    # implements WorkflowPipeline
  schema.py     # Pydantic models for this workflow's output
  eval_test.py  # runs against fixtures (Definition of Done) — pytest
```

## Steps to build
1. **Scaffold** the package from the shape above.
2. **Define the output schema** (`schema.py`, Pydantic) matching the FE screen's data needs.
3. **Implement the pipeline service** — inject the shared services; write only `decide()` + `package()`, reuse the rest.
4. **Wire rule sets** — reference the validation rule sets + (MGA) appetite / (E&S) matching config via the RulesEngine. Author rules in the FE Rules Console; load fixtures' `Validation_Rules_*.md` for tests.
5. **Expose the router** — `GET /api/<vertical>/<workflow>` list, `GET /{id}` detail, action endpoints (`POST /{id}/approve|override|escalate|send|issue`) → all call `ReviewQueueService` + `AuditService`.
6. **Register** in your vertical router (`include_router(...)`, one line).
7. **Load fixtures** — point at `Workflow_<N>/test_dataset` (DATA_AND_FIXTURES.md); the loader turns each `submission_XX/` into a `Submission` + `list[Document]`.
8. **Write the eval** — assert recommendations/flags match the expected outcomes in the dataset's rules doc. This is your Definition of Done.

## Human-action + audit (shared — you just call it)
```python
await review_queue.enqueue(output_package)
# on a human action from the FE:
await review_queue.act(item_id, action, user)     # enforces RBAC / authority limit
await audit.record(AuditEntry(actor="human", who=who, what=what, workflow=wf, ctx=ctx))  # feeds Governance & Portfolio
```
Role gating (junior authority cap, escalate, senior-only issue) is enforced by `auth` + `review_queue` — don't re-implement it per workflow.

## Definition of Done for a workflow
- [ ] Pipeline runs end-to-end on its `Workflow_<N>` fixtures.
- [ ] Output schema matches the FE screen.
- [ ] Validation + decision results match the dataset's expected outcomes (`eval_test.py` passes).
- [ ] Human actions enforce RBAC and write to the audit log.
- [ ] No files edited outside your workflow package + your vertical router.
