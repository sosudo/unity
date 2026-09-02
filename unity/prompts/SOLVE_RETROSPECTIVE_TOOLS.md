# Available coordination tools for `unity solve` — retrospective profile

Retrospective analyzes the completed run without changing its proof state. Start with
`solve_brief(author)` and use bounded `solve_status()`, `artifact_info(...)`, or
`artifact_read(...)` for exact evidence. Use `forum_post(...)` and `forum_read(...)` only when a
specific discussion detail is necessary.

This profile is read/discussion-only and exposes no gate, strategy, candidate, or verdict mutations.
The server infers the completed run state; none of these tools accepts a model-selected stage. Record
improvements in the retrospective output requested by the phase prompt, not by reopening the run.
