import asyncclick as click


@click.command(name="doctor")
async def doctor():
    """Run an interactive resolver session via the primary agent."""
    raise NotImplementedError


command = doctor
