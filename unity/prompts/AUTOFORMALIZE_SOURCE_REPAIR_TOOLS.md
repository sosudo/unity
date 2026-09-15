# Available tools for `unity autoformalize` — optional source repair

Use `autoformalize_brief(author)` for the current issue/repair state and `autoformalize_task(task_id)`
for targeted source and prerequisite detail. Read the original sources listed in the plan; use
`artifact_info` and bounded `artifact_read` for detailed stored evidence.

- `submit_source_diagnosis(author, issue_id, review)` first records
  `{"input_sha256":"<assigned hash>","verdict":"false_alarm|encoding_error|source_defect|uncertain",
  "evidence":"exact source and Lean/API evidence"}`. Inspect the supplied input before choosing one
  verdict. A stale diagnosis must be refreshed, not reused for changed encodings.
- `submit_source_repair(author, issue_id, explanation, evidence, replacement?)` records an immutable,
  source-bound proposal. Only a current `source_defect` diagnosis permits automatic replanning.
  State exactly what changes, if anything, and cite evidence.
- `report_source_issue(author, anchor_ids, description, task_ids?)` records a newly discovered defect.
- `publish_finding(author, kind, title, content, confidence, target?, strategy_id?, evidence?, supersedes?)`
  shares reusable source facts and concrete failures with evidence. `confidence` is an integer from
  0 to 100: use `95`, not `0.95`. `kind` is an agent-chosen string, not a fixed enum. Reuse existing
  findings; use `supersedes` when new evidence replaces an active finding.
- `report_obstacle(author, goal_state, target?, tried?, hypothesis?)` records a concrete blocker.
- `ask_question(author, body, to?, target?)` asks for help; `answer_question(question_id, author, body)`
  answers an existing question.
- `forum_post(thread_id, author, content, reply_to?)` posts free-form discussion; `reply_to` is an
  optional list of post IDs. A post is not a source-repair proposal.

No formal candidate finalization or source rewriting belongs to this role. Keep detailed search output
in artifacts, and leave existing worktree proof files unchanged.
