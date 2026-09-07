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


async def _call_solve_stdio(paths, server, spec, tool, kwargs):
    """Keep solve's one-shot server diagnostics out of the tool result."""
    import tempfile
    import traceback
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport
    from .. import artifacts

    error = None
    with tempfile.TemporaryFile(
        mode="w+", encoding="utf-8", errors="replace",
    ) as stderr:
        transport = StdioTransport(
            command=spec["command"],
            args=spec.get("args", []),
            env=spec.get("env"),
            cwd=spec.get("cwd"),
            keep_alive=False,
            log_file=stderr,
        )
        try:
            async with Client(transport) as client:
                result = await client.call_tool(tool, kwargs)
        except Exception as exc:
            error = exc

        stderr.seek(0)
        diagnostics = stderr.read()

    if error is not None:
        diagnostics += "\n" + "".join(traceback.format_exception(error))

    note = ""
    if diagnostics:
        try:
            record = artifacts.store_text(
                paths.artifacts,
                diagnostics,
                kind="mcp_diagnostics",
                producer=os.getenv("UNITY_AGENT_NAME", ""),
                source=f"{server}.{tool}",
            )
        except OSError as exc:
            # Diagnostic persistence must not replace the actual tool outcome.
            note = f"Server diagnostics could not be saved: {artifacts.preview_text(str(exc))}"
        else:
            note = f"Server diagnostics: {record['artifact_id']}"

    if error is not None:
        message = artifacts.preview_text(f"{type(error).__name__}: {error}")
        raise click.ClickException(
            f"{server}.{tool} failed: {message}\n{note}"
        ) from error

    return result, note


def _autoformalize_shared_paths(paths) -> tuple[Path, Path]:
    """Resolve autoformalize's own forum from main or a linked agent worktree."""
    shared_unity = paths.unity.resolve()
    return shared_unity / "forum" / "autoformalize", shared_unity.parent


async def _call_autoformalize_stdio(paths, server, spec, tool, kwargs):
    """Keep autoformalize's one-shot server diagnostics out of the tool result."""
    import tempfile
    import traceback
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport
    from .. import artifacts

    error = None
    with tempfile.TemporaryFile(
        mode="w+", encoding="utf-8", errors="replace",
    ) as stderr:
        transport = StdioTransport(
            command=spec["command"],
            args=spec.get("args", []),
            env=spec.get("env"),
            cwd=spec.get("cwd"),
            keep_alive=False,
            log_file=stderr,
        )
        try:
            async with Client(transport) as client:
                result = await client.call_tool(tool, kwargs)
        except Exception as exc:
            error = exc

        stderr.seek(0)
        diagnostics = stderr.read()

    if error is not None:
        diagnostics += "\n" + "".join(traceback.format_exception(error))

    note = ""
    if diagnostics:
        try:
            record = artifacts.store_text(
                paths.artifacts,
                diagnostics,
                kind="mcp_diagnostics",
                producer=os.getenv("UNITY_AGENT_NAME", ""),
                source=f"{server}.{tool}",
            )
        except OSError as exc:
            # Diagnostic persistence must not replace the actual tool outcome.
            note = f"Server diagnostics could not be saved: {artifacts.preview_text(str(exc))}"
        else:
            note = f"Server diagnostics: {record['artifact_id']}"

    if error is not None:
        message = artifacts.preview_text(f"{type(error).__name__}: {error}")
        raise click.ClickException(
            f"{server}.{tool} failed: {message}\n{note}"
        ) from error

    return result, note


@click.command(name="mcp")
@click.argument("server")
@click.argument("tool")
@click.argument("args", required=False)
@click.option("--args-file", type=click.File("r", encoding="utf-8"),
              help="Read the JSON argument object from a UTF-8 file, or - for stdin.")
async def mcp(server, tool, args, args_file):
    """Call TOOL on MCP SERVER with JSON ARGS (e.g. unity mcp unity-forum forum_stats '{}')."""
    if args is not None and args_file is not None:
        raise click.UsageError("Pass either positional JSON args or --args-file, not both.")
    try:
        if args_file is not None:
            kwargs = json.load(args_file)
        else:
            # Preserve the original positional interface, including empty args.
            kwargs = json.loads(args) if args is not None and args.strip() else {}
    except json.JSONDecodeError as e:
        raise click.ClickException(f"args must be a JSON object: {e}")
    except (OSError, UnicodeError) as e:
        raise click.ClickException(f"cannot read JSON args: {e}")
    if not isinstance(kwargs, dict):
        raise click.ClickException("args must be a JSON object")

    from ..config import load_paths
    from ..orchestrator import build_mcp, build_solve_mcp
    from fastmcp import Client

    paths = load_paths()
    try:
        run_state = json.loads((paths.unity / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        run_state = {}
    active_autoformalize = (
        run_state.get("command") == "autoformalize" and run_state.get("phase") != "done"
    )
    autoformalize_profile = run_state.get("phase", "chunking")
    if autoformalize_profile not in {"chunking", "formalizing", "critic", "retrospective"}:
        autoformalize_profile = "chunking"
    active_solve = run_state.get("command") == "solve" and run_state.get("phase") != "done"
    solve_profile = run_state.get("phase", "solving")
    if solve_profile not in {
        "solving", "solution_review", "chunking", "formalizing", "critic", "retrospective",
    }:
        solve_profile = "solving"
    client = None
    diagnostic_note = ""
    if server in ("unity-forum", "forum") and active_autoformalize:
        from ..forum import autoformalize_server
        shared_forum, shared_root = _autoformalize_shared_paths(paths)
        autoformalize_server.configure(shared_forum, shared_root, autoformalize_profile)
        client = Client(autoformalize_server.build_server(autoformalize_profile))
    elif server in ("unity-forum", "forum") and active_solve:
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
        if active_autoformalize:
            from dataclasses import replace
            from ..autoformalize_orchestrator import build_autoformalize_mcp
            shared_forum, _ = _autoformalize_shared_paths(paths)
            specs = build_autoformalize_mcp(replace(paths, forum=shared_forum), autoformalize_profile)
        else:
            specs = build_solve_mcp(paths, solve_profile) if active_solve else build_mcp(paths)
        if server not in specs:
            raise click.ClickException(f"unknown server '{server}' (available: {', '.join(specs)})")
        spec = specs[server]
        if active_autoformalize and spec.get("command"):
            res, diagnostic_note = await _call_autoformalize_stdio(
                paths, server, spec, tool, kwargs,
            )
        elif active_solve and spec.get("command"):
            res, diagnostic_note = await _call_solve_stdio(
                paths, server, spec, tool, kwargs,
            )
        else:
            client = Client({"mcpServers": {server: spec}})

    if client is not None:
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
    if (active_prove or active_solve or active_autoformalize) and output and not bounded_artifact_read:
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
    if diagnostic_note:
        print(diagnostic_note, flush=True)


command = mcp
