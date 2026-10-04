"""Bump-owned agent launcher, copied from the working formalization backend.

Each agent's credentials are turned into a per-process env here and handed only
to that agent's child — os.environ is never mutated, so a mixed roster can run
concurrently under asyncio.gather. Callers use this module's spawn(); its backend
helpers are independent copies, not the solve/prove launch implementation.
"""

import asyncio
import json
import os
import signal
import sys
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from rich.console import Console

from .roster import Agent

_console = Console()

CompletionCallback = Callable[[str | None], Awaitable[str | None]]


class _UnsuccessfulTurnError(RuntimeError):
    """A terminal session failure, not a transient transport exception to retry."""


class _CompletionCallbackError(Exception):
    """Keep controller failures out of the backend's transient API retry loop."""

    def __init__(self, error: Exception):
        self.error = error
        super().__init__(str(error))


async def _completion_feedback(callback: CompletionCallback, final: str | None,
                               cwd: Path) -> str | None:
    if _stop_requested(cwd):
        return None
    try:
        feedback = await callback(final)
        if feedback is not None and not isinstance(feedback, str):
            raise TypeError("completion feedback must be a string or None")
    except Exception as exc:
        raise _CompletionCallbackError(exc) from exc
    return None if _stop_requested(cwd) else feedback


def _worktree_write_roots(cwd: Path) -> tuple[Path, ...]:
    """Grant worker source access, plus the shared paths used by existing tools.

    Main-checkout phases retain their existing permissions. Resolve .unity and
    package links: granting their parent would also grant writes to main source.
    This isolates source edits, not mutually distrustful agents' shared state.
    """
    import shutil
    import subprocess
    from .config import find_unity_dir

    cwd = Path(cwd).resolve()
    unity = find_unity_dir(cwd)
    if unity is None or unity.resolve().parent == cwd:
        return ()
    unity = unity.resolve()
    git = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
        cwd=cwd, capture_output=True, text=True, check=True,
    )
    git_dir, common = (Path(line).resolve() for line in git.stdout.splitlines())
    # Candidate commits share Git objects/refs/maintenance locks with main.
    # Grant metadata, never the enclosing source checkout.
    roots = [cwd, unity, git_dir, common]
    packages = cwd / ".lake" / "packages"
    if packages.is_dir():
        roots.append(packages.resolve())
    # `unity mcp` uses uvx for local services; keep its existing global cache.
    uv = shutil.which("uv")
    if uv:
        cache = subprocess.run([uv, "cache", "dir"], capture_output=True, text=True, check=True)
        roots.append(Path(cache.stdout.strip()).resolve())
    if any(path != cwd and cwd.is_relative_to(path) for path in roots):
        raise ValueError("bump shared write access cannot include the source checkout")
    return tuple(dict.fromkeys(roots))


def _worktree_prompt(cwd: Path) -> str:
    return (
        f"\n\nWORKTREE WRITE POLICY: Your source workspace is exactly {Path(cwd).resolve()}. "
        "Use this directory as workdir/cwd for every shell command and Lean tool. "
        "Keep all source edits and scratch proofs here. Main and other agents' worktrees "
        "are read-only to you, including through absolute paths, ../ paths, and symlinks. "
        "Shared runtime/build directories exist for Unity tools, not as alternate source "
        "workspaces. Use the Forum tools for shared state, candidate submission, and sync; "
        "Unity alone integrates candidates into main. If a write is denied, correct its "
        "destination or report the blocker; do not disable or bypass the restriction."
    )


# ── env (per-agent, never global) ──────────────────────────────────────────────

_BUMP_MCP_ENV_KEYS = (
    "PATH",
    "UNITY_REAL_LAKE",
    "UNITY_BUMP_PROJECT_ROOT",
    "UNITY_BUMP_TASK_ID",
    "UNITY_BUMP_PROFILE",
    "UNITY_BUMP_DRAFT_PATH",
    "UNITY_AGENT_NAME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "UV_TOOL_DIR",
    "UV_TOOL_BIN_DIR",
    "LAKE_CACHE_DIR",
)

