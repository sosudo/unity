import asyncclick as click


@click.command(name="agent")
async def agent():
    """Run an interactive session (Hermes-style)."""
    raise NotImplementedError


command = agent
