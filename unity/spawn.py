"""Spawn one agent on its backend (claude_code or codex) and return its final text.

Each agent's credentials are turned into a per-process env here and handed only
to that agent's child — os.environ is never mutated, so a mixed roster can run
concurrently under asyncio.gather. Callers use spawn(); the per-backend helpers
(claude_spawner / codex_spawner) are the shared launch code.
"""

import asyncio
import os
import signal
import sys
import tempfile
from pathlib import Path

from rich.console import Console

from .roster import Agent

_console = Console()


# ── env (per-agent, never global) ──────────────────────────────────────────────

def _agent_env(
    agent: Agent,
    codex_home: Path | None = None,
    env_overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    if agent.backend == "claude_code":
        # Override keys only; the SDK merges these into the CLI child it spawns.
        # All three model slots are pinned to agent.model so routing can't cross agents.
        env = {k: v for k, v in {
            "ANTHROPIC_BASE_URL": agent.base_url,
            "ANTHROPIC_API_KEY": agent.api_key,
            "ANTHROPIC_AUTH_TOKEN": agent.auth_token,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": agent.model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": agent.model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": agent.model,
            "UNITY_AGENT_NAME": agent.name,
        }.items() if v}
        env.update(env_overrides or {})
        return env

    # codex: the child env is replaced wholesale, so start from os.environ and
    # isolate creds + config under a per-agent CODEX_HOME.
    env = dict(os.environ)
    if agent.api_key:
        env["CODEX_API_KEY"] = agent.api_key
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    env["UNITY_AGENT_NAME"] = agent.name
    env.update(env_overrides or {})
    return env


def _process_group_wrapper(executable: Path, directory: Path, label: str) -> tuple[Path, Path]:
    """Wrap a CLI so its entire process tree has a solve-owned process group."""
    wrapper = directory / f"{label}-group-wrapper"
    pid_file = directory / f"{label}-group.pid"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "os.setsid()\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        f"os.execv({str(executable)!r}, [{str(executable)!r}, *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o700)
    return wrapper, pid_file


async def _terminate_process_group(pid_file: Path | None) -> None:
    if pid_file is None or os.name != "posix":
        return
    try:
        pgid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return
    for sig, delay in ((signal.SIGTERM, 0.25), (signal.SIGKILL, 0.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            break
        if delay:
            await asyncio.sleep(delay)
    pid_file.unlink(missing_ok=True)


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


# Transient API failures are retried; the cap follows the MAX_ATTEMPTS env flag
# (blank/unset = retry indefinitely). Rate limits get a short backoff; other
# failures (overloads, dead providers) wait longer between attempts.
def _max_retries() -> float:
    return float(os.getenv("MAX_ATTEMPTS") or "inf")


def _retry_sleep(exc) -> float:
    s = str(exc).lower()
    if "429" in s or "rate" in s or "too many" in s:
        return 60.0
    return 600.0


# Signatures of permanent failures (dead CLI, bad/exhausted credentials): retrying
# these forever just burns wall-clock — give up after two attempts regardless of
# the MAX_ATTEMPTS-driven cap for transient errors.
_PERMANENT = ("exit code 1", "usage limit", "upgrade to", "authentication", "unauthorized",
              "401", "invalid api key", "login", "requires a newer version")


def _give_up(exc, attempt: int) -> bool:
    s = str(exc).lower()
    if any(sig in s for sig in _PERMANENT):
        return attempt >= 2
    return attempt >= _max_retries()

# Last-run accounting per agent name, harvested by spawn() into .unity/logs/run.jsonl
# so benchmark runs can compare cost across rosters.
_last_run_stats: dict[str, dict] = {}

# Per-agent buffer assembling streamed token deltas into whole log lines.
_delta_buf: dict[str, str] = {}


def _emit_delta(name: str, delta: str) -> None:
    buf = _delta_buf.get(name, "") + delta
    while "\n" in buf or len(buf) >= 300:
        cut = buf.find("\n") if "\n" in buf else 300
        line, buf = buf[:cut], buf[cut:].lstrip("\n")
        if line.strip():
            _console.print(f"[dim]{_ts()} \\[{name}][/dim] {line[:300]}")
    _delta_buf[name] = buf


def _flush_delta(name: str) -> None:
    tail = _delta_buf.pop(name, "")
    if tail.strip():
        _console.print(f"[dim]{_ts()} \\[{name}][/dim] {tail[:300]}")


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
                         idle_timeout: float = 600.0, subagents=(),
                         env_overrides: dict[str, str] | None = None,
                         own_process_group: bool = False) -> str | None:
    from claude_agent_sdk import query, ClaudeAgentOptions, AgentDefinition
    import shutil

    cli_path = None
    pid_file = None
    if own_process_group and os.name == "posix":
        from claude_agent_sdk._internal.transport import subprocess_cli
        bundled = Path(subprocess_cli.__file__).parent.parent.parent / "_bundled" / "claude"
        real_cli = bundled if bundled.is_file() else Path(shutil.which("claude") or "")
        if real_cli.is_file():
            group_dir = Path(tempfile.mkdtemp(prefix="unity-claude-group-"))
            cli_path, pid_file = _process_group_wrapper(real_cli, group_dir, "claude")

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
        env=_agent_env(agent, env_overrides=env_overrides),
        cli_path=cli_path,
    )
    attempt = 0
    while True:
        attempt += 1
        try:
            final = None
            stream = query(prompt=prompt, options=options)
            try:
                async for msg in _idle_guard(stream, idle_timeout):
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
            finally:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()
            return final
        except Exception as e:
            if _give_up(e, attempt):
                raise
            wait = _retry_sleep(e)
            _log(agent.name, f"API Error ({e}), retrying in {int(wait)}s...")
            await _terminate_process_group(pid_file)
            await asyncio.sleep(wait)
        finally:
            await _terminate_process_group(pid_file)


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
    "    unity mcp unity-forum register_strategy '{\"decl\": \"My.theorem\", \"author\": \"<you>\", \"description\": \"...\", \"strategy_family\": \"core_method\"}'\n"
    "    unity mcp unity-forum claim_strategy '{\"strategy_id\": \"strategy-...\", \"author\": \"<you>\"}'\n"
    "    unity mcp lean-lsp lean_goal '{\"file_path\": \"...\", \"line\": 12}'\n"
    "Servers: unity-forum (all forum_*/ledger_* tools), lean-lsp, axle and aristotle when "
    "configured. Read every forum/tool instruction in this prompt as 'run it via unity mcp'. "
    "The forum contract is not optional on this backend — use it through this command.\n")

_SOLVE_CODEX_MCP_NOTE = (
    "\n\nIMPORTANT — solve MCP tools on this backend: your model does NOT receive MCP tools "
    "natively. Every solve Forum, Lean, Axle, and Aristotle tool in this prompt is called "
    "through the shell:\n"
    "    unity mcp <server> <tool> '<json-args>'\n"
    "Examples:\n"
    "    unity mcp unity-forum solve_brief '{\"author\": \"<your agent name>\"}'\n"
    "    unity mcp unity-forum register_strategy '{\"target\": \"formal-task-id\", \"author\": \"<you>\", \"description\": \"...\", \"strategy_family\": \"core_method\"}'\n"
    "    unity mcp unity-forum claim_strategy '{\"strategy_id\": \"strategy-...\", \"author\": \"<you>\"}'\n"
    "    unity mcp unity-forum finalize_formalization '{\"strategy_id\": \"strategy-...\", \"task_id\": \"formal-task-id\", \"author\": \"<you>\"}'\n"
    "    unity mcp lean-lsp lean_goal '{\"file_path\": \"...\", \"line\": 12}'\n"
    "Servers: unity-forum (solve tools), lean-lsp, axle and aristotle when configured. Read "
    "every tool instruction in this prompt as 'run it via unity mcp'. The solve Forum contract "
    "is not optional on this backend. Use `target`, never prove's `decl` argument, when "
    "registering a solve strategy.\n"
)


def _codex_mcp_note(profile: str) -> str:
    return _SOLVE_CODEX_MCP_NOTE if profile == "solve" else _CODEX_MCP_NOTE


_CODEX_INTERRUPT_REQUEST_TIMEOUT = 10.0
_CODEX_INTERRUPT_DRAIN_TIMEOUT = 20.0
_CODEX_CLOSE_TIMEOUT = 15.0


async def _codex_notifications(
    handle, codex, idle_timeout: float, stream_started: asyncio.Event
):
    """Yield Codex notifications without cancelling its blocking queue waiter.

    AsyncTurnHandle.stream delegates each queue read through asyncio.to_thread.
    Shielding the __anext__ task keeps outer cancellation and idle timeouts from
    abandoning that executor thread. On either path, close the transport while
    the stream is still registered, wait for the queue read to wake, and only then
    close the generator (which unregisters the queue).
    """
    stream = handle.stream()
    pending = None
    try:
        while True:
            pending = asyncio.create_task(stream.__anext__())
            if not stream_started.is_set():
                # Let AsyncTurnHandle.stream synchronously register its queue before
                # an already-set interrupt event is allowed to close the transport.
                await asyncio.sleep(0)
                stream_started.set()
            try:
                note = await asyncio.wait_for(
                    asyncio.shield(pending), timeout=idle_timeout
                )
            except StopAsyncIteration:
                pending = None
                return
            pending = None
            yield note
    finally:
        if pending is not None and not pending.done():
            try:
                await asyncio.wait_for(codex.close(), timeout=_CODEX_CLOSE_TIMEOUT)
            except (asyncio.TimeoutError, Exception):
                pass
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()


async def codex_spawner(agent: Agent, system_prompt: str, prompt: str, cwd: Path,
                        mcp_servers: dict, *, permission: str = "bypassPermissions",
                        idle_timeout: float = 600.0, subagents=(),
                        interrupt_event: asyncio.Event | None = None,
                        env_overrides: dict[str, str] | None = None,
                        own_process_group: bool = False,
                        mcp_profile: str = "prove") -> str | None:
    from openai_codex import AsyncCodex, CodexConfig, Sandbox

    system_prompt = system_prompt + _codex_mcp_note(mcp_profile)

    home = Path(tempfile.mkdtemp(prefix="unity-codex-"))
    # from a worktree cwd, the agent still needs write access to the main project (.unity/)
    from .config import find_unity_dir
    unity_dir = find_unity_dir(Path(cwd))
    provider = _write_codex_config(home, agent, mcp_servers,
                                   writable_root=unity_dir.parent if unity_dir else None)
    _write_codex_agents(home, subagents)
    # bypassPermissions ~ full_access; anything more restrictive still needs to edit files.
    sandbox = Sandbox.full_access if permission == "bypassPermissions" else Sandbox.workspace_write

    # Prefer the user's installed codex CLI (kept current by its own updater) over the
    # SDK's pinned bundled binary — newest models often require a newer runtime.
    import shutil as _sh
    codex_bin = _sh.which("codex")
    pid_file = None
    if own_process_group and os.name == "posix":
        if codex_bin is None:
            try:
                from codex_cli_bin import bundled_codex_path
                codex_bin = str(bundled_codex_path())
            except ImportError:
                pass
        if codex_bin:
            wrapped, pid_file = _process_group_wrapper(Path(codex_bin), home, "codex")
            codex_bin = str(wrapped)
    attempt = 0
    while True:
        attempt += 1
        agent_env = _agent_env(agent, home, env_overrides)
        cfg = (CodexConfig(cwd=str(cwd), env=agent_env, codex_bin=codex_bin)
               if codex_bin else CodexConfig(cwd=str(cwd), env=agent_env))
        codex = AsyncCodex(config=cfg)
        final = None
        interrupt_task = None
        turn_finished = asyncio.Event()
        stream_started = asyncio.Event()
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

            async def interrupt_turn() -> None:
                """Stop a Codex turn without cancelling its thread-backed queue wait.

                openai-codex implements next_turn_notification with asyncio.to_thread
                around a blocking Queue.get(). Cancelling that await abandons the worker
                thread. Ask the app server to interrupt instead, then leave the stream
                registered until it completes or transport shutdown wakes the waiter.
                """
                assert interrupt_event is not None
                await interrupt_event.wait()
                await stream_started.wait()
                try:
                    await asyncio.wait_for(
                        handle.interrupt(), timeout=_CODEX_INTERRUPT_REQUEST_TIMEOUT
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.wait_for(codex.close(), timeout=_CODEX_CLOSE_TIMEOUT)
                    return

                try:
                    await asyncio.wait_for(
                        turn_finished.wait(), timeout=_CODEX_INTERRUPT_DRAIN_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    # Interrupt was acknowledged but no terminal event arrived. Close
                    # while the stream queue is still registered so fail_all() wakes its
                    # blocking notification waiter before the generator unregisters it.
                    await asyncio.wait_for(codex.close(), timeout=_CODEX_CLOSE_TIMEOUT)

            if interrupt_event is not None:
                interrupt_task = asyncio.create_task(
                    interrupt_turn(), name=f"codex-interrupt:{agent.name}"
                )

            # handle.run() and handle.stream() are competing consumers of one notification
            # queue — using both (in any order) starves one and hangs. Consume ONLY the
            # stream and assemble the final response from agentMessage items ourselves.
            usage = None
            async for note in _codex_notifications(
                handle, codex, idle_timeout, stream_started
            ):
                _log(agent.name, note, cwd)
                if _stop_requested(cwd):
                    _console.print(f"[yellow]{_ts()} \\[{agent.name}] safe stop — ending turn[/yellow]")
                    break
                method = getattr(note, "method", "") or ""
                payload = getattr(note, "payload", None)
                if method == "turn/completed":
                    turn_finished.set()
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
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if interrupt_event is not None and interrupt_event.is_set():
                return final
            if _give_up(e, attempt):
                raise
            wait = _retry_sleep(e)
            _log(agent.name, f"API Error ({e}), retrying in {int(wait)}s...")
            await asyncio.sleep(wait)
        finally:
            turn_finished.set()
            if interrupt_task is not None:
                interrupt_task.cancel()
                await asyncio.gather(interrupt_task, return_exceptions=True)
            # close() can hang after an aborted turn; don't let cleanup wedge the agent.
            try:
                await asyncio.wait_for(codex.close(), timeout=_CODEX_CLOSE_TIMEOUT)
            except (asyncio.TimeoutError, Exception):
                pass
            await _terminate_process_group(pid_file)


async def antigravity_spawner(agent: Agent, system_prompt: str, prompt: str, cwd: Path,
                              mcp_servers: dict, *, permission: str = "bypassPermissions",
                              idle_timeout: float = 600.0, subagents=(),
                              env_overrides: dict[str, str] | None = None,
                              own_process_group: bool = False,
                              mcp_profile: str = "prove") -> str | None:
    """Google Antigravity backend: drives the user's installed `agy` CLI in print mode
    (subscription auth; serves both the Gemini pool and the Claude/GPT pool). MCP tools
    reach the model through the `unity mcp` shell bridge, like codex."""
    import json as _json
    import shutil as _sh
    agy = _sh.which("agy")
    if agy is None:
        raise RuntimeError("antigravity backend needs the `agy` CLI installed and logged in "
                           "(https://antigravity.google)")
    from .config import find_unity_dir
    unity_dir = find_unity_dir(Path(cwd))
    full = system_prompt + _codex_mcp_note(mcp_profile) + "\n\n---\n\nTASK:\n" + prompt
    cmd = [agy, "--print", full, "--model", agent.model, "--output-format", "stream-json",
           "--dangerously-skip-permissions", "--print-timeout", "72h"]
    if unity_dir is not None:  # worktree cwd still needs to write the main project's .unity/
        cmd += ["--add-dir", str(unity_dir.parent)]

    attempt = 0
    while True:
        attempt += 1
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, stdin=asyncio.subprocess.DEVNULL,
            env=_agent_env(agent, env_overrides=env_overrides),
            start_new_session=own_process_group and os.name == "posix")

        async def _lines():
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                yield line

        try:
            final = None
            usage = None
            failed = None
            async for raw in _idle_guard(_lines(), idle_timeout):
                try:
                    e = _json.loads(raw)
                except _json.JSONDecodeError:
                    continue
                ev = e.get("event")
                if ev == "step_update":
                    su = e.get("step_update", {})
                    st = su.get("step_type", "")
                    if st == "agent_response" and su.get("text_delta"):
                        _emit_delta(agent.name, su["text_delta"])
                    elif (su.get("state") == "DONE"
                          and st not in ("agent_response", "checkpoint", "user_input", "unknown", "")):
                        _console.print(f"[dim]{_ts()} \\[{agent.name}][/dim] [cyan]⚙ {st[:80]}[/cyan]")
                        _tool_log(cwd, agent.name, st)
                elif ev == "result":
                    r = e.get("result", {})
                    final = r.get("response") or final
                    usage = r.get("usage")
                    if r.get("status") not in (None, "SUCCESS"):
                        failed = r.get("status")
                        _console.print(f"[red]{_ts()} \\[{agent.name}] ✗ agy result: {failed}[/red]")
                if _stop_requested(cwd):
                    _console.print(f"[yellow]{_ts()} \\[{agent.name}] safe stop — ending turn[/yellow]")
                    if own_process_group and os.name == "posix":
                        os.killpg(proc.pid, signal.SIGTERM)
                    else:
                        proc.terminate()
                    break
            rc = await proc.wait()
            if failed:
                raise RuntimeError(f"agy turn failed: {failed}")
            if final is None and rc not in (0, -15):
                raise RuntimeError(f"agy exited with code {rc} and no response")
            _flush_delta(agent.name)
            _console.print(f"[green]{_ts()} \\[{agent.name}] ✓ turn complete[/green]")
            _last_run_stats[agent.name] = {"cost_usd": None, "usage": usage}
            return final
        except Exception as e:
            if proc.returncode is None:
                proc.kill()
            if _give_up(e, attempt):
                raise
            wait = _retry_sleep(e)
            _log(agent.name, f"API Error ({e}), retrying in {int(wait)}s...")
            await asyncio.sleep(wait)
        finally:
            if proc.returncode is None:
                if own_process_group and os.name == "posix":
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    proc.kill()
                await proc.wait()


# ── dispatch ──────────────────────────────────────────────────────────────────

def _write_run_log(
    agent: Agent,
    cwd: Path,
    seconds: float,
    log_context: dict | None = None,
) -> None:
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
        if log_context:
            entry["context"] = dict(log_context)
        with (logs / "run.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


async def spawn(agent: Agent, system_prompt: str, prompt: str, cwd: Path,
                mcp_servers: dict, *, permission: str = "bypassPermissions",
                idle_timeout: float = 600.0, subagents=(),
                interrupt_event: asyncio.Event | None = None,
                log_context: dict | None = None,
                env_overrides: dict[str, str] | None = None,
                own_process_group: bool = False,
                mcp_profile: str = "prove") -> str | None:
    backend = {"claude_code": claude_spawner, "codex": codex_spawner,
               "antigravity": antigravity_spawner}[agent.backend]
    import time
    t0 = time.monotonic()
    try:
        kwargs = {
            "permission": permission,
            "idle_timeout": idle_timeout,
            "subagents": subagents,
            "env_overrides": env_overrides,
            "own_process_group": own_process_group,
        }
        if agent.backend == "codex":
            kwargs["interrupt_event"] = interrupt_event
            kwargs["mcp_profile"] = mcp_profile
        elif agent.backend == "antigravity":
            kwargs["mcp_profile"] = mcp_profile
        return await backend(agent, system_prompt, prompt, cwd, mcp_servers, **kwargs)
    finally:
        _write_run_log(agent, cwd, time.monotonic() - t0, log_context)
