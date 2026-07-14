# prepare (if continue), explore the problem, solve into .unity/source/FINAL.tex, chunk the material (likely no need to semiformalize since
# the proof is ai-generated, formalize-critic loop [both phases can elect to change the source as well], retrospective

import os
import json
import asyncclick as click

from ..config import load_paths
from ..roster import load_roster
from ..orchestrator import dispatch, build_mcp, load_prompt, run_worktree_phase, toposort, read_approved, read_finalized, mark_phase


@click.command(name="solve")
@click.option("--continue", "continue_", is_flag=True, default=False, help="Run a reprompt cycle first.")
async def solve(continue_):
    """Solve a problem in natural language and verify the solution in Lean."""
    paths = load_paths()
    (paths.unity / "stop-requested").unlink(missing_ok=True)  # stale safe-stop flag
    roster = load_roster(paths.agents_yaml)
    mcp = build_mcp(paths)
    root = paths.project_root
    max_attempts = int(os.getenv("MAX_ATTEMPTS", "5"))

    if continue_:
        await dispatch([roster.primary], roster, load_prompt("solve/PREPARATION"),
                       "Analyze the current project state and latest logs; update .unity/UNITY.md with context for continuing.",
                       root, mcp)
    else:
        # Fresh run: bootstrap LeanArchitect (version-guarded; skips cleanly when no
        # toolchain-matching release exists or the dependency breaks the build).
        mark_phase("solve", "architect")
        await dispatch([roster.primary], roster, load_prompt("ARCHITECT"),
                       "Fresh-run bootstrap: add LeanArchitect as a project dependency pinned to the "
                       "ref matching lean-toolchain, verify with lake build (revert + skip on any "
                       "breakage), so later phases can annotate declarations with @[blueprint].",
                       root, mcp)

    await dispatch(roster.agents, roster, load_prompt("solve/EXPLORATION"),
                   "Map the mathematical frontier of the problem in .unity/UNITY.md: known partial results and "
                   "their techniques, equivalent formulations, analogous solved problems, and published "
                   "data/computations. Research only — no solving, no Lean, no descoping decisions.",
                   root, mcp)

    # Adjudicated solving loop: the primary referees each round; a stalled round is
    # re-attacked with the verdict's directives instead of sliding into formalization.
    solve_attempts = max_attempts  # same knob as the formalization/critic loop
    # Panel of judges: the primary + the strongest non-primary agent (when one exists) —
    # a round only counts as solved if every judge independently agrees.
    judges = [roster.primary]
    others = sorted((a for a in roster.agents if not a.is_primary), key=lambda a: -a.strength)
    if others:
        judges.append(others[0])
    verdict = "stalled"
    for s in range(solve_attempts):
        if s == 0:
            # Independent drafts before the shared document: prevents anchoring on the
            # first idea posted.
            await dispatch(roster.agents, roster, load_prompt("solve/DRAFTING"),
                           "Independently (no forum reading first) write your own attack plan and strongest "
                           "initial results to .unity/source/drafts/<your name>.md. Do not edit PROOF.tex.",
                           root, mcp)
        reboot = "" if s == 0 else (
            " A previous round was adjudicated as stalled — read .unity/VERDICT.md, perform a research "
            "reboot (reread the problem, list every established fact, generate at least five "
            "fundamentally different attack plans before choosing one), and attack again.")
        drafts = " Start from the independent drafts in .unity/source/drafts/ — mine every one of them for lines of attack." if s == 0 else ""
        await dispatch(roster.agents, roster, load_prompt("solve/SOLVING"),
                       "Collaboratively solve the problem in .unity/UNITY.md and write the full solution and proof "
                       "as a paper to .unity/source/PROOF.tex." + drafts + reboot,
                       root, mcp)
        vdir = paths.unity / "verdicts"
        import shutil
        shutil.rmtree(vdir, ignore_errors=True)
        vdir.mkdir()
        await dispatch(judges, roster, load_prompt("solve/ADJUDICATION"),
                       "Independently adjudicate this solving round: judge .unity/source/PROOF.tex against the "
                       "original problem and write .unity/verdicts/<your name>.json and .md.",
                       root, mcp)
        # Merge: most conservative verdict wins; every judge's report goes into VERDICT.md.
        rank = {"stalled": 0, "advanced": 1, "solved": 2}
        got = []
        for jf in sorted(vdir.glob("*.json")):
            try:
                got.append(json.loads(jf.read_text()).get("verdict", "stalled"))
            except (OSError, json.JSONDecodeError):
                got.append("stalled")
        verdict = min(got, key=lambda v: rank.get(v, 0)) if got else "stalled"
        reports = "\n\n---\n\n".join(f"# Judge: {m.stem}\n\n{m.read_text()}" for m in sorted(vdir.glob("*.md")))
        (paths.unity / "VERDICT.md").write_text(f"Merged verdict: {verdict}\n\n{reports}\n")
        # Archive the round: later rounds rewrite PROOF.tex, and a stalled round's
        # partial results must never be lost (VERDICT.md points back into these).
        proof = paths.unity / "source" / "PROOF.tex"
        if proof.exists():
            rounds = paths.unity / "rounds"
            rounds.mkdir(exist_ok=True)
            (rounds / f"round-{s + 1}-{verdict}.tex").write_text(proof.read_text())
        # if verdict in ("solved", "advanced"):
        if verdict in ("solved"):
            break

    await dispatch(roster.agents, roster, load_prompt("solve/CHUNKING"),
                   "Separate .unity/source/PROOF.tex into chunks — each theorem/lemma/proposition/definition a "
                   "node with its dependencies; write .unity/dag.json.",
                   root, mcp)
    toposort(paths)

    i = 0
    approved = False   # formalization accepted [critic.json]
    finalized = True   # solution accepted as-is [finalized.json]
    while (not approved) and (i < max_attempts):
        if not finalized:
            await dispatch(roster.agents, roster, load_prompt("solve/RESOLVING"),
                           "Revise the solution in .unity/source/PROOF.tex to address the issues raised during formalization.",
                           root, mcp)
            await dispatch(roster.agents, roster, load_prompt("solve/RECHUNKING"),
                           "Re-chunk the revised .unity/source/PROOF.tex into .unity/dag.json, keeping dependencies correct.",
                           root, mcp)
            toposort(paths)
        (paths.unity / "finalized.json").write_text(json.dumps({"finalized": True}))
        await run_worktree_phase(roster, paths, mcp, load_prompt("solve/FORMALIZING"), "Formalize")
        (paths.unity / "critic.json").write_text(json.dumps({"approved": False}))
        await dispatch([roster.primary], roster, load_prompt("solve/CRITIC"),
                       "Review the project. Spot-fix trivial issues; write .unity/CRITIC.md with the remaining "
                       "issues; set .unity/critic.json to {\"approved\": true} only if every target is fully "
                       "proven (no sorry/axiom in scope, builds clean, no cheating), otherwise false.",
                       root, mcp)
        approved = read_approved(paths)
        finalized = read_finalized(paths)
        i += 1

    await dispatch([roster.primary], roster, load_prompt("solve/RETROSPECTIVE"),
                   "Distill lessons from this run into the library.",
                   root, mcp)


command = solve
