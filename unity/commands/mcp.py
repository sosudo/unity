"""`unity mcp` — call an MCP tool from the shell.

Escape hatch for backends whose native MCP passthrough is broken (codex >=0.117 with
custom Responses-API providers marks MCP servers unsupported: openai/codex#19871,
#23186, #26977). The forum is called in-process (same file-locked functions, no
subprocess); other servers get a one-shot stdio client.
"""

import json

import asyncclick as click


@click.command(name="mcp")
@click.argument("server")
@click.argument("tool")
@click.argument("args", required=False, default="{}")
async def mcp(server, tool, args):
    """Call TOOL on MCP SERVER with JSON ARGS (e.g. unity mcp unity-forum forum_stats '{}')."""
    from ..config import load_paths
    from ..orchestrator import build_mcp
    from fastmcp import Client

    try:
        kwargs = json.loads(args) if args.strip() else {}
    except json.JSONDecodeError as e:
        raise click.ClickException(f"args must be a JSON object: {e}")
    if not isinstance(kwargs, dict):
        raise click.ClickException("args must be a JSON object")

    paths = load_paths()
    if server in ("unity-forum", "forum"):
        from ..forum import server as fsrv
        fsrv.FORUM_DIR = paths.forum
        client = Client(fsrv.mcp)  # in-process: no subprocess, same flock-safe storage
    else:
        specs = build_mcp(paths)
        if server not in specs:
            raise click.ClickException(f"unknown server '{server}' (available: {', '.join(specs)})")
        client = Client({"mcpServers": {server: specs[server]}})

    async with client as c:
        res = await c.call_tool(tool, kwargs)
    for block in getattr(res, "content", None) or []:
        text = getattr(block, "text", None)
        print(text if text is not None else block)


command = mcp
