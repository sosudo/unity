import os
import json
import asyncclick as click

from ..config import load_paths
from ..roster import load_roster
from ..orchestrator import dispatch, build_mcp, load_prompt, run_worktree_phase, toposort, read_approved


@click.command(name="bump")
@click.option("--continue", "continue_", is_flag=True, default=False, help="Run a reprompt cycle first.")
async def bump(continue_):
    """Bump Lean/Mathlib version if possible"""
    paths = load_paths()
    roster = load_roster(paths.agents_yaml)
    mcp = build_mcp(paths)
    root = paths.project_root
    max_attempts = int(os.getenv("MAX_ATTEMPTS", "5"))

    if continue_:
        await dispatch([roster.primary], roster, load_prompt("bump/PREPARATION"),
                       "Analyze the current project state and latest logs; update .unity/UNITY.md with context for continuing.",
                       root, mcp)

    await dispatch(roster.agents, roster, load_prompt("bump/CHUNKING"),
                   "Each Lean declaration in the project becomes a chunk with its dependencies; write .unity/dag.json.",
                   root, mcp)
    toposort(paths)

    await dispatch(roster.agents, roster, load_prompt("bump/EXPLORATION"),
                   "Research what changed between the project's current version and the target version in "
                   ".unity/UNITY.md (renamed/moved/removed declarations, API and tactic changes, deprecations) "
                   "and gather the replacements; resolve dependencies for the chunks.",
                   root, mcp)

    # Deterministically set the project to the target version on master (builds with errors);
    # the worktree bumping phase then fixes the breakage.
    await dispatch([roster.primary], roster, load_prompt("bump/SETVERSION"),
                   "Set the project to the target version in .unity/UNITY.md (edit lean-toolchain and the "
                   "lakefile dependency, remove .lake, run lake update and lake exe cache get, commit). The "
                   "project will build with errors afterward — that is expected; do not fix declarations.",
                   root, mcp)

    i = 0
    approved = False
    while (not approved) and (i < max_attempts):
        await run_worktree_phase(roster, paths, mcp, load_prompt("bump/BUMPING"), "Bump")
        (paths.unity / "critic.json").write_text(json.dumps({"approved": False}))
        await dispatch([roster.primary], roster, load_prompt("bump/CRITIC"),
                       "Review the project. Spot-fix trivial issues; write .unity/CRITIC.md with the remaining "
                       "issues; set .unity/critic.json to {\"approved\": true} only if every target is fully "
                       "proven (no sorry/axiom in scope, builds clean, no cheating), otherwise false.",
                       root, mcp)
        approved = read_approved(paths)
        i += 1

    await dispatch([roster.primary], roster, load_prompt("bump/RETROSPECTIVE"),
                   "Distill lessons from this run into the library.",
                   root, mcp)


command = bump
