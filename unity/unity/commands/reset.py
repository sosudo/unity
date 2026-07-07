import asyncclick as click


@click.command(name="reset")
async def reset():
    """Remove all Unity subagents and skills."""
    raise NotImplementedError


command = reset
