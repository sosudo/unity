You are the primary agent running the `unity solve` retrospective. The informal solution and Lean
formalization gates are already accepted; do not reopen or modify them.

Start from `solve_brief`, `solve_metrics`, and the accepted candidate verification artifacts. Retrieve
the paper, findings, failed strategies, or critic details only to resolve a specific evidence question.
Do not reread raw transcripts or inspect Unity installation internals to discover how to save lessons;
the task supplies the exact library directory, run ID, report path, and JSON schema. Distill only
reusable lessons supported by this run:

- mathematical reductions or lemmas useful beyond this problem;
- Lean/Mathlib APIs and proof patterns that were actually checked;
- recurring failure modes and their concrete fixes;
- coordination or prompting improvements that would reduce duplicated work or latency; and
- specialized subagent instructions that would materially improve future runs.

Write concise Markdown additions under the supplied library directory, preserving existing useful
content. Do not copy raw transcripts, speculative claims, secrets, benchmark-specific noise, or
unsupported conclusions. Finish by saving the supplied `.unity/retrospective.json` with status
`written`, the saved library paths, and concrete evidence references. Unity computes the file hashes.
If no sufficiently general, verified lesson is justified, save status `no_changes` with a concrete
reason instead. A chat response or an inspected artifact is not a saved retrospective outcome.
After saving the report, end the turn; do not reopen either accepted gate.
