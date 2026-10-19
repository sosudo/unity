"""Optimize-only shell transport, installed privately on its workers' PATH.

This is not another workflow entry point. It exposes the same phase-scoped MCP
tools as the native transport and refuses nested Unity pipeline invocations.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback

from . import artifacts
from .optimize_spawn import _optimize_codex_tool_policy, optimize_mcp_with_runtime_env


def worker_context(environ=None, cwd=None):
    env = os.environ if environ is None else environ
    if env.get("UNITY_OPTIMIZE_WORKFLOW") != "metric-optimization-v1":
        raise ValueError("Optimize shell bridge requires an active Optimize worker binding")
    author = str(env.get("UNITY_AGENT_NAME") or "").strip()
    profile = str(env.get("UNITY_OPTIMIZE_PROFILE") or "").strip()
    raw_root, raw_tree = env.get("UNITY_OPTIMIZE_PROJECT_ROOT"), env.get("UNITY_OPTIMIZE_WORKTREE")
    if not author or not profile or not raw_root or not raw_tree:
        raise ValueError("Optimize shell bridge has incomplete worker identity")
    root, tree = Path(raw_root), Path(raw_tree)
    if not root.is_absolute() or root.resolve() != root or not tree.is_absolute() or tree.resolve() != tree:
        raise ValueError("Optimize worker paths must be exact absolute paths")
    here = Path(cwd or Path.cwd()).resolve()
    if not here.is_relative_to(tree):
        raise ValueError("call Optimize tools from the assigned private worktree")
    unity = tree / ".unity"
    if unity.is_symlink():
        raise ValueError("Optimize worker configuration must be private")
    marker = unity / "optimize-worktree.json"
    if marker.is_symlink() or not marker.is_file():
        raise ValueError("Optimize private worktree binding is absent")
    binding = json.loads(marker.read_text())
    if binding != {"schema": "optimize-worktree-v1", "project_root": str(root)}:
        raise ValueError("Optimize private worktree does not bind this project")
    if (unity / "forum").resolve() != root / ".unity/forum":
        raise ValueError("Optimize shared forum binding changed")
    from .optimize_workflow import paths_for
    return paths_for(root), tree, profile, author


async def _external_call(paths, server, spec, tool, arguments, *, cwd):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    error = None
    result = None
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stderr:
        transport = StdioTransport(command=spec["command"], args=spec.get("args", []),
            env=spec.get("env"), cwd=str(cwd), keep_alive=False, log_file=stderr)
        try:
            async with Client(transport) as client:
                result = await client.call_tool(tool, arguments)
        except Exception as exc:
            error = exc
        stderr.seek(0)
        diagnostics = stderr.read()
    if error is not None:
        diagnostics += "\n" + "".join(traceback.format_exception(error))
    note = ""
    if diagnostics:
        record = artifacts.store_text(paths.artifacts, diagnostics, kind="mcp_diagnostics",
            producer=os.getenv("UNITY_AGENT_NAME", ""), source=f"{server}.{tool}")
        note = "Server diagnostics: " + record["artifact_id"]
    if error is not None:
        # Provider stderr/exception text can contain credentials or headers.
        # Preserve it as an artifact rather than printing raw diagnostics.
        raise RuntimeError(f"{server}.{tool} failed ({type(error).__name__}). {note}") from error
    return result, note


async def call_tool(server, tool, arguments, *, environ=None, cwd=None):
    if not isinstance(arguments, dict):
        raise ValueError("MCP arguments must be a JSON object")
    env = os.environ if environ is None else environ
    paths, tree, profile, author = worker_context(env, cwd)
    from .optimize_workflow import mcp_servers
    servers = optimize_mcp_with_runtime_env(mcp_servers(paths, profile), env)
    server = "unity-forum" if server == "forum" else server
    policy = _optimize_codex_tool_policy(servers, profile)
    if server not in policy:
        raise ValueError("server is not exposed by this Optimize phase's configured MCP catalog. "
                         "Use exact forum server name 'unity-forum' or alias 'forum'. "
                         "Configured servers for " + profile + ": " + ", ".join(sorted(policy)))
    if tool not in policy[server]:
        raise ValueError("tool is not exposed by this Optimize phase's configured MCP catalog. "
                         "Available tools for " + profile + "/" + server + ": "
                         + ", ".join(sorted(policy[server])))
    if "author" in arguments and arguments["author"] != author:
        raise ValueError("author does not match this Optimize worker")
    note = ""
    if server == "unity-forum":
        from fastmcp import Client
        from .forum import optimize_server
        optimize_server.configure(paths.forum, paths.project_root, profile, author=author)
        async with Client(optimize_server.build_server(profile)) as client:
            result = await client.call_tool(tool, arguments)
    else:
        result, note = await _external_call(paths, server, servers[server], tool, arguments, cwd=tree)
    output = "\n".join(getattr(block, "text", str(block))
        for block in (getattr(result, "content", None) or []))
    if output:
        compacted = artifacts.compact_text(paths.artifacts, output, kind="mcp_output",
            producer=author, source=f"{server}.{tool}")
        print(artifacts.format_compacted(compacted))
    if note:
        print(note, flush=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["mcp"])
    parser.add_argument("server")
    parser.add_argument("tool")
    parser.add_argument("args", nargs="?")
    parser.add_argument("--args-file", type=argparse.FileType("r", encoding="utf-8"))
    options = parser.parse_args(argv)
    if options.args is not None and options.args_file is not None:
        parser.error("pass positional JSON or --args-file, not both")
    try:
        arguments = (json.load(options.args_file) if options.args_file is not None
                     else json.loads(options.args) if options.args else {})
        asyncio.run(call_tool(options.server, options.tool, arguments))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