_BUMP_UVX_ENV_KEYS = (
    "PATH", "TMPDIR", "TMP", "TEMP", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR",
)


def bump_mcp_with_runtime_env(
    servers: dict, environ: Mapping[str, str],
) -> dict:
    """Bind bump's local services to a worker's non-secret runtime.

    Stdio SDKs may inherit the guarded PATH without its companion variables.
    They also omit UV_TOOL_DIR, causing uvx to write to the read-only global
    tools install. Explicitly forward worker scratch paths to both uvx services.
    Never forward the whole worker environment (which contains credentials) or
    alter shared mappings; retain each service's explicitly configured keys.
    """
    result = servers
    for name, env_keys in (
        ("lean-lsp", _BUMP_MCP_ENV_KEYS),
        ("axle", _BUMP_UVX_ENV_KEYS),
        ("unity-forum", _BUMP_MCP_ENV_KEYS),
    ):
        server = servers.get(name)
        if not server or not server.get("command"):
            continue
        server_env = {
            key: value for key, value in (server.get("env") or {}).items()
            if key not in env_keys
        }
        server_env.update({key: environ[key] for key in env_keys if key in environ})
        result = {**result, name: {**server, "env": server_env}}
    return result


# Audited names from BUMP_{LEAN,AXLE,ARISTOTLE}_TOOLS.md. A newly installed
# service tool is not automatically authorized. Forum names come from its actual
# phase registration below, rather than a second copy of that contract.
_BUMP_LEAN_INSPECTION_TOOLS = (
    "lean_goal", "lean_term_goal", "lean_diagnostic_messages", "lean_file_outline",
    "lean_hover_info", "lean_completions", "lean_declaration_file", "lean_references",
    "lean_local_search", "lean_leansearch", "lean_loogle", "lean_leanfinder",
    "lean_state_search", "lean_hammer_premise", "lean_code_actions", "lean_run_code",
    "lean_verify", "lean_minimal_hypotheses", "lean_profile_proof", "lean_get_widgets",
    "lean_get_widget_source",
)
_BUMP_AXLE_INSPECTION_TOOLS = (
    "check", "verify_proof", "highlight", "extract_decls", "list_environments",
    "read_share_url", "read_docs",
)
_BUMP_AXLE_PROOF_TOOLS = (
    "repair_proofs", "simplify_theorems", "disprove", "merge", "rename", "normalize",
    "theorem2lemma", "theorem2sorry", "have2lemma", "have2sorry", "sorry2lemma",
    "share_url",
)
_BUMP_ARISTOTLE_INSPECTION_TOOLS = (
    "aristotle_status", "aristotle_wait", "aristotle_list",
)


