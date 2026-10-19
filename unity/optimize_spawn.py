"""Optimize-owned agent launcher, ported from current Bump operational helpers.

Each agent's credentials are turned into a per-process env here and handed only
to that agent's child — os.environ is never mutated, so a mixed roster can run
concurrently under asyncio.gather. Callers use this module's spawn(); its backend
helpers are independent copies, not the solve/prove launch implementation.
"""

import asyncio
import hashlib
import json
import os
import signal
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from rich.console import Console

from .roster import Agent

_console = Console()

CompletionCallback = Callable[[str | None], Awaitable[str | None]]


class _UnsuccessfulTurnError(RuntimeError):
    """A terminal session failure, not a transient transport exception to retry."""


class SpawnCleanupError(RuntimeError):
    """Owned execution could not be closed; never reschedule it as a retry."""


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


def _worktree_write_roots(cwd: Path, *, profile: str | None = None) -> tuple[Path, ...]:
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
    if unity is None:
        return ()
    unity = unity.resolve()
    # Optimize worktrees have private .unity directories; their marker binds
    # the source project, while only exact shared runtime children are writable.
    marker = unity / "optimize-worktree.json"
    private = marker.is_file()
    if unity.parent == cwd and not private:
        return ()
    git = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
        cwd=cwd, capture_output=True, text=True, check=True,
    )
    git_dir, common = (Path(line).resolve() for line in git.stdout.splitlines())
    # Candidate commits share Git objects/refs/maintenance locks with main.
    # Grant metadata, never the enclosing source checkout.
    roots = [cwd, git_dir, common]
    if private:
        binding = json.loads(marker.read_text())
        project = Path(binding["project_root"]).resolve()
        if binding.get("schema") != "optimize-worktree-v1" or project == cwd:
            raise ValueError("invalid Optimize private worktree binding")
        for name in ("forum", "artifacts", "logs", "jobs", "tmp"):
            entry = unity / name
            expected = project / ".unity" / name
            if not entry.is_symlink() or entry.resolve() != expected.resolve():
                raise ValueError("Optimize shared runtime binding changed: " + name)
            roots.append(entry.resolve())
    else:
        roots.append(unity)
    packages = cwd / ".lake" / "packages"
    if packages.is_dir():
        roots.append(packages.resolve())
    # `unity mcp` uses uvx for local services; keep its existing global cache.
    uv = shutil.which("uv")
    if uv:
        cache = subprocess.run([uv, "cache", "dir"], capture_output=True, text=True, check=True)
        roots.append(Path(cache.stdout.strip()).resolve())
    if profile == "retrospective":
        from .optimize_permissions import retrospective_library_root
        roots.append(retrospective_library_root())
    if any(path != cwd and cwd.is_relative_to(path) for path in roots):
        raise ValueError("optimize shared write access cannot include the source checkout")
    return tuple(dict.fromkeys(roots))


def _worktree_prompt(cwd: Path, *, profile: str | None = None) -> str:
    return (
        f"\n\nWORKTREE WRITE POLICY: Your source workspace is exactly {Path(cwd).resolve()}. "
        "Use this directory as workdir/cwd for every shell command and Lean tool. "
        "When your turn permits source changes, keep source and proof edits here. "
        "Administrative planning files are not source edits. Main and other agents' worktrees "
        "are read-only to you, including through absolute paths, ../ paths, and symlinks. "
        "Shared runtime/build directories exist for Unity tools, not as alternate source "
        "workspaces. Use the Forum tools for shared state, candidate submission, and sync; "
        "Unity alone integrates candidates into main. If a write is denied, correct its "
        "destination or report the blocker; do not disable or bypass the restriction."
        + _planning_file_prompt(profile)
    )


def _planning_file_prompt(profile: str | None) -> str:
    if profile == "critic":
        return (
            " Review is read-only: use native tools or inline JSON arguments to the "
            "shell bridge; do not create or modify planning files or scratch files."
        )
    return (
        " Put inventory exports, plan drafts, tool-argument JSON and temporary planning "
        "helpers only in the supplied UNITY_OPTIMIZE_SCRATCH directory, not the source "
        "root or an ad hoc .unity-tmp directory. For large arguments, use "
        'unity mcp unity-forum <tool> --args-file "$UNITY_OPTIMIZE_SCRATCH/plan.json" '
        "instead of positional JSON. This does not permit source changes in a "
        "source-read-only phase."
    )


