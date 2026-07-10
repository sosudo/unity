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
                   "Research proof strategies, formalization approaches, and any papers or resources that help "
                   "solve and later formalize the problem in .unity/UNITY.md.",
                   root, mcp)

    await dispatch(roster.agents, roster, load_prompt("solve/SOLVING"),
                   "Collaboratively solve the problem in .unity/UNITY.md and write the full solution and proof "
                   "as a paper to .unity/source/PROOF.tex.",
                   root, mcp)

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
