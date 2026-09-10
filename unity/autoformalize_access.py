"""Autoformalize's worker boundary and narrow, controller-owned MCP bridge.

Workers cannot write main or shared coordination files. A per-spawn capability
exposes only the configured phase's tools, with the actor fixed by the controller.
Forum tools run outside the worker sandbox; local Lean/remote-proof clients run
inside it. This is write containment, not a network or confidentiality sandbox.
"""

from __future__ import annotations

import asyncio
import functools
import hmac
import json
import os
import secrets
import sys
import tempfile
import threading
import urllib.request
from urllib.parse import urlsplit
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import autoformalize_sandbox as sandbox

URL_ENV = "UNITY_AUTOFORMALIZE_BROKER_URL"
TOKEN_ENV = "UNITY_AUTOFORMALIZE_BROKER_TOKEN"
MAX_REQUEST = 8 * 1024 * 1024


def request(server: str, method: str, params: dict | None = None) -> dict:
    """Client used by native MCP shims and the existing shell MCP interface."""
    url, token = os.environ[URL_ENV], os.environ[TOKEN_ENV]
    # Never let a malformed inherited setting send the capability off-machine.
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("autoformalize broker must be on loopback")
    data = json.dumps({"server": server, "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(url, data, headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
    })
    # Local tool calls may include a long Lean build. No model-turn budget here.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=86400) as response:
        result = json.load(response)
    if "error" in result:
        raise ValueError(result["error"])
    return result["result"]


async def _stdio(server: str) -> None:
    """Use the MCP SDK for transport; tool authority stays in the parent."""
    from anyio.to_thread import run_sync
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    adapter = Server("unity-autoformalize-boundary")

    async def forward(method, params=None):
        return await run_sync(functools.partial(request, server, method, params), abandon_on_cancel=True)

    @adapter.list_tools()
    async def list_tools():
        return [types.Tool.model_validate(tool) for tool in (await forward("tools/list"))["tools"]]

    @adapter.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        return types.CallToolResult.model_validate(await forward("tools/call", {"name": name, "arguments": arguments}))

    async with stdio_server() as (incoming, outgoing):
        await adapter.run(incoming, outgoing, adapter.create_initialization_options())


def _write_output(allowed: dict[str, Path], path: str, content: str) -> dict:
    """Only fixed phase outputs, atomically replaced without following symlinks.

    The parent directory is controller-owned and outside worker writable roots.
    A worker cannot turn an authorized JSON output into a write-through symlink.
    """
    destination = allowed.get(path)
    if destination is None:
        destination = next((value for value in allowed.values() if str(value) == path), None)
    if destination is None:
        raise ValueError(f"not an authorized phase output: {path}")
    if destination.is_symlink():
        raise ValueError("phase output must not be a symlink")
    json.loads(content)  # Both current structured outputs are JSON.
    fd, temporary = tempfile.mkstemp(prefix=".workspace-output-", dir=destination.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"content": [{"type": "text", "text": f"Saved {path}"}], "isError": False}