# ── env (per-agent, never global) ──────────────────────────────────────────────

_OPTIMIZE_MCP_ENV_KEYS = (
    "PATH",
    "UNITY_REAL_LAKE",
    "UNITY_REAL_LEAN",
    "UNITY_OPTIMIZE_PROJECT_ROOT",
    "UNITY_OPTIMIZE_TASK_ID",
    "UNITY_OPTIMIZE_PHASE_DISPATCH_ID",
    "UNITY_OPTIMIZE_STRATEGY_ID",
    "UNITY_OPTIMIZE_AUTHOR",
    "UNITY_OPTIMIZE_WORKTREE",
    "UNITY_OPTIMIZE_BASE_SHA",
    "UNITY_OPTIMIZE_PROCESS_RECEIPTS",
    "UNITY_OPTIMIZE_PROFILE",
    "UNITY_OPTIMIZE_SCRATCH",
    "UNITY_OPTIMIZE_WORKFLOW",
    "UNITY_OPTIMIZE_LAKE_ARGS",
    "UNITY_OPTIMIZE_DRAFT_PATH",
    "UNITY_AGENT_NAME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "UV_TOOL_DIR",
    "UV_TOOL_BIN_DIR",
    "LAKE_CACHE_DIR",
)

_OPTIMIZE_UVX_ENV_KEYS = (
    "PATH", "TMPDIR", "TMP", "TEMP", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR",
)


