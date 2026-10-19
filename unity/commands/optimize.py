"""Metric-driven optimization with durable same-project continuation."""
import math
import os
import asyncclick as click
from ..config import load_paths
from ..roster import load_roster

def _attempt_limit():
    value = os.getenv("MAX_ATTEMPTS", "").strip()
    if not value:
        return math.inf
    try:
        limit = int(value)
    except ValueError as exc:
        raise click.ClickException("MAX_ATTEMPTS must be a positive integer or blank") from exc
    if limit < 1:
        raise click.ClickException("MAX_ATTEMPTS must be a positive integer or blank")
    return limit

@click.command(name="optimize")
@click.argument("metric")
@click.option("--targets", default="Universe", help="Targets to optimize.")
@click.option("--continue", "continue_", is_flag=True, default=False,
              help="Resume the saved optimization in this project.")
async def optimize(metric, targets, continue_):
    """Optimize Lean code agentically with respect to METRIC."""
    from .. import optimize_workflow
    paths = load_paths()
    try:
        roster = load_roster(paths.agents_yaml)
        result = await optimize_workflow.run(paths, roster, metric=metric, targets=targets,
            continue_=continue_, max_attempts=_attempt_limit(),
            retrospective=os.getenv("RETROSPECTIVE", "true").strip().lower() != "false")
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    status = result.get("status", "incomplete")
    if status == "accepted":
        click.echo("optimize complete: checked improvement and primary review passed")
    else:
        click.echo(f"optimize {status}: work and evidence retained in this project's .unity")

command = optimize
