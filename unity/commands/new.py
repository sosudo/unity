import asyncclick as click
from pathlib import Path

from .. import lake
from .init import run_init


@click.command(name="new")
@click.argument("project_name")
@click.option("--version", default="latest", help="Lean toolchain version.")
@click.option("--math", is_flag=True, default=False, help="Include Mathlib.")
async def new(project_name, version, math):
    """Create a new Lean project and initialize it as a Unity project."""
    lake.new_project(project_name, math=math)
    project = Path.cwd() / project_name

    if version != "latest":
        (project / "lean-toolchain").write_text(f"leanprover/lean4:{version}\n")

    lake.ensure_initial_commit(project)
    await run_init(project)


command = new
