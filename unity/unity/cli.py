"""Unity CLI — registers each command from its own module in commands/."""

import asyncclick as click

from .commands import (
    new, init, autoformalize, formalize, prove, optimize, create, solve, verify, bump,
    serve, update, doctor, help, agent, version, reset, clean,
    complete, uninstall, source, metric,
)

_COMMANDS = (
    new, init, autoformalize, formalize, prove, optimize, create, solve, verify, bump,
    serve, update, doctor, help, agent, version, reset, clean,
    complete, uninstall, source, metric,
)


@click.group()
def cli():
    """Unity — multi-agent autoformalization for Lean 4."""


for _mod in _COMMANDS:
    cli.add_command(_mod.command)


def main():
    cli(_anyio_backend="asyncio")
