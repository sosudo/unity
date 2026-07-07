import subprocess
import asyncclick as click


@click.command(name="uninstall")
async def uninstall():
    """Uninstall Unity."""
    if not click.confirm("Uninstall unity (uv tool uninstall unity)?", default=False):
        raise click.Abort()
    if subprocess.run(["uv", "tool", "uninstall", "unity"]).returncode != 0:
        raise click.ClickException("uninstall failed")


command = uninstall
