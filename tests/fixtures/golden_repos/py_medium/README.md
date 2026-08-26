# py_medium

A synthetic ~12-module Python project used as a golden-repo fixture for
Phase 3 manual Project Profile review (docs/plan.md Phase 3 exit
criteria): "at least one test repo with enough modules to force the
hierarchical reduce path, not only small repos where a single flat
reduce suffices."

Each top-level directory is its own module (matching
SampledFile.module directory-stratified grouping from Phase 2), so this
fixture produces more module-level summaries than MAP_REDUCE_BATCH_SIZEs
default of 8 -- enough to force
docs/system-design/06-cli-contract-and-profile-scaling.md section 6.2s
batch-and-recurse path even before token size is considered.
