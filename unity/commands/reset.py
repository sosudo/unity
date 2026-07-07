import shutil
import asyncclick as click

from ..library import library_dir, ensure_library


@click.command(name="reset")
async def reset():
    """Remove all Unity subagents and skills (restore the global library to a clean slate)."""
    lib = library_dir()
    if lib.exists() and not click.confirm(f"Wipe {lib} (all learned tactics/lemmas/subagents/skills)?",
                                          default=False):
        raise click.Abort()
    if lib.exists():
        shutil.rmtree(lib)
    ensure_library()
    click.echo(f"Library reset: {lib}")


command = reset
