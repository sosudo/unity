"""Autoformalize-owned dispatch and MCP configuration.

Copied from the established formalization path. The solve/prove dispatchers and
backend modules are not used or configured by this pipeline.
"""

import asyncio
import os
import sys
from pathlib import Path

from rich.console import Console

from . import library
from .autoformalize_spawn import autoformalize_mcp_with_runtime_env, spawn
from .orchestrator import load_prompt, mark_done, mark_phase, resume_point, stop_requested, toposort

_console = Console()


def build_autoformalize_mcp(paths, profile: str) -> dict:
    """MCP servers for one ``unity autoformalize`` phase.

    The server key intentionally remains ``unity-forum`` so backend shell bridges
    keep working, while the implementation and advertised tools are autoformalize-only.
    """
    servers = {
        "lean-lsp": {"command": "uvx", "args": ["lean-lsp-mcp"]},
        "unity-forum": {
            "command": sys.executable,
            "args": [
                "-m", "unity.forum.autoformalize_server",
                "--forum-dir", str(paths.forum),
                "--project-root", str(paths.project_root),
                "--profile", profile,
            ],
        },
    }
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
    return autoformalize_mcp_with_runtime_env(servers, os.environ)



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
        "Continue until your current proof-search work is complete or concretely blocked. "
        "Publish the blocker and release your strategy before ending a blocked turn.\n\n"
    )


async def dispatch(
    agents, roster, base_prompt, task, cwd, mcp, *,
    tools_prompt="AUTOFORMALIZE_FORMALIZING_TOOLS", icrl_enabled=False,
    brief_provider=None, log_context=None, mcp_profile="autoformalize",
):
    """Launch autoformalize agents with its own backend and compact Forum state."""
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
                from .forum import autoformalize_server
                unity = find_unity_dir(Path(any_cwd))
                if unity is None:
                    return ""
                autoformalize_server.configure(
                    unity.resolve() / "forum" / "autoformalize", unity.resolve().parent,
                    (log_context or {}).get("phase", "chunking"),
                )
                text = autoformalize_server.autoformalize_brief(agent.name)
            return (
                f"\nWorkspace brief (live state — refresh anytime with autoformalize_brief):\n{text}\n"
                if text else ""
            )
        except Exception:
            return ""

    async def spawn_one(agent):
        return await spawn(
            agent, _preamble(agent, roster) + brief(agent) + full, task,
            agent_cwd(agent), mcp, subagents=subagents,
            log_context=log_context, mcp_profile="autoformalize",
        )

    results = await asyncio.gather(*(spawn_one(agent) for agent in agents), return_exceptions=True)
    for agent, result in zip(agents, results):
        if isinstance(result, Exception):
            _console.print(f"[red]agent {agent.name} failed: {result!r}[/red]")
    return results
