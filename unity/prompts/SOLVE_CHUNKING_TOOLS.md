# Available coordination tools for `unity solve` — chunking profile

Chunking derives the formalization work graph from the exact accepted natural-language solution. Start
with `solve_brief(author)` and use bounded `solve_status()` or `artifact_read(...)` only for detail the
brief omits. The header identifies the original problem, accepted solution candidate, artifact hash,
and current gate revisions.

This profile is read/discussion-only: only the common brief, status, discussion, and artifact tools are
exposed. The server infers the active gate; none of these tools accepts a model-selected stage. Do not
reopen solving, edit candidate state, or invent task ownership while deriving the graph.
