## Aristotle — enabled

Use Aristotle for a difficult Lean target or subgoal when helpful.
It is asynchronous: submit bounded work and continue useful work.

```sh
unity mcp aristotle aristotle_submit '{"prompt":"<exact Lean goal, required definitions/imports, and project toolchain>"}'
unity mcp aristotle aristotle_status '{"project_id":"<returned ID>"}'
unity mcp aristotle aristotle_result '{"project_id":"<returned ID>","destination":"<private destination>"}'
```

Publish the job ID and target in a finding so teammates can reuse it
instead of submitting duplicate jobs. Poll when useful, not continuously.
`aristotle_cancel(project_id)` cancels obsolete jobs.

Inspect returned files and incorporate only relevant proof changes in
your worktree. Preserve protected statements and definitions, and validate
locally before finalizing. Never submit credentials or runtime state.

Do not start proof-writing jobs during chunking or independent review.
Using Aristotle is optional, not a prerequisite for completing a task.
