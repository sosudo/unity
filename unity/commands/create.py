import os
import json
import asyncclick as click

from ..config import load_paths
from ..roster import load_roster
from ..orchestrator import dispatch, build_mcp, load_prompt, run_worktree_phase, toposort, read_finalized, mark_phase, stop_requested, resume_point, mark_done


@click.command(name="create")
@click.option("--continue", "continue_", is_flag=True, default=False, help="Run a reprompt cycle first.")
async def create(continue_):
    """Create a library in Lean based off of the informal description in UNITY.md."""
    paths = load_paths()
    (paths.unity / "stop-requested").unlink(missing_ok=True)  # stale safe-stop flag
    roster = load_roster(paths.agents_yaml)
    mcp = build_mcp(paths)
    resume = resume_point(paths, "create", continue_)
    _order = ["preparation", "architect", "exploration", "creation", "chunking", "recreation", "rechunking", "formalizing", "critic", "retrospective"]
    def _do(phase: str) -> bool:
        return resume is None or _order.index(phase) >= _order.index(resume)
    if resume is not None:
        click.echo(f"resuming from phase: {resume}")
    root = paths.project_root
    max_attempts = float(os.getenv("MAX_ATTEMPTS") or "inf")  # blank/unset = indefinite

    if resume is None:
        if continue_:
            await dispatch([roster.primary], roster, load_prompt("create/PREPARATION"),
                           "Analyze the current project state and latest logs; update .unity/UNITY.md with context for continuing.",
                           root, mcp)
        else:
            # Fresh run: bootstrap LeanArchitect (version-guarded; skips cleanly when no
            # toolchain-matching release exists or the dependency breaks the build).
            mark_phase("create", "architect")
            await dispatch([roster.primary], roster, load_prompt("ARCHITECT"),
                           "Fresh-run bootstrap: add LeanArchitect as a project dependency pinned to the "
                           "ref matching lean-toolchain, verify with lake build (revert + skip on any "
                           "breakage), so later phases can annotate declarations with @[blueprint].",
                           root, mcp)

    if _do("exploration"):
        await dispatch(roster.agents, roster, load_prompt("create/EXPLORATION"),
                       "Research the library described in .unity/UNITY.md — its domain, relevant existing Mathlib "
                       "APIs, prior art, and design/formalization strategies — to inform the specification and build.",
                       root, mcp)

    if _do("creation"):
        await dispatch(roster.agents, roster, load_prompt("create/CREATION"),
                       "Collaboratively design the library described in .unity/UNITY.md and write its full "
                       "specification to .unity/source/SPEC.md.",
                       root, mcp)

    if _do("chunking"):
        await dispatch(roster.agents, roster, load_prompt("create/CHUNKING"),
                       "Separate .unity/source/SPEC.md into chunks — each definition/structure/theorem/etc. a "
                       "node with its dependencies; write .unity/dag.json.",
                       root, mcp)
        toposort(paths)

    if resume != "retrospective":
        i = 0
        approved = False   # formalization accepted [critic.json]
        finalized = True   # specification accepted as-is [finalized.json]
        while (not approved) and (i < max_attempts) and not stop_requested(root):
            if not finalized:
                await dispatch(roster.agents, roster, load_prompt("create/RECREATION"),
                               "Revise the specification in .unity/source/SPEC.md to address the issues raised during formalization.",
                               root, mcp)
                await dispatch(roster.agents, roster, load_prompt("create/RECHUNKING"),
                               "Re-chunk the revised .unity/source/SPEC.md into .unity/dag.json, keeping dependencies correct.",
                               root, mcp)
                toposort(paths)
            (paths.unity / "finalized.json").write_text(json.dumps({"finalized": True}))
            await run_worktree_phase(roster, paths, mcp, load_prompt("create/FORMALIZING"), "Formalize")
            (paths.unity / "critic.json").write_text(json.dumps({"approved": False, "verdict": "stalled"}))
            await dispatch([roster.primary], roster, load_prompt("create/CRITIC"),
                           "Review the project. Spot-fix trivial issues; write .unity/CRITIC.md with the remaining "
                           "issues; set .unity/critic.json to {\"approved\": true} only if every target is fully "
                           "proven (no sorry/axiom in scope, builds clean, no cheating), otherwise false.",
                           root, mcp)
            try:
                _c = json.loads((paths.unity / "critic.json").read_text())
            except (OSError, json.JSONDecodeError):
                _c = {}
            approved = bool(_c.get("approved", False))
            verdict = _c.get("verdict", "stalled")
            click.echo(f"critic verdict: {verdict} (approved={approved})")
            finalized = read_finalized(paths)
            i += 1

    await dispatch([roster.primary], roster, load_prompt("create/RETROSPECTIVE"),
                   "Distill lessons from this run into the library.",
                   root, mcp)
    mark_done(paths, "create")


command = create
