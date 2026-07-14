"""Spawn one agent on its backend (claude_code or codex) and return its final text.

Each agent's credentials are turned into a per-process env here and handed only
to that agent's child — os.environ is never mutated, so a mixed roster can run
concurrently under asyncio.gather. Callers use spawn(); the per-backend helpers
(claude_spawner / codex_spawner) are the shared launch code.
"""

import asyncio
import os
import tempfile
from pathlib import Path

from rich.console import Console

from .roster import Agent

_console = Console()


# ── env (per-agent, never global) ──────────────────────────────────────────────

def _agent_env(agent: Agent, codex_home: Path | None = None) -> dict[str, str]:
    if agent.backend == "claude_code":
        # Override keys only; the SDK merges these into the CLI child it spawns.
        # All three model slots are pinned to agent.model so routing can't cross agents.
        return {k: v for k, v in {
            "ANTHROPIC_BASE_URL": agent.base_url,
            "ANTHROPIC_API_KEY": agent.api_key,
            "ANTHROPIC_AUTH_TOKEN": agent.auth_token,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": agent.model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": agent.model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": agent.model,
        }.items() if v}

    # codex: the child env is replaced wholesale, so start from os.environ and
    # isolate creds + config under a per-agent CODEX_HOME.
    env = dict(os.environ)
    if agent.api_key:
        env["CODEX_API_KEY"] = agent.api_key
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    return env


# ── stream helpers ──────────────────────────────────────────────────────────────

async def _idle_guard(aiter, timeout: float):
    """Yield from an async iterator, raising asyncio.TimeoutError if no item
    arrives within `timeout` seconds (per-item idle, not total runtime)."""
    it = aiter.__aiter__()
    while True:
        try:
            item = await asyncio.wait_for(it.__anext__(), timeout)
        except StopAsyncIteration:
            return
        yield item


# Transient API failures (rate limits, overloads) are retried with a long sleep;
# a hard cap stops a permanently-broken agent (bad key, dead provider) from
# spinning forever inside asyncio.gather.
_MAX_API_RETRIES = 6
_RETRY_SLEEP = 600.0

# Last-run accounting per agent name, harvested by spawn() into .unity/logs/run.jsonl
# so benchmark runs can compare cost across rosters.
_last_run_stats: dict[str, dict] = {}

# Per-agent buffer assembling codex token deltas into whole log lines.
_delta_buf: dict[str, str] = {}


def _ts() -> str:
    import time
    return time.strftime("%H:%M:%S")


def _stop_requested(cwd) -> bool:
    """Safe stop: .unity/stop-requested asks agents to end after the current stream item."""
    from .config import find_unity_dir
    u = find_unity_dir(Path(cwd))
    return u is not None and (u / "stop-requested").exists()


