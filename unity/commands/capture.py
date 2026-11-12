"""Run a command while externalizing large output to the shared artifact store."""

from __future__ import annotations

import os
import json
import shlex
import subprocess

import asyncclick as click

from .. import artifacts
from ..config import load_paths


@click.command(
    name="capture",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
async def capture(command):
    """Run COMMAND directly and retain large stdout/stderr under .unity/artifacts/."""
    if not command:
        raise click.UsageError("missing command after 'unity capture --'")
    paths = load_paths()
    source = shlex.join(command)
    try:
        completed = subprocess.run(
            list(command),
            cwd=paths.project_root,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        sections = []
        if completed.stdout:
            sections.append(completed.stdout.rstrip("\n"))
        if completed.stderr:
            sections.append(completed.stderr.rstrip("\n"))
        output = "\n".join(sections)
        code = completed.returncode
    except OSError as exc:
        output = f"could not execute {command[0]}: {exc}"
        code = 127

    if output:
        compacted = artifacts.compact_text(
            paths.artifacts,
            output,
            kind="command_output",
            producer=os.getenv("UNITY_AGENT_NAME", ""),
            source=source,
            metadata={"exit_code": code, "argv": list(command)},
        )
        if isinstance(compacted, dict):
            try:
                run_state = json.loads((paths.unity / "state.json").read_text())
            except (OSError, json.JSONDecodeError):
                run_state = {}
            server = None if run_state.get("command") == "prove" else "unity-forum"
            click.echo(artifacts.format_compacted(compacted, exit_code=code, server=server,
                                                 artifact_dir=paths.artifacts if server is None else None))
        else:
            click.echo(compacted)
    elif code:
        click.echo(f"Command exited {code} with no output.")
    if code:
        raise click.exceptions.Exit(code)


command = capture
