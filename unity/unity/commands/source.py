import shutil
from pathlib import Path

import asyncclick as click

from ..config import require_unity_dir


def _source_dir() -> Path:
    d = require_unity_dir() / "source"
    d.mkdir(parents=True, exist_ok=True)
    return d


@click.group(name="source")
def source():
    """Manage source material in .unity/source/."""


@source.command(name="add")
@click.argument("path", type=click.Path(exists=True))
async def add(path):
    """Copy a file or folder into .unity/source/."""
    src = Path(path)
    dest = _source_dir() / src.name
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dest)
    click.echo(f"Added {dest}")


@source.command(name="remove")
@click.argument("name")
async def remove(name):
    """Remove a file or folder from .unity/source/."""
    target = _source_dir() / Path(name).name
    if not target.exists():
        raise click.ClickException(f"{target} not found in .unity/source/")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    click.echo(f"Removed {target}")


@source.command(name="list")
async def list_():
    """List all files in .unity/source/ (recursive)."""
    d = _source_dir()
    entries = sorted(p.relative_to(d) for p in d.rglob("*"))
    if not entries:
        click.echo("(.unity/source/ is empty)")
        return
    for e in entries:
        click.echo(e)


@source.command(name="help")
@click.pass_context
async def help_(ctx):
    """Show help for the source subcommands."""
    click.echo(ctx.parent.get_help())


command = source