def _tool_log(cwd, name: str, tool: str, detail: str = "") -> None:
    """Per-call tool telemetry → .unity/logs/tools.jsonl (best-effort)."""
    from .config import find_unity_dir
    import json, time
    u = find_unity_dir(Path(cwd)) if cwd else None
    if u is None:
        return
    try:
        logs = u / "logs"
        logs.mkdir(exist_ok=True)
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "agent": name, "tool": tool}
        if detail:
            entry["detail"] = detail[:160]
        with (logs / "tools.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _log(name: str, msg, cwd=None) -> None:
    content = getattr(msg, "content", None)
    if isinstance(content, list):  # AssistantMessage-like
        for b in content:
            text = getattr(b, "text", "")
            if isinstance(text, str) and text.strip():
                _console.print(f"[dim]{_ts()} \\[{name}][/dim] {text[:500]}")
            elif getattr(b, "name", None):
                _console.print(f"[dim]{_ts()} \\[{name}][/dim] [cyan]⚙ {b.name}[/cyan]")
                _tool_log(cwd, name, b.name)
        return
    if type(msg).__name__ == "ResultMessage":
        cost = getattr(msg, "total_cost_usd", None)
        suffix = f" — ${cost:.4f}" if isinstance(cost, (int, float)) else ""
        _console.print(f"[green]{_ts()} \\[{name}] ✓ done{suffix}[/green]")
        return
    method = getattr(msg, "method", None)
    if method:  # codex Notification: typed payload objects
        payload = getattr(msg, "payload", None)
        if method == "item/agentMessage/delta":
            # providers stream token-level deltas; buffer per agent and emit whole lines
            delta = str(getattr(payload, "delta", "") or "")
            buf = _delta_buf.get(name, "") + delta
            while "\n" in buf or len(buf) >= 300:
                cut = buf.find("\n") if "\n" in buf else 300
                line, buf = buf[:cut], buf[cut:].lstrip("\n")
                if line.strip():
                    _console.print(f"[dim]{_ts()} \\[{name}][/dim] {line[:300]}")
            _delta_buf[name] = buf
        elif method == "item/started":
            root = getattr(getattr(payload, "item", None), "root", None)
            rtype = getattr(root, "type", "")
            if rtype == "commandExecution":
                cmd = str(getattr(root, "command", ""))
                _console.print(f"[dim]{_ts()} \\[{name}][/dim] [cyan]⚙ {cmd[:160]}[/cyan]")
                _tool_log(cwd, name, "shell", cmd)
            elif rtype == "mcpToolCall":
                server = str(getattr(root, "server", "") or "")
                tool = str(getattr(root, "tool", "") or "")
                label = f"{server}.{tool}".strip(".")
                _console.print(f"[dim]{_ts()} \\[{name}][/dim] [cyan]⚙ {label}[/cyan]")
                _tool_log(cwd, name, label)
            elif rtype and rtype not in ("agentMessage", "reasoning", "error"):
                _tool_log(cwd, name, rtype)
        elif method in ("error", "turn/failed"):
            payload_err = getattr(payload, "error", None)
            msg_txt = (getattr(payload_err, "message", None) or getattr(payload, "message", None)
                       or str(payload)[:200])
            _console.print(f"[red]{_ts()} \\[{name}] ✗ {method}: {str(msg_txt)[:300]}[/red]")
        elif method == "turn/completed":
            tail = _delta_buf.pop(name, "")
            if tail.strip():
                _console.print(f"[dim]{_ts()} \\[{name}][/dim] {tail[:300]}")
            _console.print(f"[green]{_ts()} \\[{name}] ✓ turn complete[/green]")
        return
    text = getattr(msg, "text", None) or getattr(msg, "message", None)
    if text:
        _console.print(f"[dim]{_ts()} \\[{name}][/dim] {str(text)[:500]}")


# ── backends ────────────────────────────────────────────────────────────────────

async def claude_spawner(agent: Agent, system_prompt: str, prompt: str, cwd: Path,
                         mcp_servers: dict, *, permission: str = "bypassPermissions",
                         idle_timeout: float = 600.0, subagents=()) -> str | None:
    from claude_agent_sdk import query, ClaudeAgentOptions, AgentDefinition

    agents_def = {
        s["name"]: AgentDefinition(description=s["description"], prompt=s["prompt"], tools=s["tools"])
        for s in subagents
    }
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers=mcp_servers,
        agents=agents_def,
        cwd=str(cwd),
        permission_mode=permission,
        model=agent.model,
        max_budget_usd=agent.budget,
        env=_agent_env(agent),
    )
    for attempt in range(_MAX_API_RETRIES):
        try:
            final = None
            async for msg in _idle_guard(query(prompt=prompt, options=options), idle_timeout):
                _log(agent.name, msg, cwd)
                if type(msg).__name__ == "ResultMessage":
                    final = getattr(msg, "result", None)
                    _last_run_stats[agent.name] = {
                        "cost_usd": getattr(msg, "total_cost_usd", None),
                        "num_turns": getattr(msg, "num_turns", None),
                    }
                if _stop_requested(cwd):
                    # Safe stop: wind down at the next stream item instead of being killed
                    # mid-write; abandoning the iterator disconnects the SDK client cleanly.
                    _console.print(f"[yellow]{_ts()} \\[{agent.name}] safe stop — ending turn[/yellow]")
                    return final
            return final
        except Exception as e:
            if attempt == _MAX_API_RETRIES - 1:
                raise
            _log(agent.name, f"API Error ({e}), retrying in 10 minutes...")
            await asyncio.sleep(_RETRY_SLEEP)


