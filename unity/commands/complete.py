import shutil
import asyncclick as click

from ..config import find_unity_dir


@click.command(name="complete")
async def complete():
    """Remove Unity artifacts from a Lean project."""
    unity = find_unity_dir()
    if unity is None:
        raise click.ClickException("no .unity/ found in this project")
    root = unity.parent
    worktrees = root / ".worktrees"
    if not click.confirm(f"Remove {unity}, {worktrees}, and their .gitignore entries?", default=False):
        raise click.Abort()

    shutil.rmtree(unity)
    shutil.rmtree(worktrees, ignore_errors=True)

    gi = root / ".gitignore"
    if gi.exists():
        drop = {".unity/", ".worktrees/"}
        kept = [ln for ln in gi.read_text().splitlines() if ln.strip() not in drop]
        gi.write_text("\n".join(kept) + ("\n" if kept else ""))

    click.echo(f"Removed {unity} and {worktrees}")


command = complete
