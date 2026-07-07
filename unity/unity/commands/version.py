import asyncclick as click

from .. import __version__


@click.command(name="version")
async def version():
    """Show the Unity version."""
    click.echo(__version__)


command = version
