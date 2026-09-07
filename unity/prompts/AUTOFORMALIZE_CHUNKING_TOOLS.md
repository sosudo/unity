# Available tools for `unity autoformalize` — semantic chunking

Start with `autoformalize_brief(author)` and `.unity/formalization-plan.json` for the immutable supplied-source
snapshot, file paths/artifacts and exact source-reference IDs. Read source files directly as appropriate
to their format; use `artifact_info` and bounded `artifact_read` for stored text. `autoformalize_status()` exposes
the current state; `forum_post` and `forum_read` provide necessary clarification.

Write `.unity/dag.json`, including anchored `requirements` and `spec` (scope, arguments and resolved
prerequisites), and the Lean statement scaffold in project files.
Copy the plan's `solution_candidate` and `solution_sha256` compatibility fields exactly: they identify
the supplied-source snapshot and bundle hash, not a generated solution or an informal review result.
Unity builds/freezes the scaffold before formalization. Do not edit the supplied source files.

Use prior candidate-bound chunking failures to avoid repeating unchanged unsuccessful searches.
`report_source_issue(author, anchor_ids, description, task_ids?)` records source gaps with exact locations.
Before freezing, use the plan's source-reference IDs as anchors. `submit_source_repair(author, issue_id,
explanation, evidence, replacement?)` records an explicit justified correction without changing source bytes.
Inspect existing proposals in the plan; adopted repairs must appear in `spec.arguments[].repair_ids`.
Unity schedules optional repair attempts for open issues rather than silently dropping their requirements.
