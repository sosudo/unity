import os
import json
import asyncclick as click

from ..config import load_paths
from ..roster import load_roster
from ..orchestrator import dispatch, build_mcp, load_prompt, run_worktree_phase, toposort, read_approved


@click.command(name="formalize")
@click.option("--targets", default="All", help="What to formalize.")
@click.option("--continue", "continue_", is_flag=True, default=False, help="Run a reprompt cycle first.")
async def formalize(targets, continue_):
    """Formalize source into an existing project."""
    paths = load_paths()
    roster = load_roster(paths.agents_yaml)
    mcp = build_mcp(paths)
    root = paths.project_root
    max_attempts = int(os.getenv("MAX_ATTEMPTS", "5"))

    if continue_:
        await dispatch([roster.primary], roster, load_prompt("formalize/PREPARATION"),
                       "Analyze the current project state and latest logs; update .unity/UNITY.md with context for continuing.",
                       root, mcp)
    
    await dispatch(roster.agents, roster, load_prompt("formalize/EXPLORATION"),
                   "Research the source in .unity/source/, the existing project's gaps in scope (sorries, "
                   "axioms, missing declarations), and existing Mathlib coverage, to inform semiformalization "
                   "and formalization.",
                   root, mcp)

    await dispatch(roster.agents, roster, load_prompt("formalize/SEMIFORMALIZATION"),
                   f"Identify the target gaps in the existing project (scope: {targets} — sorries, axioms, and "
                   f"missing declarations) and chunk the source material in .unity/source/ needed to formalize "
                   f"them into the project; write .unity/dag.json.",
                   root, mcp)
    toposort(paths)

    i = 0
    approved = False
    while (not approved) and (i < max_attempts):
        await run_worktree_phase(roster, paths, mcp, load_prompt("formalize/FORMALIZING"), "Formalize")
        (paths.unity / "critic.json").write_text(json.dumps({"approved": False}))
        await dispatch([roster.primary], roster, load_prompt("formalize/CRITIC"),
                       "Review the project. Spot-fix trivial issues; write .unity/CRITIC.md with the remaining "
                       "issues; set .unity/critic.json to {\"approved\": true} only if every target is fully "
                       "proven (no sorry/axiom in scope, builds clean, no cheating), otherwise false.",
                       root, mcp)
        approved = read_approved(paths)
        i += 1

    await dispatch([roster.primary], roster, load_prompt("formalize/RETROSPECTIVE"),
                   "Distill lessons from this run into the library.",
                   root, mcp)


command = formalize