def optimize_mcp_with_runtime_env(
    servers: dict, environ: Mapping[str, str],
) -> dict:
    """Bind optimize's local services to a worker's non-secret runtime.

    Stdio SDKs may inherit the guarded PATH without its companion variables.
    They also omit UV_TOOL_DIR, causing uvx to write to the read-only global
    tools install. Explicitly forward worker scratch paths to both uvx services.
    Never forward the whole worker environment (which contains credentials) or
    alter shared mappings; retain each service's explicitly configured keys.
    """
    result = servers
    for name, env_keys in (
        ("lean-lsp", _OPTIMIZE_MCP_ENV_KEYS),
        ("axle", _OPTIMIZE_UVX_ENV_KEYS),
        ("unity-forum", _OPTIMIZE_MCP_ENV_KEYS),
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


# Audited names from OPTIMIZE_{LEAN,AXLE,ARISTOTLE}_TOOLS.md. A newly installed
# service tool is not automatically authorized. Forum names come from its actual
# phase registration below, rather than a second copy of that contract.
_OPTIMIZE_LEAN_INSPECTION_TOOLS = (
    "lean_goal", "lean_term_goal", "lean_diagnostic_messages", "lean_file_outline",
    "lean_hover_info", "lean_completions", "lean_declaration_file", "lean_references",
    "lean_local_search", "lean_leansearch", "lean_loogle", "lean_leanfinder",
    "lean_state_search", "lean_hammer_premise", "lean_code_actions", "lean_run_code",
    "lean_verify", "lean_minimal_hypotheses", "lean_profile_proof", "lean_get_widgets",
    "lean_get_widget_source",
)
_OPTIMIZE_AXLE_INSPECTION_TOOLS = (
    "check", "verify_proof", "highlight", "extract_decls", "list_environments",
    "read_share_url", "read_docs",
)
_OPTIMIZE_AXLE_PROOF_TOOLS = (
    "repair_proofs", "simplify_theorems", "disprove", "merge", "rename", "normalize",
    "theorem2lemma", "theorem2sorry", "have2lemma", "have2sorry", "sorry2lemma",
    "share_url",
)
_OPTIMIZE_ARISTOTLE_INSPECTION_TOOLS = (
    "aristotle_status", "aristotle_wait", "aristotle_list",
)


def _optimize_codex_tool_policy(mcp_servers: dict, phase: str) -> dict[str, tuple[str, ...]]:
    """Authorize only this phase's existing public tools on known transports.

    Codex MCP approval is independent of shell approval_policy. Preapproval here
    is scoped to the pipeline API already delegated to this worker, not to an
    arbitrary server or any tools it might advertise in a future release.
    """
    from .forum.optimize_server import PROFILE_TOOLS

    if phase not in PROFILE_TOOLS:
        raise ValueError(f"unknown optimize MCP phase {phase!r}")
    catalogs = {
        "unity-forum": tuple(tool.__name__ for tool in PROFILE_TOOLS[phase]),
        "lean-lsp": _OPTIMIZE_LEAN_INSPECTION_TOOLS
            + (("lean_multi_attempt",) if phase == "optimizing" else ()),
        "axle": _OPTIMIZE_AXLE_INSPECTION_TOOLS
            + (_OPTIMIZE_AXLE_PROOF_TOOLS if phase == "optimizing" else ()),
        "aristotle": _OPTIMIZE_ARISTOTLE_INSPECTION_TOOLS
            + (("aristotle_submit", "aristotle_result", "aristotle_cancel")
               if phase == "optimizing" else ()),
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
            raise ValueError(f"unrecognized optimize MCP transport {name!r}")
        args = cfg.get("args", [])
        if name == "unity-forum":
            trusted = (
                cfg.get("command") == sys.executable
                and isinstance(args, list) and len(args) in {8, 10}
                and args[:3] == ["-m", "unity.forum.optimize_server", "--forum-dir"]
                and args[4] == "--project-root" and args[6:8] == ["--profile", phase]
                and (len(args) == 8 or (args[8] == "--author" and isinstance(args[9], str)))
            )
        else:
            command, expected_args = transports[name]
            trusted = cfg.get("command") == command and args == expected_args
        if not trusted:
            raise ValueError(f"unexpected optimize MCP command/profile for {name!r}")
        allowed = catalogs[name]
        if "enabled_tools" in cfg:
            allowed = tuple(tool for tool in allowed if tool in cfg["enabled_tools"])
        disabled = cfg.get("disabled_tools", ())
        result[name] = tuple(tool for tool in allowed if tool not in disabled)
    return result


def _optimize_codex_policy_overrides(
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


def _optimize_codex_shell_overrides(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Keep every phase on its assigned runtime, including non-login shell calls."""
    return ("allow_login_shell=false", "features.shell_snapshot=false") + tuple(
        "shell_environment_policy.set." + key + "=" + json.dumps(environ[key])
        for key in _OPTIMIZE_MCP_ENV_KEYS if key in environ
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
    """Wrap a CLI so its entire process tree has an optimize-owned process group."""
    wrapper = directory / f"{label}-group-wrapper"
    pid_file = directory / f"{label}-group.pid"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, json, pathlib, subprocess, time, uuid\n"
        "os.setsid()\n"
        f"breadcrumb = pathlib.Path({str(pid_file.with_suffix('.receipt'))!r})\n"
        "breadcrumb.unlink(missing_ok=True)\n"
        f"pid_path = pathlib.Path({str(pid_file)!r})\n"
        "pid_pending = pid_path.with_suffix('.pid.tmp')\n"
        "pid_pending.write_text(str(os.getpid()))\n"
        "os.replace(pid_pending, pid_path)\n"
        "receipt_dir = os.environ.get('UNITY_OPTIMIZE_PROCESS_RECEIPTS')\n"
        "if receipt_dir:\n"
        "    destination = pathlib.Path(receipt_dir)\n"
        "    if not destination.is_absolute() or destination.resolve() != destination:\n"
        "        raise RuntimeError('owned process receipts need an exact absolute directory')\n"
        "    destination.mkdir(parents=True, exist_ok=True)\n"
        "    pid = os.getpid()\n"
        "    ticks = None\n"
        "    if sys.platform.startswith('linux'):\n"
        "        ticks = int(pathlib.Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19])\n"
        "        birth = None\n"
        "    else:\n"
        "        birth = subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True, check=True).stdout.strip()\n"
        "        if not birth:\n"
        "            raise RuntimeError('owned process birth observation unavailable')\n"
        f"    receipt = dict(schema_version=1, pid=pid, pgid=os.getpgrp(), sid=os.getsid(0), start_ticks=ticks, birth=birth, cwd=os.getcwd(), executable={str(executable)!r}, created_at=time.time())\n"
        "    receipt_path = destination / (str(pid) + '-' + uuid.uuid4().hex + '.json')\n"
        "    with os.fdopen(os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as handle:\n"
        "        json.dump(receipt, handle, sort_keys=True)\n"
        "        handle.flush()\n"
        "        os.fsync(handle.fileno())\n"
        "    pending = breadcrumb.with_suffix('.receipt.tmp')\n"
        "    pending.write_text(str(receipt_path))\n"
        "    os.replace(pending, breadcrumb)\n"
        f"os.execv({str(executable)!r}, [{str(executable)!r}, *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o700)
    return wrapper, pid_file


def _record_process_group_closed(pid_file: Path, pgid: int) -> None:
    """Retain immutable birth evidence and append closure only after observation."""
    breadcrumb = pid_file.with_suffix(".receipt")
    if not breadcrumb.exists():
        return  # Older/non-workflow spawns did not request persistent receipts.
    path = Path(breadcrumb.read_text().strip())
    if not path.is_absolute() or path.resolve() != path or not path.is_file():
        raise RuntimeError("owned process receipt path changed")
    data = path.read_bytes()
    receipt = json.loads(data)
    if receipt.get("pgid") != pgid or receipt.get("pid") != pgid or receipt.get("sid") != pgid:
        raise RuntimeError("owned process receipt identity mismatch")
    closed = path.with_suffix(".closed.json")
    value = {"receipt_sha256": hashlib.sha256(data).hexdigest(), "pgid": pgid,
             "closed_at": time.time(), "group_empty": True}
    try:
        fd = os.open(closed, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        prior = json.loads(closed.read_text())
        if prior.get("receipt_sha256") != value["receipt_sha256"] or not prior.get("group_empty"):
            raise RuntimeError("owned process closure receipt mismatch")
        return
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())


_PROCESS_GROUP_CLOSE_TIMEOUT = 1.0
_PROCESS_GROUP_POLL_SECONDS = 0.01


def _process_group_alive(pgid: int) -> bool:
    """Observe running group members; zombies are closed, not retained work."""
    proc = Path('/proc')
    if sys.platform.startswith('linux') and proc.is_dir():
        for directory in proc.iterdir():
            if not directory.name.isdecimal():
                continue
            try:
                fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                # Inaccessible unrelated processes must not obscure our group.
                try:
                    if os.getpgid(int(directory.name)) != pgid:
                        continue
                except ProcessLookupError:
                    continue
                raise
            if int(fields[2]) == pgid and fields[0] not in {'Z', 'X'}:
                return True
        return False
    # POSIX hosts without procfs still need an observed non-zombie drain.
    import subprocess
    result = subprocess.run(['ps', '-e', '-o', 'pgid=,stat='],
                            capture_output=True, text=True, check=True, timeout=0.25)
    return any(int(fields[0]) == pgid and fields[1][0] not in {'Z', 'X'}
               for line in result.stdout.splitlines() if len(fields := line.split()) >= 2)


def _process_group_births(pgid: int) -> set[tuple[int, int]] | None:
    """Linux birth anchors keep delayed signals off a reused numeric PGID."""
    if not sys.platform.startswith('linux'):
        return None
    members = set()
    for directory in Path('/proc').iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            try:
                if os.getpgid(int(directory.name)) != pgid:
                    continue
            except ProcessLookupError:
                continue
            raise
        if int(fields[2]) == pgid and fields[0] not in {'Z', 'X'}:
            members.add((int(directory.name), int(fields[19])))
    return members


def _capture_process_group_births(pid_file: Path | None):
    if pid_file is None or os.name != 'posix':
        return None
    try:
        return _process_group_births(int(pid_file.read_text().strip()))
    except FileNotFoundError:
        return None
    except Exception as exc:
        raise SpawnCleanupError('owned process group birth could not be verified') from exc


async def _terminate_process_group(pid_file: Path | None, *, captured_births=None) -> None:
    try:
        await _terminate_process_group_checked(pid_file, captured_births=captured_births)
    except Exception as exc:
        raise SpawnCleanupError('owned process group closure could not be verified') from exc


async def _terminate_process_group_checked(pid_file: Path | None, *, captured_births=None) -> None:
    if pid_file is None or os.name != "posix":
        return
    try:
        pgid = int(pid_file.read_text().strip())
    except FileNotFoundError:
        return
    if pgid <= 1 or pgid == os.getpgrp():
        raise RuntimeError('refusing to terminate an unowned process group')
    anchors = (_process_group_births(pgid) if captured_births is None
               else set(captured_births))
    for sig, delay in ((signal.SIGTERM, 0.25), (signal.SIGKILL, 0.0)):
        members = _process_group_births(pgid)
        if anchors is not None and members is not None:
            if not anchors.intersection(members):
                if members:
                    raise RuntimeError('process group birth identity changed during cleanup')
                break
            # New children belong to this still-birth-anchored live group.
            anchors.update(members)
        if not _process_group_alive(pgid):
            break
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        if delay:
            await asyncio.sleep(delay)
    # Signal delivery is asynchronous, including SIGKILL. Do not publish spawn
    # completion or allow replacement until actual owned work has stopped.
    deadline = asyncio.get_running_loop().time() + _PROCESS_GROUP_CLOSE_TIMEOUT
    while _process_group_alive(pgid):
        if asyncio.get_running_loop().time() >= deadline:
            # Retain the breadcrumb if closure cannot be certified.
            raise TimeoutError('owned process group did not stop after termination')
        await asyncio.sleep(_PROCESS_GROUP_POLL_SECONDS)
    _record_process_group_closed(pid_file, pgid)
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

def _claude_sandbox_tmpdir() -> Path:
    """Keep Claude's HTTP/SOCKS bridge sockets below Unix path limits.

    Worker cache paths can be much longer than sockaddr_un.sun_path. This is
    private, invocation-local socket scratch, not a replacement for those
    persistent caches or for the MCP servers' explicitly configured runtime.
    """
    import shutil

    scratch = Path(tempfile.mkdtemp(prefix="uo-", dir="/tmp"))
    try:
        scratch.chmod(0o700)
        # Claude 2.1.105 appends a 16-hex nonce. Use the smaller macOS bound
        # (104 bytes, including NUL), which also fits Linux's 108-byte field.
        for root in (scratch, scratch.resolve()):
            for bridge in ("http", "socks"):
                socket_path = root / f"claude-{bridge}-{'0' * 16}.sock"
                if len(os.fsencode(socket_path)) >= 104:
                    raise ValueError("Claude sandbox socket path exceeds Unix socket limit")
        return scratch
    except BaseException:
        shutil.rmtree(scratch)
        raise


async def claude_spawner(agent: Agent, system_prompt: str, prompt: str, cwd: Path,
                         mcp_servers: dict, *, permission: str = "bypassPermissions",
                         idle_timeout: float = 600.0, subagents=(),
                         env_overrides: dict[str, str] | None = None,
                         own_process_group: bool = False,
                         on_normal_completion: CompletionCallback | None = None) -> str | None:
    from claude_agent_sdk import query, ClaudeAgentOptions, AgentDefinition
    import shutil

    scratch = None
    sdk_closed = True
    cleanup_failed = False
    pid_file = None

    def mark_sdk_closed() -> None:
        nonlocal sdk_closed
        sdk_closed = True

    async def close_group() -> None:
        nonlocal cleanup_failed
        try:
            await _terminate_process_group(pid_file)
        except BaseException:
            cleanup_failed = True
            raise

    try:
        isolation = {}
        profile = (env_overrides or {}).get("UNITY_OPTIMIZE_PROFILE")
        roots = _worktree_write_roots(cwd, profile=profile)
        if roots:
            from .optimize_permissions import claude_worktree_options
            scratch = _claude_sandbox_tmpdir()
            isolation = claude_worktree_options(cwd, (*roots, scratch),
                read_only=profile == "critic", retrospective=profile == "retrospective")
            system_prompt += _worktree_prompt(cwd, profile=profile)
            env_overrides = {**(env_overrides or {}),
                             **dict.fromkeys(("TMPDIR", "TMP", "TEMP"), str(scratch))}

        cli_path = None
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
            sdk_closed = True
            try:
                if on_normal_completion is not None:
                    sdk_closed = False
                    return await _claude_continuation(
                        agent, options, prompt, cwd,
                        on_normal_completion, idle_timeout,
                        on_sdk_closed=mark_sdk_closed,
                    )
                final = None
                stream = query(prompt=prompt, options=options)
                sdk_closed = False
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
                        mark_sdk_closed()
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
                await close_group()
                await asyncio.sleep(wait)
            finally:
                cleanup_failed |= not sdk_closed
                await close_group()
    finally:
        # Never remove scratch while a failed/interrupted SDK or group cleanup
        # could still leave an owned process using it. A retry uses the same
        # private directory; closure failure remains sticky across attempts.
        if scratch is not None and sdk_closed and not cleanup_failed:
            shutil.rmtree(scratch)


async def _claude_continuation(agent, options, prompt, cwd,
                               callback, idle_timeout, *, on_sdk_closed=None):
    """One connected SDK session; validation corrections are ordinary user turns."""
    from claude_agent_sdk import ClaudeSDKClient

    client = ClaudeSDKClient(options=options)
    await client.connect()
    total_turns = 0
    response_closed = True
    try:
        while not _stop_requested(cwd):
            await client.query(prompt)
            result = None
            stream = client.receive_response()
            response_closed = False
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
                response_closed = True
            if result is None or getattr(result, "is_error", True):
                detail = getattr(result, "subtype", "missing successful result")
                raise _UnsuccessfulTurnError(f"Claude turn did not complete successfully: {detail}")
            final = getattr(result, "result", None)
            prompt = await _completion_feedback(callback, final, cwd)
            if not prompt:
                return final
    finally:
        await client.disconnect()
        if response_closed and on_sdk_closed is not None:
            on_sdk_closed()


def _write_codex_config(home: Path, agent: Agent, mcp_servers: dict,
                        writable_roots: tuple[Path, ...] = (), *,
                        optimize_phase: str | None = None) -> str | None:
    """Seed CODEX_HOME/config.toml with a custom provider (if base_url), MCP servers,
    and workspace-write sandbox tuning. Returns the provider id to pass as
    model_provider, or None for the default openai provider."""
    policy = (_optimize_codex_tool_policy(mcp_servers, optimize_phase)
              if optimize_phase is not None else {})
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
_OPTIMIZE_CODEX_MCP_NOTE = (
    "\n\nOPTIMIZE MCP TOOLS: Use this phase's advertised native tools when exposed. "
    "If this backend does not expose native MCP, use the shell bridge:\n"
    "    unity mcp unity-forum <tool> '<json-args>'\n"
    "The Optimize forum server is unity-forum (alias forum), not unity_forum or optimize. "
    "Large output may be retained as an artifact; read it "
    "with artifact_read and follow next_offset until complete. Read optimize_brief "
    "for your phase/task binding, then follow your turn's instructions. Preparation "
    "phase names are not task IDs; call optimize_task only for a real bound task. "
    "Use only tools advertised for your phase, and do not import Unity internals or edit "
    "controller state. A transport denial is a configuration blocker, not permission to "
    "bypass permissions. Keep private source edits in your assigned worktree."
)


def _codex_mcp_note(profile: str, phase: str | None = None) -> str:
    return _OPTIMIZE_CODEX_MCP_NOTE + _planning_file_prompt(phase)


_CODEX_INTERRUPT_REQUEST_TIMEOUT = 10.0
_CODEX_INTERRUPT_DRAIN_TIMEOUT = 20.0
_CODEX_CLOSE_TIMEOUT = 15.0
_CODEX_CANCELLED_CLOSE_TIMEOUT = 0.25
_CODEX_NOTIFICATION_DRAIN_TIMEOUT = 1.0


async def _drain_spawn_cleanup(cleanup) -> None:
    """Finish mandatory cleanup before cancellation/accounting can escape."""
    task = asyncio.create_task(cleanup)
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    # A closure failure takes precedence over cancellation: otherwise the
    # controller could regard retained execution as a cancelled/eligible worker.
    task.result()
    if interrupted:
        raise asyncio.CancelledError


async def _close_codex_bounded(codex) -> None:
    # Already-cancelled workers must not start another full 15s close wait:
    # the controller's hard cancellation drain is only 10s. Still start SDK
    # close so its transport/router can wake registered notification queues.
    current = asyncio.current_task()
    timeout = (_CODEX_CANCELLED_CLOSE_TIMEOUT if current and current.cancelling()
               else _CODEX_CLOSE_TIMEOUT)
    try:
        await asyncio.wait_for(codex.close(), timeout=timeout)
    except (asyncio.TimeoutError, Exception):
        pass


async def _cleanup_codex_attempt(codex, interrupt_task, pid_file) -> None:
    births = None
    try:
        births = _capture_process_group_births(pid_file)
        if interrupt_task is not None:
            interrupt_task.cancel()
            await asyncio.gather(interrupt_task, return_exceptions=True)
        # Leave slow close cancellable. Shield only mandatory group cleanup,
        # so a cancelled worker cannot retain its server or delay the controller
        # by another full close timeout.
        await _close_codex_bounded(codex)
    finally:
        await _drain_spawn_cleanup(_terminate_process_group(pid_file, captured_births=births))


async def _codex_notifications(
    handle, codex, idle_timeout: float, stream_started: asyncio.Event,
    pid_file: Path | None = None,
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
        aborting = pending is not None and not pending.done()
        births = None
        try:
            if aborting:
                births = _capture_process_group_births(pid_file)
                await _close_codex_bounded(codex)
        finally:
            async def drain_stream() -> None:
                failure = None
                try:
                    if aborting:
                        # Terminate owned transport/children before draining the
                        # SDK's shielded, thread-backed Queue.get waiter. Never
                        # unregister its queue while that read is still blocked.
                        await _terminate_process_group(pid_file, captured_births=births)
                except SpawnCleanupError as exc:
                    failure = exc
                if pending is not None:
                    done, _ = await asyncio.wait(
                        {pending}, timeout=_CODEX_NOTIFICATION_DRAIN_TIMEOUT,
                    )
                    if not done:
                        # Keep the queue registered and its reader intact. A
                        # retained transport is fatal, not an unbounded cleanup
                        # wait or permission to replace this worker.
                        pending.add_done_callback(
                            lambda task: None if task.cancelled() else task.exception()
                        )
                        raise SpawnCleanupError('Codex notification reader did not drain') from failure
                    await asyncio.gather(pending, return_exceptions=True)
                await stream.aclose()
                if failure is not None:
                    raise failure

            await _drain_spawn_cleanup(drain_stream())


async def codex_spawner(agent: Agent, system_prompt: str, prompt: str, cwd: Path,
                        mcp_servers: dict, *, permission: str = "bypassPermissions",
                        idle_timeout: float = 600.0, subagents=(),
                        interrupt_event: asyncio.Event | None = None,
                        env_overrides: dict[str, str] | None = None,
                        own_process_group: bool = False,
                        mcp_profile: str = "optimize",
                        on_normal_completion: CompletionCallback | None = None) -> str | None:
    from openai_codex import AsyncCodex, CodexConfig, Sandbox

    system_prompt = system_prompt + _codex_mcp_note(
        mcp_profile, (env_overrides or {}).get("UNITY_OPTIMIZE_PROFILE"),
    )

    home = Path(tempfile.mkdtemp(prefix="unity-codex-"))
    roots = _worktree_write_roots(cwd, profile=(env_overrides or {}).get("UNITY_OPTIMIZE_PROFILE"))
    if roots:
        system_prompt += _worktree_prompt(cwd, profile=(env_overrides or {}).get("UNITY_OPTIMIZE_PROFILE"))
    optimize_phase = None
    policy = {}
    if mcp_profile == "optimize":
        optimize_phase = (env_overrides or {}).get("UNITY_OPTIMIZE_PROFILE")
        if optimize_phase is None:
            forum_args = mcp_servers.get("unity-forum", {}).get("args", [])
            if len(forum_args) in {8, 10} and forum_args[6] == "--profile":
                optimize_phase = forum_args[7]
        if optimize_phase is None:
            raise ValueError("optimize Codex worker requires an explicit phase")
        policy = _optimize_codex_tool_policy(mcp_servers, optimize_phase)
    provider = _write_codex_config(home, agent, mcp_servers,
                                   writable_roots=roots, optimize_phase=optimize_phase)
    _write_codex_agents(home, subagents)
    # bypassPermissions ~ full_access; anything more restrictive still needs to edit files.
    sandbox = (Sandbox.workspace_write if roots or permission != "bypassPermissions"
               else Sandbox.full_access)
    # The critic must use shell-MCP for providers without native MCP. A
    # filesystem-wide read-only sandbox would also forbid its Forum verdict.
    # Preserve worktree sandboxing; phase tools and the controller's exact
    # before/after source/config check enforce the read-only review contract.

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
        if mcp_profile == "optimize":
            # Login startup and cached shell state can move elan ahead of optimize's
            # Lake shim even when the app-server process receives the right PATH.
            config_kwargs["config_overrides"] = (
                config_kwargs.get("config_overrides", ())
                + _optimize_codex_shell_overrides(agent_env)
                + _optimize_codex_policy_overrides(policy, mcp_servers)
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
                    handle, codex, idle_timeout, stream_started, pid_file
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
        except SpawnCleanupError:
            raise
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
            await _cleanup_codex_attempt(codex, interrupt_task, pid_file)


async def antigravity_spawner(agent: Agent, system_prompt: str, prompt: str, cwd: Path,
                              mcp_servers: dict, *, permission: str = "bypassPermissions",
                              idle_timeout: float = 600.0, subagents=(),
                              env_overrides: dict[str, str] | None = None,
                              own_process_group: bool = False,
                              mcp_profile: str = "optimize",
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
    roots = _worktree_write_roots(cwd, profile=(env_overrides or {}).get("UNITY_OPTIMIZE_PROFILE"))
    if roots:
        system_prompt += _worktree_prompt(cwd, profile=(env_overrides or {}).get("UNITY_OPTIMIZE_PROFILE"))
    full = system_prompt + _codex_mcp_note(
        mcp_profile, (env_overrides or {}).get("UNITY_OPTIMIZE_PROFILE"),
    ) + "\n\n---\n\nTASK:\n" + prompt
    cmd = [agy, "--print", full, "--model", agent.model, "--output-format", "stream-json",
           "--dangerously-skip-permissions", "--print-timeout", "72h"]
    pid_file = None
    if own_process_group and os.name == "posix":
        group_dir = Path(tempfile.mkdtemp(prefix="unity-antigravity-group-"))
        wrapped, pid_file = _process_group_wrapper(Path(agy), group_dir, "antigravity")
        cmd[0] = str(wrapped)
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
            start_new_session=False)  # The wrapper records birth immediately after setsid.

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
            try:
                if pid_file is not None:
                    await _drain_spawn_cleanup(_terminate_process_group(pid_file))
                elif proc.returncode is None:
                    proc.kill()
            finally:
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


def _optimize_external_tools_prompt(mcp_servers: dict) -> str:
    """Describe configured optimization tools without exposing credentials."""
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
        sections.append((prompt_dir / "OPTIMIZE_LEAN_TOOLS.md").read_text())
    for name in enabled:
        sections.append(
            (prompt_dir / f"OPTIMIZE_{name.upper()}_TOOLS.md").read_text()
        )
    return "\n\n".join(sections)


async def spawn(agent: Agent, system_prompt: str, prompt: str, cwd: Path,
                mcp_servers: dict, *, permission: str = "bypassPermissions",
                idle_timeout: float = 600.0, subagents=(),
                interrupt_event: asyncio.Event | None = None,
                log_context: dict | None = None,
                env_overrides: dict[str, str] | None = None,
                own_process_group: bool = False,
                mcp_profile: str = "optimize",
                on_normal_completion: CompletionCallback | None = None) -> str | None:
    backend = {"claude_code": claude_spawner, "codex": codex_spawner,
               "antigravity": antigravity_spawner}[agent.backend]
    import time
    t0 = time.monotonic()
    cleanup_failed = False
    try:
        if _stop_requested(cwd):
            return None
        phase = (log_context or {}).get("phase")
        if phase and "UNITY_OPTIMIZE_PROFILE" not in (env_overrides or {}):
            env_overrides = {**(env_overrides or {}), "UNITY_OPTIMIZE_PROFILE": phase}
        # Other optimize phases keep their own prompts, without proof-development catalogs.
        if (mcp_profile == "optimize"
                and (env_overrides or {}).get("UNITY_OPTIMIZE_PROFILE", phase) == "optimizing"):
            system_prompt += "\n\n" + _optimize_external_tools_prompt(mcp_servers)
        if mcp_profile == "optimize":
            worker_env = dict(os.environ)
            worker_env.update(_agent_env(agent, env_overrides=env_overrides))
            mcp_servers = optimize_mcp_with_runtime_env(mcp_servers, worker_env)
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
    except SpawnCleanupError:
        cleanup_failed = True
        raise
    finally:
        if not cleanup_failed:
            _write_run_log(agent, cwd, time.monotonic() - t0, log_context)