def _write_codex_config(home: Path, agent: Agent, mcp_servers: dict,
                        writable_root: Path | None = None) -> str | None:
    """Seed CODEX_HOME/config.toml with a custom provider (if base_url), MCP servers,
    and workspace-write sandbox tuning. Returns the provider id to pass as
    model_provider, or None for the default openai provider."""
    home.mkdir(parents=True, exist_ok=True)
    # No api_key -> ride the user's Codex subscription: copy their login into this
    # agent's isolated CODEX_HOME.
    if not agent.api_key:
        user_auth = Path.home() / ".codex" / "auth.json"
        if user_auth.exists():
            import shutil
            shutil.copy2(user_auth, home / "auth.json")
    lines: list[str] = []
    # Unity agents run under workspace_write: they need network (lake, arXiv, MCP)
    # and, from a worktree cwd, write access to the main project (.unity/dag.json).
    lines += ["[sandbox_workspace_write]", "network_access = true"]
    if writable_root is not None:
        lines.append(f'writable_roots = ["{writable_root}"]')
    lines.append("")
    provider = None
    if agent.base_url:
        provider = "unity"
        lines += [
            "[model_providers.unity]",
            'name = "unity"',
            f'base_url = "{agent.base_url}"',
            'env_key = "CODEX_API_KEY"',
            # codex-cli >= 0.132 dropped wire_api="chat"; providers must speak the
            # OpenAI Responses API (vLLM and FreeInference both serve /v1/responses).
            'wire_api = "responses"',
            "",
        ]
    for name, cfg in (mcp_servers or {}).items():
        lines.append(f"[mcp_servers.{name}]")
        if cfg.get("command"):
            lines.append(f'command = "{cfg["command"]}"')
            if cfg.get("args"):
                args = ", ".join(f'"{a}"' for a in cfg["args"])
                lines.append(f"args = [{args}]")
        elif cfg.get("url"):
            lines.append(f'url = "{cfg["url"]}"')
        lines.append("")
        if cfg.get("env"):
            lines.append(f"[mcp_servers.{name}.env]")
            for k, v in cfg["env"].items():
                lines.append(f'{k} = "{v}"')
            lines.append("")
    (home / "config.toml").write_text("\n".join(lines))
    return provider


def _write_codex_agents(home: Path, subagents) -> None:
    """Register subagents as codex custom-agent TOMLs under CODEX_HOME/agents/."""
    if not subagents:
        return
    adir = home / "agents"
    adir.mkdir(parents=True, exist_ok=True)
    for s in subagents:
        body = s["prompt"].replace('"""', '\\"\\"\\"')
        toml = (
            f'name = "{s["name"]}"\n'
            f'description = "{s["description"]}"\n'
            f'developer_instructions = """\n{body}\n"""\n'
        )
        (adir / f'{s["name"]}.toml').write_text(toml)


# codex >=0.117 does not expose MCP tools to custom Responses-API providers
# (openai/codex#19871, #23186, #26977) — codex agents call them via `unity mcp` instead.
_CODEX_MCP_NOTE = (
    "\n\nIMPORTANT — MCP tools on this backend: your model does NOT receive MCP tools natively. "
    "Every MCP tool in this prompt (forum_*, ledger_*, lean_*, axle, aristotle) is instead called "
    "through the shell:\n"
    "    unity mcp <server> <tool> '<json-args>'\n"
    "Examples:\n"
    "    unity mcp unity-forum forum_brief '{\"author\": \"<your agent name>\"}'\n"
    "    unity mcp unity-forum forum_claim '{\"chunk\": \"chunk-1\", \"author\": \"<you>\", \"strategy\": \"...\"}'\n"
    "    unity mcp lean-lsp lean_goal '{\"file_path\": \"...\", \"line\": 12}'\n"
    "Servers: unity-forum (all forum_*/ledger_* tools), lean-lsp, axle and aristotle when "
    "configured. Read every forum/tool instruction in this prompt as 'run it via unity mcp'. "
    "The forum contract is not optional on this backend — use it through this command.\n")


