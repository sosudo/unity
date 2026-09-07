# Available tools for `unity autoformalize` — optional source repair

Use `autoformalize_brief(author)` for the current issue/repair state and `autoformalize_task(task_id)`
for targeted source and prerequisite detail. Read the original sources listed in the plan; use
`artifact_info` and bounded `artifact_read` for detailed stored evidence.

- `submit_source_repair(author, issue_id, explanation, evidence, replacement?)` records an immutable,
  source-bound proposal. State exactly what changes, if anything, and cite evidence.
- `report_source_issue(author, anchor_ids, description, task_ids?)` records a newly discovered defect.
- Findings, obstacles, questions/answers and free-form Forum notes support collaboration.

No formal candidate finalization or source rewriting belongs to this role. Keep detailed search output
in artifacts, and leave existing worktree proof files unchanged.
