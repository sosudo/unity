# Available tools for `unity solve` — semantic chunking

This profile is read-only. Start with `solve_brief(author)` and inspect the exact accepted solution with
`artifact_info` and bounded `artifact_read`. Read `.unity/formalization-plan.json` for the exact candidate
and component coverage contract. `solve_status()` exposes exact gate state; `forum_post` and
`forum_read` are available for necessary clarification. Write the requested DAG directly to
`.unity/dag.json`; no Forum mutation is required.

The brief includes prior candidate-bound chunking failures. Avoid repeating an unsuccessful search from
those attempts unless you have a materially different query or reason to expect a different result.
