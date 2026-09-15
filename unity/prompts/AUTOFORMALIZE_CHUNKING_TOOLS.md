# Available tools for `unity autoformalize` — semantic chunking

Start with `autoformalize_brief(author)` and the absolute formalization-plan path in your task for the immutable supplied-source
snapshot, file paths/artifacts and exact source-reference IDs. Read source files directly as appropriate
to their format; use `artifact_info` and bounded `artifact_read` for stored text. `autoformalize_status()` exposes
the current state; `forum_read` provides discussion detail.

- `publish_finding(author, kind, title, content, confidence, target?, strategy_id?, evidence?, supersedes?)`
  shares reusable source facts and concrete failures with evidence. `confidence` is an integer from
  0 to 100: use `95`, not `0.95`. `kind` is an agent-chosen string, not a fixed enum. Reuse existing
  findings; use `supersedes` when new evidence replaces an active finding.
- `report_obstacle(author, goal_state, target?, tried?, hypothesis?)` records a concrete blocker.
- `ask_question(author, body, to?, target?)` asks for help; `answer_question(question_id, author, body)`
  answers an existing question.
- `forum_post(thread_id, author, content, reply_to?)` posts free-form discussion; `reply_to` is an
  optional list of post IDs. A post does not publish or accept your draft.

Write only the assigned draft: an informal, source-linked DAG with stable node IDs, titles,
`predicted_kind`, `informal_statement`, nullable `informal_proof`, `statement_dependencies`,
`proof_dependencies`, source/anchor/requirement references, and optional proposed formal hints.
Initially use one node per in-scope source definition, theorem, lemma, corollary or construction,
keeping its statement and proof together. Do not pre-decompose routine proof steps or prospective
Lean helpers; formalizers can add needed helpers and edges later with `refine_chunks`.
Initial plans include anchored `requirements` and `spec` (scope, arguments and prerequisites).
Replans use the seeded mutable-only draft: `base_revision`, `requirement_tasks`, `prerequisites`,
`arguments`, `chunks`, and unchanged source-binding fields. Unity supplies frozen obligations.
Use `kind="declaration"` for a known proposed witness (external or project-local), `kind="argument"`
with an explicit rationale for inline discharge/target attribution, or `kind="task"` for separate work.
A citation of the target itself is not an additional prerequisite. Unknown matches may remain unresolved.
Do not generate Lean files or a compilable scaffold, run builds,
or perform proof search/import minimization for chunking.
Copy the plan's `solution_candidate` and `solution_sha256` compatibility fields exactly: they identify
the supplied-source snapshot and bundle hash, not a generated solution or an informal review result.
Unity validates source links and the dependency graph before formalization. Formalizers create and
submit versioned Lean outputs later. Do not edit the supplied source files or silently drop obligations.

`validate_chunks()` reads your assigned draft and returns precise validation diagnostics without
publishing it. Correct and revalidate in this same session. Ordinary corrections do not count as failed
executions. The controller alone accepts the draft and records success. Only this phase's tools are
available; do not import internal Unity helpers or directly edit shared state/attempt records.
For shell tool access use `unity mcp unity-forum <tool> '<JSON arguments>'` from the project checkout.

Use prior candidate-bound chunking failures to avoid repeating unchanged unsuccessful searches.
`report_source_issue(author, anchor_ids, description, task_ids?)` records source gaps with exact locations.
Before freezing, use the plan's source-reference IDs as anchors. `submit_source_repair(author, issue_id,
explanation, evidence, replacement?)` records an explicit justified correction without changing source bytes.
Inspect existing proposals in the plan; adopted repairs must appear in `spec.arguments[].repair_ids`
for initial chunking, or `arguments[].repair_ids` in a mutable-only replan.
Unity schedules optional repair attempts for open issues rather than silently dropping their requirements.
