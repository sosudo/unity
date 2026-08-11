"""The event-driven ``unity prove`` command."""

import json
import os

import asyncclick as click

from ..config import load_paths
from ..orchestrator import load_prompt, mark_done
from ..prove_runtime import RuntimeStore, active_run_dir, discover_goals, missing_requested_targets
from ..prove_scheduler import ProveScheduler, deterministic_setup
from ..roster import load_roster


@click.command(name="prove")
@click.option("--targets", default="All", help="Declarations to prove (comma-separated), or All.")
@click.option("--continue", "continue_", is_flag=True, default=False,
              help="Resume the active prove runtime and its structured state.")
async def prove(targets: str, continue_: bool):
    """Prove targets with adaptive tasks, immutable candidates, and deterministic checks."""
    paths = load_paths()
    (paths.unity / "stop-requested").unlink(missing_ok=True)

    click.echo("deterministic setup: inspecting toolchain/project and validating the build")
    setup = deterministic_setup(paths.project_root)

    store = None
    current = active_run_dir(paths.unity) if continue_ else None
    if current is not None:
        candidate_store = RuntimeStore(current)
        try:
            state = candidate_store.load()
            if state.get("command") == "prove" and state.get("status") != "complete":
                store = candidate_store
                store.mark_status("running")
                click.echo(f"resuming prove runtime: {state['run_id']}")
        except (OSError, json.JSONDecodeError, KeyError):
            store = None

    if store is None:
        missing = missing_requested_targets(paths.project_root, targets)
        if missing:
            raise click.ClickException("requested Lean declaration(s) not found: " + ", ".join(missing))
        goals = discover_goals(paths.project_root, targets)
        quorum = int(os.getenv("UNITY_PROVE_REVIEW_QUORUM", "1"))
        store = RuntimeStore.create(paths.unity, paths.project_root, goals,
                                    review_quorum=quorum)
        (store.run_dir / "artifacts" / "setup.json").write_text(json.dumps(setup, indent=2))
        click.echo(f"discovered {len(goals)} target goal(s) without LLM chunking")

    initial = store.load()
    if not initial["goals"]:
        store.mark_status("complete")
        mark_done(paths, "prove")
        click.echo("prove runtime complete: no unresolved targets (0 model calls)")
        return

    # Loading this single prompt records the only model phase: continuous PROVE.
    roster = load_roster(paths.agents_yaml, use_learned_strength=False)
    prompt = load_prompt("prove/PROVE") + "\n\n" + load_prompt("TOOLS")
    scheduler = ProveScheduler(roster, paths, store, prompt)
    final = await scheduler.run()
    closed = sum(g["status"] == "closed" for g in final["goals"].values())
    total = len(final["goals"])
    click.echo(f"prove runtime {final['status']}: {closed}/{total} goal(s) closed")
    click.echo(f"state: {store.path}")
    if final["status"] == "complete":
        mark_done(paths, "prove")
    elif final["status"] == "exhausted":
        raise click.ClickException(
            "prove exhausted configured attempts/capacity with unresolved goals; "
            "inspect forum_brief and the run state, then resume after adding capacity or tasks")


command = prove
