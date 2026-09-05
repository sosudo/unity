# Available tools for `unity solve` — retrospective

The solve-state tools are read-only in this phase. Use `solve_brief(author)`, `solve_metrics()`,
`artifact_info`, and `artifact_read` to retrieve only the evidence needed for durable lessons.
`solve_status()`, `forum_post`, and `forum_read` remain available when specific detail is needed.
Do not mutate gates, strategies, candidates, tasks, or project sources.

Use normal file-writing tools or shell commands to save Markdown lessons under the exact library
directory supplied in your task and the required `.unity/retrospective.json` outcome. These writes
are permitted; read-only refers to the accepted solve state, not the retrospective deliverables.
