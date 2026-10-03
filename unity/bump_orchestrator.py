"""Bump-owned dispatch and MCP configuration.

Copied from the established formalization path. The solve/prove dispatchers and
backend modules are not used or configured by this pipeline.
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

from rich.console import Console

from . import library
from .bump_spawn import bump_mcp_with_runtime_env, spawn
from .bump_provider import BumpTransportRetriesExhausted
from .orchestrator import load_prompt, mark_done, mark_phase, resume_point, toposort

_console = Console()


def stop_requested(cwd) -> bool:
    """Honor target-local and identity-bound original-project stop requests.

    A private migration owns its runtime state, while ``unity stop`` is normally
    issued in the unchanged original checkout. Never follow an arbitrary path
    from an origin record; malformed or mismatched identities stop fail-closed.
    """
    from .config import find_unity_dir

    unity = find_unity_dir(Path(cwd))
    if unity is None:
        return False
    unity = unity.resolve()
    if (unity / "stop-requested").exists():
        return True
    origin = unity / "bump-origin.json"
    if not origin.exists() and not origin.is_symlink():
        return False
    try:
        if origin.is_symlink():
            return True
        record = json.loads(origin.read_text())
        if set(record) != {"project_root", "run_id", "target_path"}:
            return True
        run_id = record["run_id"]
        if not isinstance(run_id, str) or not re.fullmatch(r"bump-[A-Za-z0-9_-]+", run_id):
            return True
        original, target = Path(record["project_root"]), Path(record["target_path"])
        if (not original.is_absolute() or not target.is_absolute()
                or original.resolve(strict=True) != original
                or target.resolve(strict=True) != target
                or target != original / ".unity" / "bump" / run_id / "target"
                or unity.parent != target):
            return True
        active_path = original / ".unity" / "bump" / "active.json"
        if active_path.is_symlink():
            return True
        active = json.loads(active_path.read_text())
        if (not isinstance(active, dict)
                or any(active.get(key) != value for key, value in record.items())
                or active.get("status") not in {"ready", "preparing"}):
            return True
        return (original / ".unity" / "stop-requested").exists()
    except (OSError, ValueError, TypeError, RuntimeError):
        return True


def load_role_prompt(name: str) -> str:
    """Optional work roles are not phase transitions (nor shell-MCP profile changes)."""
    if name != "SOURCE_REPAIR":
        raise ValueError(f"unknown bump work role {name!r}")
    return (Path(__file__).with_name("prompts") / "bump" / f"{name}.md").read_text()


def build_bump_mcp(paths, profile: str) -> dict:
    """MCP servers for one ``unity bump`` phase.

    The server key intentionally remains ``unity-forum`` so backend shell bridges
    keep working, while the implementation and advertised tools are bump-only.
    """
    servers = {
        "unity-forum": {
            "command": sys.executable,
            "args": [
                "-m", "unity.forum.bump_server",
                "--forum-dir", str(paths.forum),
                "--project-root", str(paths.project_root),
                "--profile", profile,
            ],
        },
    }
    # Critics consume already-bound machine evidence. Do not expose external
    # services that can rebuild, edit, or submit proof work from the review view.
    if profile == "critic":
        return bump_mcp_with_runtime_env(servers, os.environ)
    servers["lean-lsp"] = {"command": "uvx", "args": ["lean-lsp-mcp"]}
    axle_key = os.getenv("AXLE_API_KEY")
    if axle_key:
        servers["axle"] = {
            "command": "uvx",
            "args": ["--from", "axiom-axle-mcp", "axle-mcp-server"],
            "env": {"AXLE_API_KEY": axle_key},
        }
    aristotle_key = os.getenv("ARISTOTLE_API_KEY")
    if aristotle_key:
        servers["aristotle"] = {
            "command": sys.executable,
            "args": ["-m", "unity.aristotle"],
            "env": {"ARISTOTLE_API_KEY": aristotle_key},
        }
    # Shell bridges run inside the worker. Native backends rebind this mapping
    # in spawn() using their own runtime overrides instead of controller values.
    return bump_mcp_with_runtime_env(servers, os.environ)



def _preamble(agent, roster, ranking=None, *, icrl_enabled=False) -> str:
    """Describe the roster without ICRL incentives or dynamic rankings."""
    team = "\n".join(
        f"- {a.name}: {a.model} ({a.backend})"
        f"{' [primary]' if a.is_primary else ''}"
        for a in roster.agents
    )
    return (
        f"You are agent '{agent.name}', running model '{agent.model}' "
        f"(backend: {agent.backend}).\n"
        f"You are collaborating with this team via the forum:\n{team}\n"
        f"The primary agent is '{roster.primary.name}'.\n"
        "Follow the current role's completion instructions. When formalizing, continue until your "
        "current task work is complete or concretely blocked. Before ending a blocked formalizing "
        "attempt, call yield_task with your task ID and reason; do not merely unclaim and repeat "
        "the same blocker. Other roles follow their own reporting tools.\n\n"
    )


async def dispatch(
    agents, roster, base_prompt, task, cwd, mcp, *,
    tools_prompt="BUMP_FORMALIZING_TOOLS", icrl_enabled=False,
    brief_provider=None, log_context=None, mcp_profile="bump",
    on_normal_completion=None, env_overrides=None,
):
    """Launch bump agents with its own backend and compact Forum state."""
    agents = list(agents)
    phase = (log_context or {}).get("phase")
    forum_args = mcp.get("unity-forum", {}).get("args", [])
    forum_phase = (forum_args[forum_args.index("--profile") + 1]
                   if "--profile" in forum_args and forum_args.index("--profile") + 1 < len(forum_args)
                   else None)
    env_phase = (env_overrides or {}).get("UNITY_BUMP_PROFILE")
    phases = {value for value in (phase, forum_phase, env_phase) if value is not None}
    if "critic" in phases:
        if mcp_profile != "bump" or phases != {"critic"} or forum_phase != "critic":
            raise ValueError("Bump critic requires a consistent snapshot-bound critic profile")
        if any(agent.backend != "codex" for agent in agents):
            raise ValueError("Bump critic requires the verified read-only Codex backend")
        if set(mcp) != {"unity-forum"}:
            raise ValueError("Bump critic may use only the snapshot-bound Forum tools")
    def agent_cwd(agent):
        return cwd[agent.name] if isinstance(cwd, dict) else cwd

    any_cwd = next(iter(cwd.values())) if isinstance(cwd, dict) else cwd
    if stop_requested(any_cwd):
        _console.print("[yellow]stop requested — skipping phase[/yellow]")
        return []

    context = library.library_context()
    full = base_prompt + "\n\n" + load_prompt(tools_prompt)
    if context:
        full += "\n\n" + context
    subagents = library.library_subagents()

    def brief(agent):
        if os.getenv("UNITY_FORUM_BRIEF", "on").lower() == "off":
            return ""
        try:
            if brief_provider is not None:
                text = brief_provider(agent.name)
            else:
                from .config import find_unity_dir
                from .forum import bump_server
                unity = find_unity_dir(Path(any_cwd))
                if unity is None:
                    return ""
                bump_server.configure(
                    unity.resolve() / "forum" / "bump", unity.resolve().parent,
                    (log_context or {}).get("phase", "chunking"),
                )
                text = bump_server.bump_brief(agent.name)
            return (
                f"\nWorkspace brief (live state — refresh anytime with bump_brief):\n{text}\n"
                if text else ""
            )
        except Exception:
            return ""

    async def spawn_one(agent):
        # Chunking/critic/retrospective workers use this dispatcher rather than
        # the formalizing scheduler. Their uvx children need the same scoped
        # writable tool environment; never fall back to the global tools install.
        from .config import Paths, find_unity_dir
        from .bump_runtime import _agent_runtime_env
        unity = find_unity_dir(Path(agent_cwd(agent)))
        runtime_env = {}
        if unity is not None:
            runtime_env = _agent_runtime_env(
                Paths.from_unity_dir(unity.resolve()), log_context or {}, agent.name,
            )
        runtime_env.update(env_overrides or {})
        return await spawn(
            agent, _preamble(agent, roster) + brief(agent) + full, task,
            agent_cwd(agent), mcp, subagents=subagents,
            log_context=log_context, mcp_profile=mcp_profile,
            on_normal_completion=on_normal_completion,
            env_overrides=runtime_env,
        )

    results = await asyncio.gather(*(spawn_one(agent) for agent in agents), return_exceptions=True)
    for agent, result in zip(agents, results):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BumpTransportRetriesExhausted):
            # Keep transport exhaustion distinct from a missing critic verdict
            # so the command can try another already-configured eligible critic.
            raise result
        if isinstance(result, BaseException):
            # Operational failure is not an ordinary missing verdict. Preserve
            # the cause and stop instead of spending another critic attempt.
            raise RuntimeError(
                f"Bump {forum_phase or phase or 'phase'} agent {agent.name} failed "
                f"({type(result).__name__}); pipeline stopped. See run diagnostics."
            ) from result
    return results
