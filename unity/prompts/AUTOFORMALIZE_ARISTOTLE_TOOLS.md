## Aristotle — enabled

Use Aristotle for a difficult Lean target or subgoal when helpful.
It is asynchronous: submit bounded work and continue useful work.

- `aristotle_submit(prompt, project_dir?)` — submit a proving job; returns its project ID.
- `aristotle_status(project_id)` — inspect project and task status.
- `aristotle_wait(project_id, timeout_seconds?, poll_seconds?)` — wait for a bounded interval;
  prefer continuing other useful work while a long job runs.
- `aristotle_result(project_id, destination?)` — download completed results to a private destination.
- `aristotle_cancel(project_id)` — cancel obsolete jobs.
- `aristotle_list(limit?)` — list recent jobs when an existing job needs to be located.

Use native MCP when exposed; otherwise, for example:

```sh
unity mcp aristotle aristotle_submit '{"prompt":"<exact Lean goal, required definitions/imports, and project toolchain>"}'
unity mcp aristotle aristotle_status '{"project_id":"<returned ID>"}'
unity mcp aristotle aristotle_result '{"project_id":"<returned ID>","destination":"<private destination>"}'
```

Publish the job ID and target in a finding so teammates can reuse it
instead of submitting duplicate jobs. Poll when useful, not continuously.

Include the exact target, needed definitions/imports, and project toolchain. If supplying
`project_dir`, use a sanitized Lean-only context directory, not a checkout containing credentials
or `.unity` runtime state. Inspect returned files and incorporate only relevant proof changes in
your worktree. Preserve protected statements and definitions, and validate locally with targeted
diagnostics before finalizing. Do not duplicate Unity's authoritative full main build.

Do not start proof-writing jobs during chunking or independent review.
Using Aristotle is optional, not a prerequisite for completing a task.