def _bump_codex_tool_policy(mcp_servers: dict, phase: str) -> dict[str, tuple[str, ...]]:
    """Authorize only this phase's existing public tools on known transports.

    Codex MCP approval is independent of shell approval_policy. Preapproval here
    is scoped to the pipeline API already delegated to this worker, not to an
    arbitrary server or any tools it might advertise in a future release.
    """
    from .forum.bump_server import PROFILE_TOOLS

    if phase not in PROFILE_TOOLS:
        raise ValueError(f"unknown bump MCP phase {phase!r}")
    catalogs = {
        "unity-forum": tuple(tool.__name__ for tool in PROFILE_TOOLS[phase]),
        "lean-lsp": _BUMP_LEAN_INSPECTION_TOOLS
            + (("lean_multi_attempt",) if phase == "formalizing" else ()),
        "axle": _BUMP_AXLE_INSPECTION_TOOLS
            + (_BUMP_AXLE_PROOF_TOOLS if phase == "formalizing" else ()),
        "aristotle": _BUMP_ARISTOTLE_INSPECTION_TOOLS
            + (("aristotle_submit", "aristotle_result", "aristotle_cancel")
               if phase == "formalizing" else ()),
    }
    # lean_build is intentionally not authorized: workers must not initiate the
    # controller's project-wide build through either native MCP or the bridge.
    transports = {
        "lean-lsp": ("uvx", ["lean-lsp-mcp"]),
        "axle": ("uvx", ["--from", "axiom-axle-mcp", "axle-mcp-server"]),
        "aristotle": (sys.executable, ["-m", "unity.aristotle"]),
    }
    result = {}
    for name, cfg in mcp_servers.items():
        if name not in catalogs or cfg.get("url"):
            raise ValueError(f"unrecognized bump MCP transport {name!r}")
        args = cfg.get("args", [])
        if name == "unity-forum":
            trusted = (
                cfg.get("command") == sys.executable
                and isinstance(args, list) and len(args) == 8
                and args[:3] == ["-m", "unity.forum.bump_server", "--forum-dir"]
                and args[4] == "--project-root" and args[6:] == ["--profile", phase]
            )
        else:
            command, expected_args = transports[name]
            trusted = cfg.get("command") == command and args == expected_args
        if not trusted:
            raise ValueError(f"unexpected bump MCP command/profile for {name!r}")
        allowed = catalogs[name]
        if "enabled_tools" in cfg:
            allowed = tuple(tool for tool in allowed if tool in cfg["enabled_tools"])
        disabled = cfg.get("disabled_tools", ())
        result[name] = tuple(tool for tool in allowed if tool not in disabled)
    return result


def _bump_codex_policy_overrides(
    policy: dict[str, tuple[str, ...]], mcp_servers: dict,
) -> tuple[str, ...]:
    """Pin scoped approval above project config, without changing shell approvals."""
    overrides = []
    for name, names in policy.items():
        # These identifiers came from the fixed catalog, not user text. Bare
        # TOML keys also match Codex's dotted CLI override parser exactly.
        prefix = "mcp_servers." + name
        overrides.extend((
            prefix + ".command=" + json.dumps(mcp_servers[name]["command"]),
            prefix + ".args=" + json.dumps(mcp_servers[name]["args"]),
            prefix + ".enabled_tools=" + json.dumps(names),
            prefix + '.default_tools_approval_mode="prompt"',
        ))
        overrides.extend(prefix + ".tools." + tool + '.approval_mode="approve"'
                         for tool in names)
    return tuple(overrides)