async def codex_spawner(agent: Agent, system_prompt: str, prompt: str, cwd: Path,
                        mcp_servers: dict, *, permission: str = "bypassPermissions",
                        idle_timeout: float = 600.0, subagents=()) -> str | None:
    from openai_codex import AsyncCodex, CodexConfig, Sandbox

    system_prompt = system_prompt + _CODEX_MCP_NOTE

    home = Path(tempfile.mkdtemp(prefix="unity-codex-"))
    # from a worktree cwd, the agent still needs write access to the main project (.unity/)
    from .config import find_unity_dir
    unity_dir = find_unity_dir(Path(cwd))
    provider = _write_codex_config(home, agent, mcp_servers,
                                   writable_root=unity_dir.parent if unity_dir else None)
    _write_codex_agents(home, subagents)
    # bypassPermissions ~ full_access; anything more restrictive still needs to edit files.
    sandbox = Sandbox.full_access if permission == "bypassPermissions" else Sandbox.workspace_write

    for attempt in range(_MAX_API_RETRIES):
        codex = AsyncCodex(config=CodexConfig(cwd=str(cwd), env=_agent_env(agent, home)))
        try:
            # login_api_key is OpenAI-official auth only; custom providers (base_url set)
            # authenticate via the provider's env_key (CODEX_API_KEY in _agent_env).
            if agent.api_key and not agent.base_url:
                await codex.login_api_key(agent.api_key)
            thread = await codex.thread_start(
                model=agent.model,
                model_provider=provider,
                sandbox=sandbox,
                base_instructions=system_prompt,
                cwd=str(cwd),
            )
            handle = await thread.turn(prompt)
            # handle.run() and handle.stream() are competing consumers of one notification
            # queue — using both (in any order) starves one and hangs. Consume ONLY the
            # stream and assemble the final response from agentMessage items ourselves.
            final = None
            usage = None
            async for note in _idle_guard(handle.stream(), idle_timeout):
                _log(agent.name, note, cwd)
                if _stop_requested(cwd):
                    _console.print(f"[yellow]{_ts()} \\[{agent.name}] safe stop — ending turn[/yellow]")
                    break
                method = getattr(note, "method", "") or ""
                payload = getattr(note, "payload", None)
                if method == "item/completed":
                    root = getattr(getattr(payload, "item", None), "root", None)
                    if getattr(root, "type", "") == "agentMessage":
                        final = getattr(root, "text", None) or final
                elif method == "thread/tokenUsage/updated":
                    total = getattr(getattr(payload, "token_usage", None), "total", None)
                    if total is not None:
                        usage = {k: getattr(total, k) for k in
                                 ("input_tokens", "cached_input_tokens", "output_tokens",
                                  "reasoning_output_tokens", "total_tokens") if hasattr(total, k)}
            _last_run_stats[agent.name] = {"cost_usd": None, "usage": usage}
            return final
        except Exception as e:
            if attempt == _MAX_API_RETRIES - 1:
                raise
            _log(agent.name, f"API Error ({e}), retrying in 10 minutes...")
            await asyncio.sleep(_RETRY_SLEEP)
        finally:
            # close() can hang after an aborted turn; don't let cleanup wedge the agent.
            try:
                await asyncio.wait_for(codex.close(), timeout=15)
            except (asyncio.TimeoutError, Exception):
                pass


# ── dispatch ──────────────────────────────────────────────────────────────────

def _write_run_log(agent: Agent, cwd: Path, seconds: float) -> None:
    """Append per-agent run accounting to .unity/logs/run.jsonl (best-effort)."""
    from .config import find_unity_dir
    import json, time
    unity = find_unity_dir(Path(cwd))
    if unity is None:
        return
    try:
        logs = unity / "logs"
        logs.mkdir(exist_ok=True)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": agent.name, "model": agent.model, "backend": agent.backend,
            "seconds": round(seconds, 1),
            **_last_run_stats.pop(agent.name, {}),
        }
        with (logs / "run.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


async def spawn(agent: Agent, system_prompt: str, prompt: str, cwd: Path,
                mcp_servers: dict, *, permission: str = "bypassPermissions",
                idle_timeout: float = 600.0, subagents=()) -> str | None:
    backend = claude_spawner if agent.backend == "claude_code" else codex_spawner
    import time
    t0 = time.monotonic()
    try:
        return await backend(agent, system_prompt, prompt, cwd, mcp_servers,
                             permission=permission, idle_timeout=idle_timeout, subagents=subagents)
    finally:
        _write_run_log(agent, cwd, time.monotonic() - t0)
