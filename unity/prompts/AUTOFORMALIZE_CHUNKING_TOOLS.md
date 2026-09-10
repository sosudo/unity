# Available tools for `unity autoformalize` — semantic chunking

Start with `autoformalize_brief(author)` and `.unity/formalization-plan.json` for the immutable supplied-source
snapshot, file paths/artifacts and exact source-reference IDs. Read source files directly as appropriate
to their format; use `artifact_info` and bounded `artifact_read` for stored text. `autoformalize_status()` exposes
the current state; `forum_post` and `forum_read` provide necessary clarification.

Write only `.unity/dag.json`: an informal, source-linked DAG with stable node IDs, titles,
`predicted_kind`, `informal_statement`, nullable `informal_proof`, `statement_dependencies`,
`proof_dependencies`, source/anchor/requirement references, and optional proposed formal hints.
Retain anchored `requirements` and `spec` (scope, arguments and prerequisites). Unresolved library
matches are allowed in the plan. Do not generate Lean files or a compilable scaffold, run builds,
or perform proof search/import minimization for chunking.
Preserve abbreviated source arguments and cross-references in `informal_proof`. Use null only for an
absent source argument; null is provenance, not unprovability or permission to leave final proof holes.
Copy the plan's `solution_candidate` and `solution_sha256` compatibility fields exactly: they identify
the supplied-source snapshot and bundle hash, not a generated solution or an informal review result.
Unity validates source links and the dependency graph before formalization. Formalizers create and
submit versioned Lean outputs later. Do not edit the supplied source files or silently drop obligations.

After saving `dag.json`, call `validate_chunks()`. Correct reported errors in the existing draft and
validate again before ending the current attempt. Preserve source obligations and node IDs; do not
restart source analysis for a schema correction. This is read-only bookkeeping validation, not Lean
compilation, adoption or mathematical faithfulness approval.

Use prior candidate-bound chunking failures to avoid repeating unchanged unsuccessful searches.
`report_source_issue(author, anchor_ids, description, task_ids?)` records source gaps with exact locations.
Before freezing, use the plan's source-reference IDs as anchors. `submit_source_repair(author, issue_id,
explanation, evidence, replacement?)` records an explicit justified correction without changing source bytes.
Inspect existing proposals in the plan; adopted repairs must appear in `spec.arguments[].repair_ids`.
Unity schedules optional repair attempts for open issues rather than silently dropping their requirements.
