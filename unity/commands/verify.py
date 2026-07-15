# prepare (if continue), semiformalize the source, explore for helpers, verify-critic loop, retrospect

import os
import json
import asyncclick as click

from ..config import load_paths
from ..roster import load_roster
from ..orchestrator import dispatch, build_mcp, load_prompt, run_worktree_phase, toposort, read_approved, mark_phase


@click.command(name="verify")
@click.option("--targets", default="All", help="What to verify.")
@click.option("--continue", "continue_", is_flag=True, default=False, help="Run a reprompt cycle first.")
async def verify(targets, continue_):
    """For software/program verification"""
    paths = load_paths()
    (paths.unity / "stop-requested").unlink(missing_ok=True)  # stale safe-stop flag
    roster = load_roster(paths.agents_yaml)
    mcp = build_mcp(paths)
    root = paths.project_root
    max_attempts = float(os.getenv("MAX_ATTEMPTS") or "inf")  # blank/unset = indefinite

    if continue_:
        await dispatch([roster.primary], roster, load_prompt("verify/PREPARATION"),
                       "Analyze the current project state and latest logs; update .unity/UNITY.md with context for continuing.",
                       root, mcp)
    else:
        # Fresh run: bootstrap LeanArchitect (version-guarded; skips cleanly when no
        # toolchain-matching release exists or the dependency breaks the build).
        mark_phase("verify", "architect")
        await dispatch([roster.primary], roster, load_prompt("ARCHITECT"),
                       "Fresh-run bootstrap: add LeanArchitect as a project dependency pinned to the "
                       "ref matching lean-toolchain, verify with lake build (revert + skip on any "
                       "breakage), so later phases can annotate declarations with @[blueprint].",
                       root, mcp)

    await dispatch(roster.agents, roster, load_prompt("verify/SEMIFORMALIZATION"),
                   f"Model the source code in .unity/source/ and the verification goals in .unity/UNITY.md "
                   f"(scope: {targets}) as a dependency DAG of chunks — data structures, function behaviors, "
                   f"specifications, and the correctness properties to verify; write .unity/dag.json.",
                   root, mcp)
    toposort(paths)

    await dispatch(roster.agents, roster, load_prompt("verify/EXPLORATION"),
                   "Resolve external dependencies and gather helper material (relevant Lean/Mathlib libraries, "
                   "semantics models, prior verifications) for the chunks.",
                   root, mcp)

    i = 0
    approved = False
    while (not approved) and (i < max_attempts):
        await run_worktree_phase(roster, paths, mcp, load_prompt("verify/VERIFYING"), "Verify")
        (paths.unity / "critic.json").write_text(json.dumps({"approved": False}))
        await dispatch([roster.primary], roster, load_prompt("verify/CRITIC"),
                       "Review the project. Spot-fix trivial issues; write .unity/CRITIC.md with the remaining "
                       "issues; set .unity/critic.json to {\"approved\": true} only if every target is fully "
                       "proven (no sorry/axiom in scope, builds clean, no cheating), otherwise false.",
                       root, mcp)
        approved = read_approved(paths)
        i += 1

    await dispatch([roster.primary], roster, load_prompt("verify/RETROSPECTIVE"),
                   "Distill lessons from this run into the library.",
                   root, mcp)


command = verify
