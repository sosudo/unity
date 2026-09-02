"""`unity solve`: informal mathematical solving followed by faithful Lean formalization."""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

import asyncclick as click

from ..Architect import architect
from .. import solve_state
from ..config import load_paths
from ..orchestrator import mark_done, mark_phase
from ..roster import load_roster
from ..solve_runtime import run_solve_retrospective, run_solve_runtime


def _retrospective_enabled() -> bool:
    """Retrospective is on by default and can be disabled for evaluation runs."""
    return os.getenv("RETROSPECTIVE", "true").strip().lower() != "false"


@contextmanager
def _controller_lock(unity_dir: Path):
    """Ensure one controller owns a solve run, including fresh-state reset."""
    unity_dir.mkdir(parents=True, exist_ok=True)
    handle = (unity_dir / "solve-controller.lock").open("a+")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise click.ClickException(
                "another unity solve controller is already running for this project"
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


@click.command(name="solve")
@click.option("--continue", "continue_", is_flag=True, default=False,
              help="Resume the authoritative solve state where it stopped.")
async def solve(continue_):
    """Solve a problem informally, then formalize that exact solution in Lean."""
    paths = load_paths()
    roster = load_roster(paths.agents_yaml)

    with _controller_lock(paths.unity):
        await _solve_locked(paths, roster, continue_)


async def _solve_locked(paths, roster, continue_: bool) -> None:
    """Execute solve while the stable command-lifetime controller lock is held."""
    (paths.unity / "stop-requested").unlink(missing_ok=True)

    try:
        current = solve_state.load_state(paths.unity)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    has_run = bool(current.get("run_id"))
    reset = not continue_
    if continue_ and has_run:
        if current.get("outcome") in {"stopped", "exhausted", "failed"}:
            resumed = solve_state.resume_run(paths.unity)
            current = solve_state.load_state(paths.unity)
            click.echo(
                f"resuming solve run {current['run_id']} after "
                f"{resumed['previous_outcome']} from authoritative stage: "
                f"{resumed['stage']}"
            )
        else:
            click.echo(
                f"resuming solve run {current['run_id']} from authoritative stage: "
                f"{current.get('stage', 'solving')}"
            )
    elif continue_:
        click.echo("no prior solve state found; starting a new solve run")
        reset = True

    if reset:
        mark_phase("solve", "architect")
        architect(paths.project_root)

    state = await run_solve_runtime(roster, paths, reset_state=reset)
    outcome = state.get("outcome") or state.get("stage")

    if _retrospective_enabled() and outcome in {"complete", "exhausted", "failed"}:
        try:
            await run_solve_retrospective(roster, paths)
        except Exception as exc:
            # Retrospective is optional post-run learning. It must not falsify the
            # already-computed solve outcome when its model/backend is unavailable.
            click.echo(f"warning: solve retrospective failed: {exc}", err=True)

    if outcome == "complete":
        mark_done(paths, "solve")
        click.echo("solve complete: informal and formalization gates accepted")
        return

    mark_phase("solve", str(outcome or "failed"))
    if outcome == "stopped":
        click.echo("solve stopped safely; rerun with --continue to resume")
        return
    raise click.ClickException(
        f"solve ended with outcome={outcome or 'failed'}; rerun with --continue after addressing the recorded blockers"
    )


command = solve
