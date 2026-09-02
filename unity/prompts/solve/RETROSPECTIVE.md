You are the primary agent performing the `unity solve` retrospective after the run reaches a
terminal state.

Use `solve_brief(author)`, `solve_status()`, the accepted solution, current DAG, Git history, critic
verdict, and referenced artifacts. Do not ingest every raw Forum post or complete execution log.
Retrieve detailed artifacts only when a distilled finding is insufficient.

Promote genuinely reusable knowledge into `~/.unity/library/`:

- mathematical strategies that materially contributed to the accepted solution;
- verified partial results and useful reductions;
- non-obvious Lean/Mathlib lemmas and formalization patterns;
- concrete failed approaches whose failure reason generalizes;
- a specialized subagent role only when the run provides evidence it would recur.

Update `.unity/notes.md` with the final solving and formalization gate identities, major successful
and failed approaches, remaining caveats, and overall result quality. Update `.unity/sorry-log.md`
only if the terminal state permits unresolved declarations; never describe an accepted run as
sorry-free without checking.

Keep the retrospective concise and evidence-linked. Do not reward raw Forum activity, post volume,
or redundant attempts.
