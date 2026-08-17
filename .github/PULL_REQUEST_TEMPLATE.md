## Summary

<!-- What does this PR do, in 1-3 sentences? -->

## Which phase / requirement does this belong to?

<!-- e.g. "docs/plan.md Phase 0 — Walking Skeleton", or "FR17" -->
<!-- If this doesn't map to an existing phase/FR/NFR, explain why it's needed
     now rather than as part of the phase that would normally own it. -->

## Changes

<!-- Bullet list of what changed. Call out any deviation from
     docs/design.md or docs/requirements.md explicitly — either this PR
     also updates the relevant doc, or it should not deviate. -->

-
-

## Testing

<!-- Required. A PR without this filled in will be sent back before review. -->

- [ ] `pytest -q` passes locally
- [ ] Manually exercised the changed behavior (describe how below)
- [ ] If this touches the LLM client or evaluation schema: manually reviewed
      at least one real Ollama response for *groundedness*, not just schema
      validity (schema-valid-but-hallucinated output is a failure, per
      `docs/plan.md` Phase 0 exit criteria)
- [ ] If this touches the timer: confirmed LLM/eval latency is excluded from
      the answer clock, not just that the code compiles

**How I tested it:**

<!-- Commands run, repos used, sample output, screenshots, etc. -->

## Checklist

- [ ] I read `CONTRIBUTING.md`
- [ ] This PR is scoped to one phase/concern
- [ ] New/changed behavior has test coverage
- [ ] Docs (`docs/design.md`, `docs/requirements.md`, `docs/plan.md`,
      `README.md`) are updated if this changes what they describe
- [ ] No network calls were added to the test suite without mocking
