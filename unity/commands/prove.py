import os
import json
import asyncclick as click

from ..Architect import architect
from ..blueprint import build_prove_dag
from ..config import load_paths
from .. import prove_control, prove_mcp, prove_state
from ..prove_runtime import _prove_codex_reasoning_effort, run_prove_runtime
from ..roster import load_roster
from ..orchestrator import dispatch, build_mcp, load_prompt, toposort, mark_phase, stop_requested, resume_point, mark_done


def _retrospective_enabled() -> bool:
    """Retrospective is on by default and may be disabled for evaluation runs."""
    return os.getenv("RETROSPECTIVE", "true").strip().lower() != "false"


def _primary_env_overrides(roster) -> dict[str, dict[str, str]] | None:
    """Apply the Prove effort setting to solo phases as well as proof workers."""
    agent = roster.primary
    if agent.backend != "codex":
        return None
    effort = _prove_codex_reasoning_effort(
        agent.name, os.getenv("UNITY_PROVE_CODEX_REASONING_EFFORT")
    )
    if effort is None:
        return None
    return {agent.name: {"UNITY_PROVE_CODEX_REASONING_EFFORT": effort}}


def _check_stop(root) -> None:
    if stop_requested(root):
        raise click.ClickException("prove stopped before completion")


def _approved_result(paths) -> bool:
    """Require the persisted critic decision and a nonempty solved workspace."""
    try:
        critic = json.loads((paths.unity / "critic.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(critic, dict)
        and critic.get("approved") is True
        and critic.get("verdict") == "proven"
        and critic.get("reopen") == []
        and prove_state.all_solved(prove_state.load_state(paths.forum))
    )


@click.command(name="prove")
@click.option("--targets", default="All", help="Unresolved declaration names or Lean files to prove.")
@click.option("--continue", "continue_", is_flag=True, default=False, help="Run a reprompt cycle first.")
async def prove(targets, continue_):
    """Prove all sorrys and axioms."""
    paths = load_paths()
    (paths.unity / "stop-requested").unlink(missing_ok=True)  # stale safe-stop flag
    roster = load_roster(paths.agents_yaml, use_learned_strength=False)
    primary_env = _primary_env_overrides(roster)
    mcp = prove_mcp.for_run(build_mcp(paths, forum_icrl=False), paths)
    prove_control.configure(paths.forum, paths.project_root)
    resume = resume_point(paths, "prove", continue_)
    if resume == "exploration":
        resume = "proving"
    _order = ["architect", "chunking", "proving", "critic", "retrospective"]
    def _do(phase: str) -> bool:
        return resume is None or _order.index(phase) >= _order.index(resume)
    if resume is not None:
        click.echo(f"resuming from phase: {resume}")
    root = paths.project_root
    max_attempts = float(os.getenv("MAX_ATTEMPTS") or "inf")  # blank/unset = indefinite

    if resume is None and not continue_:
        mark_phase("prove", "architect")
        architect(root)

    _check_stop(root)

    if _do("chunking"):
        mark_phase("prove", "chunking")
        try:
            dag = build_prove_dag(root, paths.unity, targets)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"discovered {len(dag['chunks'])} proof target(s) using {dag['source']} extraction")
        if not dag["chunks"]:
            raise click.ClickException(
                "no unresolved prove targets were discovered; verify the project build and target selection"
            )
        toposort(paths)

    _check_stop(root)

    if resume != "retrospective":
        i = 0
        approved = False
        run_provers = resume != "critic"
        reset_runtime = resume is None and not continue_
        while (not approved) and (i < max_attempts) and not stop_requested(root):
            if run_provers:
                await run_prove_runtime(
                    roster,
                    paths,
                    mcp,
                    load_prompt("prove/PROVING"),
                    reset_state=reset_runtime,
                )
                reset_runtime = False

            _check_stop(root)

            (paths.unity / "critic.json").write_text(json.dumps(
                {"approved": False, "verdict": "stalled", "reopen": []}
            ))
            await dispatch([roster.primary], roster, load_prompt("prove/CRITIC"),
                           "Audit every target without editing Lean. Write actionable findings to "
                           ".unity/CRITIC.md and the structured approved/verdict/reopen decision to "
                           ".unity/critic.json. Reopen any currently solved declaration whose accepted "
                           "candidate has a concrete defect.",
                           root, mcp, tools_prompt="PROVE_TOOLS", icrl_enabled=False,
                           forum_enabled=False, brief_provider=prove_control.build_brief,
                           mcp_profile="prove-control",
                           agent_env_overrides=primary_env)
            _check_stop(root)
            try:
                _c = json.loads((paths.unity / "critic.json").read_text())
            except (OSError, json.JSONDecodeError):
                _c = {}
            approved = _c.get("approved") is True
            verdict = _c.get("verdict", "stalled")
            reopen = _c.get("reopen", [])
            current_state = prove_state.load_state(paths.forum)
            unresolved_state = [
                decl for decl, item in current_state.get("declarations", {}).items()
                if item.get("status") != "solved"
            ]
            if verdict not in {"proven", "advanced", "stalled"}:
                verdict = "stalled"
                approved = False
            if approved and (verdict != "proven" or reopen):
                click.echo("critic approval ignored: approved requires verdict=proven and reopen=[]")
                approved = False
                verdict = "stalled"
            elif approved and unresolved_state:
                click.echo(
                    "critic approval ignored: authoritative state still has unresolved declarations: "
                    + ", ".join(unresolved_state)
                )
                approved = False
                verdict = "stalled"
            elif not approved and verdict == "proven":
                verdict = "stalled"

            if not approved:
                applied = prove_state.apply_critic_reopens(paths.forum, reopen)
                for item in applied["reopened"]:
                    click.echo(
                        f"critic reopened {item['decl']}"
                        + (f" from {item['candidate_id']}" if item["candidate_id"] else "")
                    )
                for item in applied["rejected"]:
                    click.echo(f"ignored invalid critic reopen: {item['reason']}")
                state = prove_state.load_state(paths.forum)
                if prove_state.all_solved(state) and not applied["reopened"]:
                    raise click.ClickException(
                        "critic rejected the run but did not validly reopen any solved declaration"
                    )

            click.echo(f"critic verdict: {verdict} (approved={approved})")
            i += 1
            run_provers = not approved

    _check_stop(root)
    if not _approved_result(paths):
        raise click.ClickException(
            "prove did not reach an approved proven result; inspect the critic and proof state"
        )

    if _retrospective_enabled():
        await dispatch([roster.primary], roster, load_prompt("prove/RETROSPECTIVE"),
                       "Distill lessons from this run into the library.",
                       root, mcp, tools_prompt="PROVE_TOOLS", icrl_enabled=False,
                       forum_enabled=False, brief_provider=prove_control.build_brief,
                       mcp_profile="prove-control",
                       agent_env_overrides=primary_env)
    _check_stop(root)
    mark_done(paths, "prove")


command = prove
