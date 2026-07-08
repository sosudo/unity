"""Interactive REPL sessions with the primary agent (used by `unity agent` / `unity doctor`).

One long-lived session per invocation: claude_code via ClaudeSDKClient (multi-turn),
codex via a single thread with repeated turns. Type 'exit' or 'quit' (or Ctrl-D) to leave.
"""

import asyncio
import tempfile
from pathlib import Path

import asyncclick as click

from .roster import Agent
from .spawn import _agent_env, _log, _write_codex_config, _write_codex_agents


async def _read_user() -> str | None:
    try:
        return (await asyncio.to_thread(input, "\nyou> ")).strip()
    except (EOFError, KeyboardInterrupt):
        return None


async def _claude_session(agent: Agent, system_prompt: str, cwd: Path, mcp: dict, subagents) -> None:
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers=mcp,
        agents={s["name"]: AgentDefinition(description=s["description"], prompt=s["prompt"], tools=s["tools"])
                for s in subagents},
        cwd=str(cwd),
        permission_mode="bypassPermissions",
        model=agent.model,
        env=_agent_env(agent),
    )
    client = ClaudeSDKClient(options=options)
    await client.connect()
    try:
        while True:
            user = await _read_user()
            if not user or user.lower() in ("exit", "quit"):
                break
            await client.query(user)
            async for msg in client.receive_response():
                _log(agent.name, msg)
    finally:
        await client.disconnect()


async def _codex_session(agent: Agent, system_prompt: str, cwd: Path, mcp: dict, subagents) -> None:
    from openai_codex import AsyncCodex, CodexConfig, Sandbox

    home = Path(tempfile.mkdtemp(prefix="unity-codex-"))
    provider = _write_codex_config(home, agent, mcp)
    _write_codex_agents(home, subagents)
    codex = AsyncCodex(config=CodexConfig(cwd=str(cwd), env=_agent_env(agent, home)))
    try:
        if agent.api_key:
            await codex.login_api_key(agent.api_key)
        thread = await codex.thread_start(
            model=agent.model, model_provider=provider,
            sandbox=Sandbox.full_access, base_instructions=system_prompt, cwd=str(cwd),
        )
        while True:
            user = await _read_user()
            if not user or user.lower() in ("exit", "quit"):
                break
            handle = await thread.turn(user)
            # stream() is the sole consumer (run() would compete for the same notification
            # queue and hang); the final text arrives as an agentMessage item.
            final = None
            async for note in handle.stream():
                _log(agent.name, note)
                if getattr(note, "method", "") == "item/completed":
                    root = getattr(getattr(getattr(note, "payload", None), "item", None), "root", None)
                    if getattr(root, "type", "") == "agentMessage":
                        final = getattr(root, "text", None) or final
            if final:
                click.echo(f"\n{final}")
    finally:
        try:
            await asyncio.wait_for(codex.close(), timeout=15)
        except (asyncio.TimeoutError, Exception):
            pass


async def run_interactive(agent: Agent, system_prompt: str, cwd: Path, mcp: dict, subagents=()) -> None:
    click.echo(f"Interactive session with '{agent.name}' ({agent.model}). Type 'exit' to leave.")
    if agent.backend == "claude_code":
        await _claude_session(agent, system_prompt, cwd, mcp, subagents)
    else:
        await _codex_session(agent, system_prompt, cwd, mcp, subagents)
