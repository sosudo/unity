"""`unity mcp` — call an MCP tool from the shell.

Escape hatch for backends whose native MCP passthrough is broken (codex >=0.117 with
custom Responses-API providers marks MCP servers unsupported: openai/codex#19871,
#23186, #26977). The forum is called in-process (same file-locked functions, no
subprocess); other servers get a one-shot stdio client.
"""

import json
import os

import asyncclick as click


_SOLVE_PROFILES = {
    "solving",
    "solution_review",
    "chunking",
    "formalizing",
    "critic",
    "retrospective",
}


def _solve_forum_profile(paths, run_state: dict) -> str:
    """Select the least-privileged solve Forum profile for the current stage."""
    assigned = str(os.getenv("UNITY_SOLVE_PROFILE") or "")
    if assigned in _SOLVE_PROFILES:
        # A model turn keeps the role it was spawned with. Global state may advance
        # while that turn is draining after an interrupt; it must not inherit the
        # next role's mutation tools through the shell bridge.
        return assigned
    try:
        solve_state = json.loads((paths.unity / "solve" / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        solve_state = {}
    # The retrospective runs after the authoritative solve state is complete, so the
    # command-level phase is the only place that stage is visible.  Every live stage
    # otherwise comes from solve state, not model-selected arguments.
    run_phase = str(run_state.get("phase") or "")
    if run_phase == "retrospective":
        return "retrospective"
    stage = str(solve_state.get("stage") or run_phase or "solving")
    if stage in _SOLVE_PROFILES:
        return stage
    return {
        "architect": "solving",
        "complete": "critic",
        "stopped": "critic",
        "exhausted": "critic",
        "failed": "critic",
        "done": "critic",
    }.get(stage, "solving")


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
    if server in ("unity-forum", "forum"):
        from ..forum import server as fsrv
        fsrv.FORUM_DIR = paths.forum
        fsrv.PROJECT_ROOT = paths.unity.resolve().parent
        fsrv.ICRL_ENABLED = not (
            run_state.get("command") == "prove" and run_state.get("phase") != "done"
        )
        client = Client(fsrv.mcp)  # in-process: no subprocess, same flock-safe storage
    elif server in ("unity-solve-forum", "solve-forum"):
        profile = _solve_forum_profile(paths, run_state)
        specs = build_solve_mcp(
            paths,
            profile,
            agent_name=str(os.getenv("UNITY_AGENT_NAME") or ""),
        )
        solve_spec = specs.get("unity-solve-forum")
        if solve_spec is None:
            raise click.ClickException("solve Forum is unavailable in this Unity installation")
        client = Client({"mcpServers": {"unity-solve-forum": solve_spec}})
    else:
        specs = build_mcp(paths)
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
    active_structured_run = (
        run_state.get("command") in {"prove", "solve"}
        and run_state.get("phase") != "done"
    )
    bounded_artifact_read = server in (
        "unity-forum", "forum", "unity-solve-forum", "solve-forum"
    ) and tool in {
        "artifact_read", "artifact_snapshot_file",
    }
    if active_structured_run and output and not bounded_artifact_read:
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
