'''
Metrics exist in .unity/metrics/
Preexisting metrics are .unity/metrics/[length, modularity, completion]

metrics consist of a name, prompt, examples [optional], score function [optional], metric function [optional]

agents should read through the codebase and the metric, chunk appropriately (maybe a whole file for some metrics, maybe per decl for others),
optimize/critic loop, retrospective
'''
import os
import json
import asyncclick as click

from ..config import load_paths
from ..roster import load_roster
from ..orchestrator import dispatch, build_mcp, load_prompt, run_worktree_phase, toposort, mark_phase, stop_requested, resume_point, mark_done


@click.command(name="optimize")
@click.argument("metric")
@click.option("--targets", default="Universe", help="Targets to optimize.")
@click.option("--continue", "continue_", is_flag=True, default=False, help="Run a reprompt cycle first.")
async def optimize(metric, targets, continue_):
    """Optimize Lean code agentically with respect to METRIC."""
    paths = load_paths()
    (paths.unity / "stop-requested").unlink(missing_ok=True)  # stale safe-stop flag
    roster = load_roster(paths.agents_yaml)
    mcp = build_mcp(paths)
    resume = resume_point(paths, "optimize", continue_)
    _order = ["preparation", "architect", "exploration", "chunking", "optimizing", "critic", "retrospective"]
    def _do(phase: str) -> bool:
        return resume is None or _order.index(phase) >= _order.index(resume)
    if resume is not None:
        click.echo(f"resuming from phase: {resume}")
    root = paths.project_root
    max_attempts = float(os.getenv("MAX_ATTEMPTS") or "inf")  # blank/unset = indefinite

    if resume is None:
        if continue_:
            await dispatch([roster.primary], roster, load_prompt("optimize/PREPARATION"),
                           "Analyze the current project state and latest logs; update .unity/UNITY.md with context for continuing.",
                           root, mcp)
        else:
            # Fresh run: bootstrap LeanArchitect (version-guarded; skips cleanly when no
            # toolchain-matching release exists or the dependency breaks the build).
            mark_phase("optimize", "architect")
            await dispatch([roster.primary], roster, load_prompt("ARCHITECT"),
                           "Fresh-run bootstrap: add LeanArchitect as a project dependency pinned to the "
                           "ref matching lean-toolchain, verify with lake build (revert + skip on any "
                           "breakage), so later phases can annotate declarations with @[blueprint].",
                           root, mcp)

    if _do("exploration"):
        await dispatch(roster.agents, roster, load_prompt("optimize/EXPLORATION"),
                       f"Understand the metric '{metric}' (its definition in .unity/metrics/) and the codebase; "
                       f"research techniques and references for optimizing Lean code with respect to it.",
                       root, mcp)

    if _do("chunking"):
        await dispatch(roster.agents, roster, load_prompt("optimize/CHUNKING"),
                       f"Each Lean declaration in scope ({targets}) becomes a chunk with its dependencies and its "
                       f"current score on the metric '{metric}'; record the metric name and write .unity/dag.json.",
                       root, mcp)
        toposort(paths)

    if resume != "retrospective":
        i = 0
        approved = False # for if the optimization is successful [critic.json]
        while (not approved) and (i < max_attempts) and not stop_requested(root):
            await run_worktree_phase(roster, paths, mcp, load_prompt("optimize/OPTIMIZING"), "Optimize")
            (paths.unity / "critic.json").write_text(json.dumps({"approved": False, "verdict": "stalled"}))
            await dispatch([roster.primary], roster, load_prompt("optimize/CRITIC"),
                           f"Review the optimization against the metric '{metric}'. Spot-fix trivial issues; write "
                           f".unity/CRITIC.md with the remaining issues; set .unity/critic.json to {{\"approved\": true}} "
                           f"only if the in-scope code genuinely improved on the metric while still building and staying "
                           f"correct (no new sorry/axiom, statements preserved, no cheating), otherwise false.",
                           root, mcp)
            try:
                _c = json.loads((paths.unity / "critic.json").read_text())
            except (OSError, json.JSONDecodeError):
                _c = {}
            approved = bool(_c.get("approved", False))
            verdict = _c.get("verdict", "stalled")
            click.echo(f"critic verdict: {verdict} (approved={approved})")
            i += 1

    await dispatch([roster.primary], roster, load_prompt("optimize/RETROSPECTIVE"),
                   "Distill lessons from this run into the library.",
                   root, mcp)
    mark_done(paths, "optimize")


command = optimize