def _bump_codex_shell_overrides(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Keep every phase on its assigned runtime, including non-login shell calls."""
    return ("allow_login_shell=false", "features.shell_snapshot=false") + tuple(
        "shell_environment_policy.set." + key + "=" + json.dumps(environ[key])
        for key in _BUMP_MCP_ENV_KEYS if key in environ
    )


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
    """Wrap a CLI so its entire process tree has an bump-owned process group."""
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
            status = getattr(getattr(payload, "turn", None), "status", None)
            status = getattr(status, "value", status)
            if status == "completed":
                _console.print(f"[green]{_ts()} \\[{name}] ✓ turn complete[/green]")
            else:
                _console.print(f"[yellow]{_ts()} \\[{name}] turn ended: {status or 'unknown'}[/yellow]")
        return
    text = getattr(msg, "text", None) or getattr(msg, "message", None)
    if text:
        _console.print(f"[dim]{_ts()} \\[{name}][/dim] {str(text)[:500]}")


# ── backends ────────────────────────────────────────────────────────────────────

async def claude_spawner(agent: Agent, system_prompt: str, prompt: str, cwd: Path,
                         mcp_servers: dict, *, permission: str = "bypassPermissions",
                         idle_timeout: float = 600.0, subagents=(),
                         env_overrides: dict[str, str] | None = None,
                         own_process_group: bool = False,
                         on_normal_completion: CompletionCallback | None = None) -> str | None:
    from claude_agent_sdk import query, ClaudeAgentOptions, AgentDefinition
    import shutil

    isolation = {}
    roots = _worktree_write_roots(cwd)
    if roots:
        from .bump_permissions import claude_worktree_options
        isolation = claude_worktree_options(cwd, roots)
        system_prompt += _worktree_prompt(cwd)

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
        mcp_servers={name: ({"type": "http", **cfg} if cfg.get("url") else cfg)
                     for name, cfg in mcp_servers.items()},
        agents=agents_def,
        cwd=str(cwd),
        permission_mode=isolation.pop("permission_mode", permission),
        model=agent.model,
        max_budget_usd=agent.budget,
        env=_agent_env(agent, env_overrides=env_overrides),
        cli_path=cli_path,
        **isolation,
    )
    attempt = 0
    while True:
        attempt += 1
        try:
            if on_normal_completion is not None:
                return await _claude_continuation(
                    agent, options, prompt, cwd,
                    on_normal_completion, idle_timeout,
                )
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
        except _CompletionCallbackError as exc:
            raise exc.error
        except _UnsuccessfulTurnError:
            raise
        except Exception as e:
            if _give_up(e, attempt):
                raise
            wait = _retry_sleep(e)
            _log(agent.name, f"API Error ({e}), retrying in {int(wait)}s...")
            await _terminate_process_group(pid_file)
            await asyncio.sleep(wait)
        finally:
            await _terminate_process_group(pid_file)


async def _claude_continuation(agent, options, prompt, cwd,
                               callback, idle_timeout):
    """One connected SDK session; validation corrections are ordinary user turns."""
    from claude_agent_sdk import ClaudeSDKClient

    client = ClaudeSDKClient(options=options)
    await client.connect()
    total_turns = 0
    try:
        while not _stop_requested(cwd):
            await client.query(prompt)
            result = None
            stream = client.receive_response()
            try:
                async for msg in _idle_guard(stream, idle_timeout):
                    _log(agent.name, msg, cwd)
                    if type(msg).__name__ == "ResultMessage":
                        result = msg
                        total_turns += getattr(msg, "num_turns", 0) or 0
                        # The SDK reports cumulative session cost, not a per-turn delta.
                        _last_run_stats[agent.name] = {
                            "cost_usd": getattr(msg, "total_cost_usd", None),
                            "num_turns": total_turns,
                        }
                    if _stop_requested(cwd):
                        return getattr(result, "result", None)
            finally:
                await stream.aclose()
            if result is None or getattr(result, "is_error", True):
                detail = getattr(result, "subtype", "missing successful result")
                raise _UnsuccessfulTurnError(f"Claude turn did not complete successfully: {detail}")
            final = getattr(result, "result", None)
            prompt = await _completion_feedback(callback, final, cwd)
            if not prompt:
                return final
    finally:
        await client.disconnect()


def _write_codex_config(home: Path, agent: Agent, mcp_servers: dict,
                        writable_roots: tuple[Path, ...] = (), *,
                        bump_phase: str | None = None) -> str | None:
    """Seed CODEX_HOME/config.toml with a custom provider (if base_url), MCP servers,
    and workspace-write sandbox tuning. Returns the provider id to pass as
    model_provider, or None for the default openai provider."""
    policy = (_bump_codex_tool_policy(mcp_servers, bump_phase)
              if bump_phase is not None else {})
    home.mkdir(parents=True, exist_ok=True)
    # No api_key -> ride the user's Codex subscription: copy their login into this
    # agent's isolated CODEX_HOME.
    if not agent.api_key:
        user_auth = Path.home() / ".codex" / "auth.json"
        if user_auth.exists():
            import shutil
            shutil.copy2(user_auth, home / "auth.json")
    lines: list[str] = []
    # Keep network access for tools, without granting the main source checkout.
    lines += ["[sandbox_workspace_write]", "network_access = true"]
    if writable_roots:
        lines.append("writable_roots = " + json.dumps([str(path) for path in writable_roots]))
        lines += ["exclude_slash_tmp = true", "exclude_tmpdir_env_var = true"]
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
            lines.append("command = " + json.dumps(cfg["command"]))
            if cfg.get("args"):
                lines.append("args = " + json.dumps(cfg["args"]))
        elif cfg.get("url"):
            lines.append("url = " + json.dumps(cfg["url"]))
            headers = cfg.get("http_headers") or cfg.get("headers")
            if headers:
                lines.append("http_headers = { " + ", ".join(
                    f"{json.dumps(key)} = {json.dumps(value)}" for key, value in headers.items()
                ) + " }")
        if name in policy:
            lines.append("enabled_tools = " + json.dumps(policy[name]))
            lines.append('default_tools_approval_mode = "prompt"')
        lines.append("")
        if cfg.get("env"):
            lines.append(f"[mcp_servers.{name}.env]")
            for k, v in cfg["env"].items():
                lines.append(f'{k} = {json.dumps(str(v), ensure_ascii=False)}')
            lines.append("")
        for tool in policy.get(name, ()):
            lines.extend((f"[mcp_servers.{name}.tools.{tool}]",
                          'approval_mode = "approve"', ""))
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


# Some custom Responses providers do not expose native MCP. The documented
# shell bridge remains a transport fallback, not a way around a denied call.
_BUMP_CODEX_MCP_NOTE = (
    "\n\nBUMP MCP TOOLS: Use the native tools when exposed. If this backend does not "
    "expose native MCP tools (as with some custom providers), use the shell bridge:\n"
    "    unity mcp <server> <tool> '<json-args>'\n"
    "Examples:\n"
    "    unity mcp unity-forum bump_brief '{\"author\": \"<your agent name>\"}'\n"
    "    unity mcp unity-forum register_strategy '{\"target\": \"formal-task-id\", \"author\": \"<you>\", \"description\": \"...\", \"strategy_family\": \"core_method\"}'\n"
    "    unity mcp unity-forum claim_strategy '{\"strategy_id\": \"strategy-...\", \"author\": \"<you>\"}'\n"
    "    unity mcp unity-forum finalize_formalization '{\"strategy_id\": \"strategy-...\", \"task_id\": \"formal-task-id\", \"author\": \"<you>\"}'\n"
    "    unity mcp lean-lsp lean_goal '{\"file_path\": \"...\", \"line\": 12}'\n"
    "Servers: unity-forum (bump tools), lean-lsp, axle and aristotle when configured. "
    "An approval denial is a configuration blocker: report it, do not bypass it with another "
    "transport. The bump Forum contract "
    "is not optional on this backend. Use `target`, never prove's `decl` argument, when "
    "registering an bump strategy. Only call tools exposed for your current phase.\n"
)


def _codex_mcp_note(profile: str, phase: str | None = None) -> str:
    if phase == "chunking":
        return (
            "\n\nCHUNKING MCP TOOLS: Use only the chunking tools described in your role prompt. "
            "If native MCP tools are unavailable, use the shell bridge:\n"
            "    unity mcp unity-forum <tool> '<json-args>'\n"
            "For example: unity mcp unity-forum bump_brief "
            "'{\"author\": \"<your agent name>\"}'\n"
            "Do not import internal Unity state helpers or modify controller state. "
            "Use only your phase's public Forum tools and edit your draft; "
            "the controller validates and installs it after your turn."
        )
    return _BUMP_CODEX_MCP_NOTE


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
                        mcp_profile: str = "bump",
                        on_normal_completion: CompletionCallback | None = None) -> str | None:
    from openai_codex import AsyncCodex, CodexConfig, Sandbox

    system_prompt = system_prompt + _codex_mcp_note(
        mcp_profile, (env_overrides or {}).get("UNITY_BUMP_PROFILE"),
    )

    home = Path(tempfile.mkdtemp(prefix="unity-codex-"))
    roots = _worktree_write_roots(cwd)
    if roots:
        system_prompt += _worktree_prompt(cwd)
    bump_phase = None
    policy = {}
    if mcp_profile == "bump":
        bump_phase = (env_overrides or {}).get("UNITY_BUMP_PROFILE")
        if bump_phase is None:
            forum_args = mcp_servers.get("unity-forum", {}).get("args", [])
            if len(forum_args) == 8 and forum_args[6] == "--profile":
                bump_phase = forum_args[7]
        if bump_phase is None:
            raise ValueError("bump Codex worker requires an explicit phase")
        policy = _bump_codex_tool_policy(mcp_servers, bump_phase)
    provider = _write_codex_config(home, agent, mcp_servers,
                                   writable_roots=roots, bump_phase=bump_phase)
    _write_codex_agents(home, subagents)
    # bypassPermissions ~ full_access; anything more restrictive still needs to edit files.
    sandbox = (Sandbox.workspace_write if roots or permission != "bypassPermissions"
               else Sandbox.full_access)

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
        config_kwargs = {}
        if roots:
            # CLI overrides outrank project configuration; workers cannot inherit
            # an extra writable checkout or a full-access project profile.
            config_kwargs["config_overrides"] = (
                'sandbox_mode="workspace-write"',
                'approval_policy="never"',
                "sandbox_workspace_write.writable_roots=" + json.dumps([str(path) for path in roots]),
                "sandbox_workspace_write.network_access=true",
                "sandbox_workspace_write.exclude_slash_tmp=true",
                "sandbox_workspace_write.exclude_tmpdir_env_var=true",
            )
        if mcp_profile == "bump":
            # Login startup and cached shell state can move elan ahead of bump's
            # Lake shim even when the app-server process receives the right PATH.
            config_kwargs["config_overrides"] = (
                config_kwargs.get("config_overrides", ())
                + _bump_codex_shell_overrides(agent_env)
                + _bump_codex_policy_overrides(policy, mcp_servers)
            )
        cfg = CodexConfig(
            cwd=str(cwd), env=agent_env, codex_bin=codex_bin, **config_kwargs,
        )
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
            thread_options = {}
            if roots:
                from openai_codex import ApprovalMode
                thread_options["approval_mode"] = ApprovalMode.deny_all
            thread = await codex.thread_start(
                model=agent.model,
                model_provider=provider,
                sandbox=sandbox,
                base_instructions=system_prompt,
                cwd=str(cwd),
                **thread_options,
            )
            next_prompt = prompt
            thread_usage = None
            while True:
                if (_stop_requested(cwd)
                        or (interrupt_event is not None and interrupt_event.is_set())):
                    return final
                turn_finished = asyncio.Event()
                stream_started = asyncio.Event()
                final = None
                handle = await thread.turn(next_prompt)

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
                successful = False
                failure_detail = ""
                async for note in _codex_notifications(
                    handle, codex, idle_timeout, stream_started
                ):
                    _log(agent.name, note, cwd)
                    if _stop_requested(cwd):
                        _console.print(f"[yellow]{_ts()} \\[{agent.name}] safe stop — ending turn[/yellow]")
                        break
                    method = getattr(note, "method", "") or ""
                    payload = getattr(note, "payload", None)
                    if method in {"error", "turn/failed"}:
                        error = getattr(payload, "error", None)
                        failure_detail = str(getattr(error, "message", None)
                                             or getattr(payload, "message", None) or failure_detail)[:1000]
                    if method == "turn/completed":
                        turn_finished.set()
                        turn = getattr(payload, "turn", None)
                        status = getattr(turn, "status", None)
                        successful = getattr(status, "value", status) == "completed"
                        error = getattr(turn, "error", None)
                        failure_detail = str(getattr(error, "message", None) or failure_detail)[:1000]
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
                thread_usage = usage if usage is not None else thread_usage
                _last_run_stats[agent.name] = {"cost_usd": None, "usage": thread_usage}
                if interrupt_task is not None:
                    interrupt_task.cancel()
                    await asyncio.gather(interrupt_task, return_exceptions=True)
                    interrupt_task = None
                if (_stop_requested(cwd)
                        or (interrupt_event is not None and interrupt_event.is_set())):
                    return final
                if not successful:
                    # Codex can report provider errors as a terminal notification
                    # rather than raising from the stream. Route those through
                    # the same inherited transport retry cap and backoff.
                    raise RuntimeError("Codex turn did not complete successfully"
                                       + (": " + failure_detail if failure_detail else ""))
                if on_normal_completion is None:
                    return final
                next_prompt = await _completion_feedback(on_normal_completion, final, cwd)
                if not next_prompt:
                    return final
        except asyncio.CancelledError:
            raise
        except _CompletionCallbackError as exc:
            raise exc.error
        except _UnsuccessfulTurnError:
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
                              mcp_profile: str = "bump",
                              on_normal_completion: CompletionCallback | None = None) -> str | None:
    """Google Antigravity backend: drives the user's installed `agy` CLI in print mode
    (subscription auth; serves both the Gemini pool and the Claude/GPT pool). MCP tools
    reach the model through the `unity mcp` shell bridge, like codex."""
    import json as _json
    import shutil as _sh
    agy = _sh.which("agy")
    if agy is None:
        raise RuntimeError("antigravity backend needs the `agy` CLI installed and logged in "
                           "(https://antigravity.google)")
    roots = _worktree_write_roots(cwd)
    if roots:
        system_prompt += _worktree_prompt(cwd)
    full = system_prompt + _codex_mcp_note(
        mcp_profile, (env_overrides or {}).get("UNITY_BUMP_PROFILE"),
    ) + "\n\n---\n\nTASK:\n" + prompt
    cmd = [agy, "--print", full, "--model", agent.model, "--output-format", "stream-json",
           "--dangerously-skip-permissions", "--print-timeout", "72h"]
    if roots:
        # AG has no per-session editor/path hook. Its native terminal sandbox is
        # best effort; keep headless tool approvals so shell-MCP is not soft-denied.
        cmd += ["--sandbox"]
        for root in roots:
            if root == Path(cwd).resolve():
                continue
            cmd += ["--add-dir", str(root)]
        _console.print(
            f"[yellow]{agent.name}: Antigravity worktree isolation is best effort; "
            "terminal sandbox enabled, editor writes rely on the worktree policy.[/yellow]"
        )

    attempt = 0
    total_usage = None
    continuation_warned = False
    while True:
        if _stop_requested(cwd):
            return None
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
            successful = False
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
                    successful = r.get("status") == "SUCCESS"
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
                failure_type = _UnsuccessfulTurnError if on_normal_completion is not None else RuntimeError
                raise failure_type(f"agy turn failed: {failed}")
            if final is None and rc not in (0, -15):
                failure_type = _UnsuccessfulTurnError if on_normal_completion is not None else RuntimeError
                raise failure_type(f"agy exited with code {rc} and no response")
            _flush_delta(agent.name)
            _console.print(f"[green]{_ts()} \\[{agent.name}] ✓ turn complete[/green]")
            total_usage = _sum_usage(total_usage, usage)
            _last_run_stats[agent.name] = {"cost_usd": None, "usage": total_usage}
            if on_normal_completion is not None and not _stop_requested(cwd):
                if not successful or rc != 0:
                    raise _UnsuccessfulTurnError("Antigravity turn did not complete successfully")
                feedback = await _completion_feedback(on_normal_completion, final, cwd)
                if feedback:
                    if not continuation_warned:
                        _log(agent.name, "Antigravity continuation: retaining this logical attempt and draft; "
                             "conversation resume is unavailable, so feedback starts a new CLI session.")
                        continuation_warned = True
                    cmd[2] = full + "\n\nCONTROLLER VALIDATION FEEDBACK:\n" + feedback
                    attempt -= 1  # A draft correction is not a backend failure.
                    continue
            return final
        except _CompletionCallbackError as exc:
            raise exc.error
        except _UnsuccessfulTurnError:
            raise
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


def _sum_usage(previous, current):
    """Accumulate independent CLI-turn usage without assuming a provider schema."""
    if current is None:
        return previous
    if isinstance(previous, dict) and isinstance(current, dict):
        return {key: _sum_usage(previous.get(key), current.get(key))
                for key in previous.keys() | current.keys()}
    if (isinstance(previous, (int, float)) and not isinstance(previous, bool)
            and isinstance(current, (int, float)) and not isinstance(current, bool)):
        return previous + current
    return current


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


def _bump_external_tools_prompt(mcp_servers: dict) -> str:
    """Describe configured formalizing tools without exposing credentials."""
    prompt_dir = Path(__file__).parent / "prompts"
    enabled = [
        name for name in ("axle", "aristotle")
        if name in mcp_servers
    ]
    sections = [
        "External services enabled for this run: "
        + (", ".join(enabled) or "none")
        + ".",
        "Tool availability does not change your phase's permissions "
        "or Unity's verification requirements.",
    ]
    if "lean-lsp" in mcp_servers:
        sections.append((prompt_dir / "BUMP_LEAN_TOOLS.md").read_text())
    for name in enabled:
        sections.append(
            (prompt_dir / f"BUMP_{name.upper()}_TOOLS.md").read_text()
        )
    return "\n\n".join(sections)


async def spawn(agent: Agent, system_prompt: str, prompt: str, cwd: Path,
                mcp_servers: dict, *, permission: str = "bypassPermissions",
                idle_timeout: float = 600.0, subagents=(),
                interrupt_event: asyncio.Event | None = None,
                log_context: dict | None = None,
                env_overrides: dict[str, str] | None = None,
                own_process_group: bool = False,
                mcp_profile: str = "bump",
                on_normal_completion: CompletionCallback | None = None) -> str | None:
    backend = {"claude_code": claude_spawner, "codex": codex_spawner,
               "antigravity": antigravity_spawner}[agent.backend]
    import time
    t0 = time.monotonic()
    try:
        if _stop_requested(cwd):
            return None
        phase = (log_context or {}).get("phase")
        if phase and "UNITY_BUMP_PROFILE" not in (env_overrides or {}):
            env_overrides = {**(env_overrides or {}), "UNITY_BUMP_PROFILE": phase}
        # Other bump phases keep their own prompts, without proof-development catalogs.
        if (mcp_profile == "bump"
                and (env_overrides or {}).get("UNITY_BUMP_PROFILE", phase) == "formalizing"):
            system_prompt += "\n\n" + _bump_external_tools_prompt(mcp_servers)
        if mcp_profile == "bump":
            worker_env = dict(os.environ)
            worker_env.update(_agent_env(agent, env_overrides=env_overrides))
            mcp_servers = bump_mcp_with_runtime_env(mcp_servers, worker_env)
        kwargs = {
            "permission": permission,
            "idle_timeout": idle_timeout,
            "subagents": subagents,
            "env_overrides": env_overrides,
            "own_process_group": own_process_group,
            "on_normal_completion": on_normal_completion,
        }
        if agent.backend == "codex":
            kwargs["interrupt_event"] = interrupt_event
            kwargs["mcp_profile"] = mcp_profile
        elif agent.backend == "antigravity":
            kwargs["mcp_profile"] = mcp_profile
        return await backend(agent, system_prompt, prompt, cwd, mcp_servers, **kwargs)
    finally:
        _write_run_log(agent, cwd, time.monotonic() - t0, log_context)