class Broker:
    def __init__(self, actor: str, specs: dict, allowed_outputs: dict[str, Path],
                 artifacts_dir: Path | None = None, git_pointer: tuple[Path, bytes] | None = None):
        self.actor, self.specs, self.allowed_outputs = actor, specs, allowed_outputs
        self.artifacts_dir = artifacts_dir
        self.git_pointer = git_pointer
        self.locks = {name: asyncio.Lock() for name in [*specs, "workspace"]}
        self.clients = {}
        self.sessions = {}
        self.closed = False

    async def _session(self, server: str, ready: asyncio.Future):
        """One lifetime owner keeps MCP context entry/exit in the same task."""
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport
        try:
            spec = self.specs[server]
            transport = StdioTransport(command=spec["command"], args=spec.get("args", []),
                                       env=spec.get("env"), cwd=spec.get("cwd"), keep_alive=False)
            async with Client(transport) as client:
                ready.set_result(client)
                await asyncio.Future()  # close() cancels the owner and closes the transport.
        except BaseException as exc:
            if not ready.done():
                if isinstance(exc, asyncio.CancelledError):
                    ready.cancel()
                else:
                    ready.set_exception(exc)
                    ready.exception()  # The startup caller may already have been cancelled.
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def close(self):
        self.closed = True
        for ready in self.clients.values():
            ready.cancel()
        for task in self.sessions.values():
            task.cancel()
        await asyncio.gather(*self.sessions.values(), return_exceptions=True)

    async def call(self, server: str, method: str, params: dict) -> dict:
        if server == "forum":
            server = "unity-forum"
        if server not in self.locks:
            raise ValueError(f"server is not configured for this worker: {server}")
        async with self.locks[server]:
            return await self._call(server, method, params)

    async def _call(self, server: str, method: str, params: dict) -> dict:
        if self.closed:
            raise ValueError("worker capability has been closed")
        if method not in {"tools/list", "tools/call"}:
            raise ValueError("only tools/list and tools/call are exposed")
        if not isinstance(params, dict):
            raise ValueError("tool params must be an object")
        if server == "workspace":
            if method == "tools/list":
                return {"tools": [{"name": "write_phase_output",
                    "description": "Save an authorized shared JSON phase output: " + ", ".join(self.allowed_outputs),
                    "inputSchema": {"type": "object", "properties": {
                        "path": {"type": "string", "enum": list(self.allowed_outputs)},
                        "content": {"type": "string"}}, "required": ["path", "content"],
                        "additionalProperties": False}}] if self.allowed_outputs else []}
            if params.get("name") != "write_phase_output":
                raise ValueError("unknown workspace tool")
            return _write_output(self.allowed_outputs, **params.get("arguments", {}))
        args = dict(params.get("arguments") or {})
        if server == "unity-forum" and params.get("name") in {
            "finalize_formalization", "emit_formalization_candidate", "sync_from_main",
        } and self.git_pointer is not None:
            path, expected = self.git_pointer
            if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
                raise ValueError("worktree Git identity changed; cannot perform trusted Git mutations")
        if server == "unity-forum" and "author" in args:
            if str(args["author"]).casefold() != self.actor.casefold():
                raise ValueError("cannot act as another worker")
            args["author"] = self.actor
        if server not in self.sessions or self.sessions[server].done():
            self.clients[server] = asyncio.get_running_loop().create_future()
            self.clients[server].add_done_callback(lambda done: None if done.cancelled() else done.exception())
            self.sessions[server] = asyncio.create_task(self._session(server, self.clients[server]))
        client = await asyncio.shield(self.clients[server])
        if method == "tools/list":
            result = {"tools": [t.model_dump(mode="json", exclude_none=True) for t in await client.list_tools()]}
        else:
            value = await client.call_tool(params["name"], args, raise_on_error=False)
            result = {"content": [c.model_dump(mode="json", exclude_none=True) for c in value.content],
                      "isError": value.is_error}
            if value.structured_content is not None:
                result["structuredContent"] = value.structured_content
        if params.get("unityCompact") and self.artifacts_dir is not None and params.get("name") not in {
            "artifact_read", "artifact_snapshot_file",
        }:
            from . import artifacts
            output = "\n".join(item.get("text", "") for item in result.get("content", []))
            compacted = artifacts.compact_text(self.artifacts_dir, output, kind="mcp_output",
                                               producer=self.actor, source=f"{server}.{params['name']}")
            result = {"content": [{"type": "text", "text": artifacts.format_compacted(compacted)}],
                      "isError": result.get("isError", False)}
        return result


