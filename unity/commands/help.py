import asyncclick as click


@click.command(name="help")
@click.pass_context
async def help(ctx):
    """List all commands."""
    group = ctx.parent.command
    click.echo("Usage: unity [OPTIONS] COMMAND [ARGS]...\n")
    click.echo("  Unity — multi-agent autoformalization for Lean 4.\n")
    click.echo("Commands:")
    width = max(len(n) for n in group.commands)
    for name in sorted(group.commands):
        text = (group.commands[name].help or "").strip()
        desc = text.splitlines()[0] if text else ""
        click.echo(f"  {name:<{width}}  {desc}")


command = help
