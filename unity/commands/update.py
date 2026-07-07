import subprocess
import asyncclick as click


@click.command(name="update")
async def update():
    """Fetch the latest version of Unity and update."""
    if subprocess.run(["uv", "tool", "upgrade", "unity"]).returncode != 0:
        raise click.ClickException("update failed")


command = update