@asynccontextmanager
async def serve(broker: Broker):
    loop, token = asyncio.get_running_loop(), secrets.token_urlsafe(32)
    pending = set()
    pending_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Never log the capability or tool contents.

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_REQUEST:
                    raise ValueError("invalid broker request size")
                self.connection.settimeout(30)
                body = json.loads(self.rfile.read(size))
                future = asyncio.run_coroutine_threadsafe(broker.call(**body), loop)
                with pending_lock:
                    pending.add(future)
                try:
                    result = {"result": future.result()}
                finally:
                    with pending_lock:
                        pending.discard(future)
            except Exception as exc:
                result = {"error": str(exc)}
            payload = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    http.daemon_threads = True
    thread = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield {URL_ENV: f"http://127.0.0.1:{http.server_port}/", TOKEN_ENV: token}
    finally:
        broker.closed = True  # Includes authenticated requests still reading their body.
        await asyncio.to_thread(http.shutdown)
        http.server_close()
        with pending_lock:
            for future in pending:
                future.cancel()
        # Let canceled calls unwind before closing their sessions.
        await asyncio.sleep(0)
        await broker.close()
        thread.join(timeout=1)


def restricted_backend(function):
    """Only spawn() requests enforcement; direct backend tests need no project."""
    @functools.wraps(function)
    async def wrapped(agent, system_prompt, prompt, cwd, mcp_servers, **kwargs):
        context = kwargs.pop("workspace_request", None)
        if context is None:
            return await function(agent, system_prompt, prompt, cwd, mcp_servers, **kwargs)
        async with boundary(agent.name, cwd, mcp_servers, context, kwargs.get("env_overrides")) as access:
            env, servers, policy, control = access
            old_scratch = (kwargs.get("env_overrides") or {}).get("TMPDIR")
            if old_scratch:
                system_prompt = system_prompt.replace(old_scratch, env["TMPDIR"])
            note = (
                "\n\nWorkspace writes are OS-enforced for this launch (including shell commands, "
                "local MCP servers and child agents). Main/shared state is read-only to your process. "
                f"Private scratch: {env['TMPDIR']}. Use Forum tools for shared coordination and Git "
                "finalization; do not run git add/commit in a linked worktree. "
                "If a write is denied, use the correct workspace/tool or report the blocker; "
                "do not attempt to bypass the restriction."
            )
            role = context.get("role") or context.get("phase")
            if role in {"chunker", "chunking", "retrospective"}:
                output = ".unity/retrospective.json" if role == "retrospective" else ".unity/dag.json"
                note += (
                    f"\nSave `{output}` with workspace.write_phase_output(path, content), "
                    "or `unity mcp workspace write_phase_output '<JSON args>'`. "
                    f"Path is `{output}`; content is a JSON text string. Use --args-file for large arguments."
                )
            subagents = kwargs.get("subagents", ())
            if old_scratch:
                subagents = [{**s, "prompt": s["prompt"].replace(old_scratch, env["TMPDIR"])} for s in subagents]
            kwargs.update(env_overrides=env, own_process_group=True, sandbox_policy=policy,
                          sandbox_control=control,
                          subagents=[{**s, "prompt": s["prompt"] + note} for s in subagents])
            return await function(agent, system_prompt + note, prompt, cwd, servers, **kwargs)
    return wrapped


