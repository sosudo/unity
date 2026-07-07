import asyncclick as click


@click.command(name="clean")
async def clean():
    """Remove unused Unity subagents and skills."""
    raise NotImplementedError


command = clean
