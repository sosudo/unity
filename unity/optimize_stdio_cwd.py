"""Preserve Optimize MCP working directories when Claude starts elsewhere.

This controller-owned stdio adapter changes only cwd and PWD. It replaces
itself with the configured server; it is not a shell or another worker loop.
"""

import argparse
import os
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    command = options.command
    if command and command[0] == "--":
        command = command[1:]
    if not command or not command[0]:
        parser.error("an executable command is required after --")

    source = Path(options.cwd)
    try:
        valid = source.is_absolute() and source.resolve(strict=True) == source and source.is_dir()
    except (OSError, RuntimeError):
        valid = False
    if not valid:
        parser.error("--cwd must be an existing canonical absolute directory")

    os.chdir(source)
    environment = {**os.environ, "PWD": str(source)}
    os.execvpe(command[0], command, environment)


if __name__ == "__main__":
    main()
