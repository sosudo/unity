import shutil
from pathlib import Path

import asyncclick as click

from ..config import require_unity_dir

_TEMPLATE = """# {name}

## Prompt
{prompt}

## Examples
{examples}

## Score function
{score}

## Metric function
{metric}
"""


def _metrics_dir() -> Path:
    d = require_unity_dir() / "metrics"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _metric_path(name: str) -> Path:
    return _metrics_dir() / f"{Path(name).name.lower()}.md"


@click.group(name="metric")
def metric():
    """Manage optimization metrics in .unity/metrics/ (for `unity optimize`)."""


@metric.command(name="add")
@click.argument("name")
async def add(name):
    """Create a new metric interactively at .unity/metrics/<name>.md."""
    path = _metric_path(name)
    if path.exists():
        raise click.ClickException(f"metric '{name}' already exists — use `unity metric modify {name}`")
    prompt = await click.prompt("Prompt (what to optimize for, how to judge an improvement)")
    examples = await click.prompt("Examples (optional)", default="")
    score = await click.prompt("Score function — a file in .unity/metrics/ (optional)", default="")
    metric_fn = await click.prompt("Metric function — a file in .unity/metrics/ (optional)", default="")
    path.write_text(_TEMPLATE.format(
        name=name,
        prompt=prompt,
        examples=examples or "(none)",
        score=score or "(none)",
        metric=metric_fn or "(none)",
    ))
    click.echo(f"Created {path}")


@metric.command(name="modify")
@click.argument("name")
async def modify(name):
    """Open an existing metric in your editor."""
    path = _metric_path(name)
    if not path.exists():
        raise click.ClickException(f"metric '{name}' not found")
    edited = click.edit(path.read_text())
    if edited is None:
        click.echo("No changes.")
    else:
        path.write_text(edited)
        click.echo(f"Updated {path}")


@metric.command(name="remove")
@click.argument("name")
async def remove(name):
    """Remove .unity/metrics/<name>.md."""
    path = _metric_path(name)
    if not path.exists():
        raise click.ClickException(f"metric '{name}' not found")
    path.unlink()
    click.echo(f"Removed {path}")


@metric.command(name="move")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
async def move(file):
    """Move a file into .unity/metrics/ (e.g. a score or metric function to reference by name)."""
    src = Path(file)
    dest = _metrics_dir() / src.name
    shutil.move(str(src), str(dest))
    click.echo(f"Moved {dest}")


@metric.command(name="list")
async def list_():
    """List metrics and helper files in .unity/metrics/."""
    d = _metrics_dir()
    entries = sorted(p.relative_to(d) for p in d.rglob("*"))
    if not entries:
        click.echo("(.unity/metrics/ is empty)")
        return
    for e in entries:
        click.echo(e)


@metric.command(name="help")
@click.pass_context
async def help_(ctx):
    """Show help for the metric subcommands."""
    click.echo(ctx.parent.get_help())


command = metric
