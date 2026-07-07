import asyncclick as click


@click.command(name="help")
@click.pass_context
async def help(ctx):
    """List all commands."""
    click.echo(ctx.parent.get_help())


command = help
