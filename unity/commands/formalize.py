import os
import json
import asyncclick as click

from ..config import load_paths
from ..roster import load_roster
from ..orchestrator import dispatch, build_mcp, load_prompt, run_worktree_phase, toposort, mark_phase, stop_requested, resume_point, mark_done


@click.command(name="formalize")
@click.option("--targets", default="All", help="What to formalize.")
@click.option("--continue", "continue_", is_flag=True, default=False, help="Run a reprompt cycle first.")
async def formalize(targets, continue_):
    """Formalize source into an existing project."""
    paths = load_paths()
    (paths.unity / "stop-requested").unlink(missing_ok=True)  # stale safe-stop flag
    roster = load_roster(paths.agents_yaml)
    mcp = build_mcp(paths)
    resume = resume_point(paths, "formalize", continue_)
    _order = ["preparation", "architect", "exploration", "semiformalization", "formalizing", "critic", "retrospective"]
    def _do(phase: str) -> bool:
        return resume is None or _order.index(phase) >= _order.index(resume)
    if resume is not None:
        click.echo(f"resuming from phase: {resume}")
    root = paths.project_root
    max_attempts = float(os.getenv("MAX_ATTEMPTS") or "inf")  # blank/unset = indefinite

    if resume is None:
        if continue_:
            await dispatch([roster.primary], roster, load_prompt("formalize/PREPARATION"),
                           "Analyze the current project state and latest logs; update .unity/UNITY.md with context for continuing.",
                           root, mcp)
        else:
            # Fresh run: bootstrap LeanArchitect (version-guarded; skips cleanly when no
            # toolchain-matching release exists or the dependency breaks the build).
            mark_phase("formalize", "architect")
            await dispatch([roster.primary], roster, load_prompt("ARCHITECT"),
                           "Fresh-run bootstrap: add LeanArchitect as a project dependency pinned to the "
                           "ref matching lean-toolchain, verify with lake build (revert + skip on any "
                           "breakage), so later phases can annotate declarations with @[blueprint].",
                           root, mcp)

    if _do("exploration"):
        await dispatch(roster.agents, roster, load_prompt("formalize/EXPLORATION"),
                       "Research the source in .unity/source/, the existing project's gaps in scope (sorries, "
                       "axioms, missing declarations), and existing Mathlib coverage, to inform semiformalization "
                       "and formalization.",
                       root, mcp)

    if _do("semiformalization"):
        await dispatch(roster.agents, roster, load_prompt("formalize/SEMIFORMALIZATION"),
                       f"Identify the target gaps in the existing project (scope: {targets} — sorries, axioms, and "
                       f"missing declarations) and chunk the source material in .unity/source/ needed to formalize "
                       f"them into the project; write .unity/dag.json.",
                       root, mcp)
        toposort(paths)

    if resume != "retrospective":
        i = 0
        approved = False
        while (not approved) and (i < max_attempts) and not stop_requested(root):
            await run_worktree_phase(roster, paths, mcp, load_prompt("formalize/FORMALIZING"), "Formalize")
            (paths.unity / "critic.json").write_text(json.dumps({"approved": False, "verdict": "stalled"}))
            await dispatch([roster.primary], roster, load_prompt("formalize/CRITIC"),
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
            i += 1

    await dispatch([roster.primary], roster, load_prompt("formalize/RETROSPECTIVE"),
                   "Distill lessons from this run into the library.",
                   root, mcp)
    mark_done(paths, "formalize")


command = formalize