@asynccontextmanager
async def boundary(actor: str, cwd: Path, servers: dict, context: dict, env_overrides=None):
    from .config import find_unity_dir
    from .autoformalize_jobs import terminate_worker_state

    cwd = Path(cwd).resolve(strict=True)
    unity = find_unity_dir(cwd)
    if unity is None:
        raise ValueError("autoformalize workspace restriction requires a project .unity directory")
    unity = unity.resolve()
    main = unity.parent
    role = context.get("role") or context.get("phase")
    formalizer = (context.get("phase") == "formalizing" or role in {"formalizer", "formalizing", "worker"})
    formalizer = formalizer and role not in {"source_repair", "critic"}
    if formalizer and (cwd == main or not (cwd / ".git").is_file() or (cwd / ".git").is_symlink()):
        raise ValueError("formalization workers require an isolated Git worktree, not main")
    outputs = {}
    if role in {"chunker", "chunking"}:
        outputs[".unity/dag.json"] = unity / "dag.json"
    elif role == "retrospective":
        outputs[".unity/retrospective.json"] = unity / "retrospective.json"
    with tempfile.TemporaryDirectory(prefix="unity-autoformalize-access-") as temporary:
        control = Path(temporary).resolve()
        private = control / "worker"
        private.mkdir()
        scratch, sessions = private / "tmp", private / "sessions"
        scratch.mkdir()
        sessions.mkdir()
        bridge_bin = control / "bin"
        bridge_bin.mkdir()
        bridge_cli = bridge_bin / "unity"
        bridge_cli.write_text(f"#!{sys.executable} -I\nfrom unity.cli import main\nmain()\n")
        bridge_cli.chmod(0o700)
        roots = (cwd, private) if formalizer else (private,)
        if role == "retrospective":
            from .library import ensure_library
            roots += (ensure_library().resolve(),)
        # Resolved shared destinations, never the worktree's symlink objects.
        protected = (unity, main / ".git")
        if formalizer and sys.platform == "darwin":
            protected += (cwd / ".git",)  # Seatbelt supports an explicit nested exclusion.
        packages = cwd / ".lake" / "packages"
        if packages.exists() and not packages.resolve().is_relative_to(cwd):
            protected += (packages.resolve(),)
        policy = sandbox.WriteSandboxPolicy(roots, protected)
        sandbox.check_support(policy)  # No unrestricted retry on an unsupported host.
        env = {**(env_overrides or {}), "TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch),
               "UNITY_AUTOFORMALIZE_WORKER_STATE": str(private / "jobs-state"),
               "UNITY_AUTOFORMALIZE_SESSIONS": str(sessions),
               "UNITY_AUTOFORMALIZE_PROJECT_ROOT": str(main), "UNITY_AGENT_NAME": actor,
               "UNITY_AUTOFORMALIZE_PROFILE": str(role or "chunking"),
               "GIT_OPTIONAL_LOCKS": "0",
               "PYTHONDONTWRITEBYTECODE": "1", "UV_CACHE_DIR": str(private / "uv-cache"),
               "XDG_CACHE_HOME": str(private / "cache")}
        env["PATH"] = str(bridge_bin) + os.pathsep + env.get("PATH", os.environ.get("PATH", ""))
        specs = {}
        for name, spec in servers.items():
            if not spec.get("command"):
                raise ValueError(f"autoformalize boundary requires a local, configured MCP launcher: {name}")
            spec = {**spec, "env": {**spec.get("env", {}), **env}, "cwd": str(cwd)}
            argv = [spec["command"], *spec.get("args", [])]
            # lean-lsp-mcp is already a Unity dependency. Reuse that installed
            # entry point read-only instead of redownloading it per workspace.
            installed_lsp = Path(sys.executable).parent / "lean-lsp-mcp"
            if argv == ["uvx", "lean-lsp-mcp"] and installed_lsp.is_file():
                argv = [str(installed_lsp)]
            if name == "unity-forum":
                # Trusted control-plane subprocess: no worker-local job redirection.
                spec["env"].pop("UNITY_AUTOFORMALIZE_WORKER_STATE", None)
                # This service is trusted; never import from the worker cwd or
                # worker-controlled PYTHONPATH before its state checks execute.
                if argv[0] != sys.executable or argv[1:3] != ["-m", "unity.forum.autoformalize_server"]:
                    raise ValueError("unexpected autoformalize Forum launcher")
                argv.insert(1, "-I")
            else:
                argv = sandbox.command(policy, argv)
            specs[name] = {**spec, "command": argv[0], "args": argv[1:]}
        pointer = (cwd / ".git", (cwd / ".git").read_bytes()) if formalizer else None
        broker = Broker(actor, specs, outputs, unity / "artifacts", pointer)
        try:
            async with serve(broker) as capability:
                env.update(capability)
                native = {name: {"command": sys.executable,
                    "args": ["-I", "-B", "-m", "unity.autoformalize_access", "--stdio", name],
                    "env": capability} for name in [*specs, *(["workspace"] if outputs else [])]}
                yield env, native, policy, control
        finally:
            await asyncio.to_thread(terminate_worker_state, private / "jobs-state")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--stdio":
        raise SystemExit("usage: python -m unity.autoformalize_access --stdio SERVER")
    asyncio.run(_stdio(sys.argv[2]))
