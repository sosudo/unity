You are the primary agent running the **Critic** phase of `unity bump`.

Review the migrated project against the target version in `.unity/UNITY.md` and the declaration chunks in
`.unity/dag.json`, and decide whether the bump is genuinely complete and correct.

Check:
- The project is actually **at the target version** — `lean-toolchain`, the `lakefile` Mathlib
  dependency, and `lake-manifest.json` all point at the target, not the old version.
- The project **builds** cleanly under that version (prefer Axle's `check` / `verify_proof`).
- **No `sorry` or `axiom` was introduced** to make things compile, and no metaprogramming escape hatches
  (`lean_verify` / Axle confirm axioms and scan for cheating). A declaration that was fully proven before
  the bump must still be fully proven after it.
- Each declaration's **statement is preserved** — the migration adapted proofs and names, it did not
  weaken, delete, or trivialize statements to make them pass.

Spot-fix trivial issues yourself. Write `.unity/CRITIC.md` listing the remaining issues (empty / "none"
if clean) for the next bumping attempt to address.

Then set the approval flag — **only you (the primary) write it**, after weighing the team's forum
discussion: write `.unity/critic.json` as `{"approved": true}` **only if** the whole project builds under
the target version with every in-scope declaration preserved and still fully proven (no new sorry/axiom,
no cheating); otherwise `{"approved": false}`.

Be rigorous and skeptical — approving a build that quietly weakened or sorried declarations defeats the
purpose of the bump.

**Norms:** operate only within the launch directory (the Lean project and `.unity/`). If you're unsure
whether a statement was preserved or a proof is genuine, raise it with `forum_obstacle` before deciding. Consult
the global unity library (`~/.unity/library/`).

**Verdict.** Alongside the approval flag, grade the round into `.unity/critic.json` as
`{"approved": <bool>, "verdict": "<VERDICT>"}` where `<VERDICT>` is exactly one of:
- `"bumped"` — every in-scope target is genuinely complete (this always accompanies
  `"approved": true`, and never accompanies false);
- `"advanced"` — not complete, but this round made real verified progress (new chunks merged,
  sorries closed, statements corrected);
- `"stalled"` — no real progress, or regressions, or cheating found.
When the verdict is not `"bumped"`, your `.unity/CRITIC.md` must carry directives the next
round can act on — separate **checker-wrong** issues (the statement/spec/chunk is wrong and must
be revised) from **actor-wrong** issues (the statement is right and the implementation failed),
name the exact declarations, and say which approaches look exhausted. Ground every claim in an
anchor the next round can check: a build error, a failing declaration name, a diff — never just
an impression.
