# Available tools for `unity autoformalize` — semantic chunking

Start with `autoformalize_brief(author)` and `.unity/formalization-plan.json` for the immutable supplied-source
snapshot, file paths/artifacts and exact source-reference IDs. Read source files directly as appropriate
to their format; use `artifact_info` and bounded `artifact_read` for stored text. `autoformalize_status()` exposes
the current state; `forum_post` and `forum_read` provide necessary clarification.

Write `.unity/dag.json`, including `requirements`, and the Lean statement scaffold in project files.
Copy the plan's `solution_candidate` and `solution_sha256` compatibility fields exactly: they identify
the supplied-source snapshot and bundle hash, not a generated solution or an informal review result.
Unity builds/freezes the scaffold before formalization. Do not edit the supplied source files.

Use prior candidate-bound chunking failures to avoid repeating unchanged unsuccessful searches.
Report unreadable or unsupported source passages with exact locations; do not invent replacement text.
