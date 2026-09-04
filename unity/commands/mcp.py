"""`unity mcp` — call an MCP tool from the shell.

Escape hatch for backends whose native MCP passthrough is broken (codex >=0.117 with
custom Responses-API providers marks MCP servers unsupported: openai/codex#19871,
#23186, #26977). The forum is called in-process (same file-locked functions, no
subprocess); other servers get a one-shot stdio client.
"""

import json
import os
from pathlib import Path

import asyncclick as click


def _solve_shared_paths(paths) -> tuple[Path, Path]:
    """Return solve's shared forum and main root, resolving worktree links."""
    shared_unity = paths.unity.resolve()
    return shared_unity / "forum", shared_unity.parent


@click.command(name="mcp")
@click.argument("server")
@click.argument("tool")
@click.argument("args", required=False, default="{}")
async def mcp(server, tool, args):
    """Call TOOL on MCP SERVER with JSON ARGS (e.g. unity mcp unity-forum forum_stats '{}')."""
    from ..config import load_paths
    from ..orchestrator import build_mcp, build_solve_mcp
    from fastmcp import Client

    try:
        kwargs = json.loads(args) if args.strip() else {}
    except json.JSONDecodeError as e:
        raise click.ClickException(f"args must be a JSON object: {e}")
    if not isinstance(kwargs, dict):
        raise click.ClickException("args must be a JSON object")

    paths = load_paths()
    try:
        run_state = json.loads((paths.unity / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        run_state = {}
    active_solve = run_state.get("command") == "solve" and run_state.get("phase") != "done"
    solve_profile = run_state.get("phase", "solving")
    if solve_profile not in {
        "solving", "solution_review", "chunking", "formalizing", "critic", "retrospective",
    }:
        solve_profile = "solving"
    if server in ("unity-forum", "forum") and active_solve:
        from ..forum import solve_server
        # Worktrees expose the run-scoped .unity directory through a symlink.
        # Resolve it before deriving the source root so candidate verification and
        # synchronization operate against main rather than the caller's HEAD.
        shared_forum, shared_root = _solve_shared_paths(paths)
        solve_server.configure(shared_forum, shared_root, solve_profile)
        client = Client(solve_server.build_server(solve_profile))
    elif server in ("unity-forum", "forum"):
        from ..forum import server as fsrv
        fsrv.FORUM_DIR = paths.forum
        fsrv.PROJECT_ROOT = paths.unity.resolve().parent
        fsrv.ICRL_ENABLED = not (
            run_state.get("command") == "prove" and run_state.get("phase") != "done"
        )
        client = Client(fsrv.mcp)  # in-process: no subprocess, same flock-safe storage
    else:
        specs = build_solve_mcp(paths, solve_profile) if active_solve else build_mcp(paths)
        if server not in specs:
            raise click.ClickException(f"unknown server '{server}' (available: {', '.join(specs)})")
        client = Client({"mcpServers": {server: specs[server]}})

    async with client as c:
        res = await c.call_tool(tool, kwargs)
    rendered = []
    for block in getattr(res, "content", None) or []:
        text = getattr(block, "text", None)
        rendered.append(text if text is not None else str(block))
    output = "\n".join(rendered)
    active_prove = run_state.get("command") == "prove" and run_state.get("phase") != "done"
    bounded_artifact_read = server in ("unity-forum", "forum") and tool in {
        "artifact_read", "artifact_snapshot_file",
    }
    if (active_prove or active_solve) and output and not bounded_artifact_read:
        from .. import artifacts
        compacted = artifacts.compact_text(
            paths.artifacts,
            output,
            kind="mcp_output",
            producer=os.getenv("UNITY_AGENT_NAME", ""),
            source=f"{server}.{tool}",
        )
        print(artifacts.format_compacted(compacted))
    elif output:
        print(output)


command = mcp
