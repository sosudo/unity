import os
import json
import asyncclick as click

from ..config import load_paths
from ..roster import load_roster
from ..orchestrator import dispatch, build_mcp, load_prompt, run_worktree_phase, toposort, read_approved, mark_phase


@click.command(name="prove")
@click.option("--targets", default="All", help="What to prove.")
@click.option("--continue", "continue_", is_flag=True, default=False, help="Run a reprompt cycle first.")
async def prove(targets, continue_):
    """Prove all sorrys and axioms."""
    paths = load_paths()
    roster = load_roster(paths.agents_yaml)
    mcp = build_mcp(paths)
    root = paths.project_root
    max_attempts = int(os.getenv("MAX_ATTEMPTS", "5"))

    if continue_:
        await dispatch([roster.primary], roster, load_prompt("prove/PREPARATION"),
                       "Analyze the current project state and latest logs; update .unity/UNITY.md with context for continuing.",
                       root, mcp)
    else:
        # Fresh run: bootstrap LeanArchitect (version-guarded; skips cleanly when no
        # toolchain-matching release exists or the dependency breaks the build).
        mark_phase("prove", "architect")
        await dispatch([roster.primary], roster, load_prompt("ARCHITECT"),
                       "Fresh-run bootstrap: add LeanArchitect as a project dependency pinned to the "
                       "ref matching lean-toolchain, verify with lake build (revert + skip on any "
                       "breakage), so later phases can annotate declarations with @[blueprint].",
                       root, mcp)

    await dispatch(roster.agents, roster, load_prompt("prove/CHUNKING"),
                   f"Every sorry and axiom in scope ({targets}) becomes a chunk with its dependencies; write .unity/dag.json.",
                   root, mcp)
    toposort(paths)

    await dispatch(roster.agents, roster, load_prompt("prove/EXPLORATION"),
                   "Resolve external dependencies and gather helper material for the chunks.",
                   root, mcp)

    i = 0
    approved = False
    while (not approved) and (i < max_attempts):
        await run_worktree_phase(roster, paths, mcp, load_prompt("prove/PROVING"), "Prove")
        (paths.unity / "critic.json").write_text(json.dumps({"approved": False}))
        await dispatch([roster.primary], roster, load_prompt("prove/CRITIC"),
                       "Review the project. Spot-fix trivial issues; write .unity/CRITIC.md with the remaining "
                       "issues; set .unity/critic.json to {\"approved\": true} only if every target is fully "
                       "proven (no sorry/axiom in scope, builds clean, no cheating), otherwise false.",
                       root, mcp)
        approved = read_approved(paths)
        i += 1

    await dispatch([roster.primary], roster, load_prompt("prove/RETROSPECTIVE"),
                   "Distill lessons from this run into the library.",
                   root, mcp)


command = prove
