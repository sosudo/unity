"""One restricted chunker workspace and its controller-owned Forum service.

Only the scratch directory is granted to the worker. The service runs outside
that sandbox and exposes the existing chunking tools, never controller methods.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import shutil
import socket
import sys
import tempfile
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from . import autoformalize_state


ENDPOINT_ENV = "UNITY_AUTOFORMALIZE_CHUNKER_ENDPOINT"
TOKEN_ENV = "UNITY_AUTOFORMALIZE_CHUNKER_TOKEN"


@dataclass
class ChunkingWorkspace:
    cwd: Path
    draft_path: Path
    env: dict[str, str] = field(repr=False)
    mcp: dict = field(repr=False)
    preserve: bool = False


@contextmanager
def _scratch_directory():
    directory = Path(tempfile.mkdtemp(prefix="unity-autoformalize-chunking-")).resolve()
    workspace = ChunkingWorkspace(directory, directory / "dag.json", {}, {})
    try:
        yield workspace
    finally:
        # The controller sets preserve only when archival failed. Retain the
        # exact draft for manual recovery rather than erase its only copy.
        if not workspace.preserve:
            shutil.rmtree(directory)


async def call_chunker_tool(endpoint: str, token: str, server: str, tool: str, arguments: dict):
    """A shell client has no local state-writing fallback or profile override."""
    from urllib.parse import urlsplit

    address = urlsplit(endpoint)
    if (address.scheme != "http" or address.hostname != "127.0.0.1"
            or not address.port or address.username or address.password
            or address.path != "/mcp" or address.query or address.fragment or not token):
        raise ValueError("invalid controller-bound chunker endpoint or token")
    if server not in {"unity-forum", "forum"}:
        raise ValueError("only unity-forum is available to the chunker")
    transport = StreamableHttpTransport(endpoint, headers={"Authorization": f"Bearer {token}"})
    async with Client(transport) as client:
        return await client.call_tool(tool, arguments)


@asynccontextmanager
async def chunking_workspace(paths, author: str, attempt: dict):
    """Yield a scratch draft and a scoped service; stop both on every exit path."""
    current = autoformalize_state.load_state(paths.forum)
    bound = next((item for item in current.get("chunking_attempts", [])
                  if item.get("attempt_id") == attempt.get("attempt_id")), None)
    if (not bound or bound.get("status") != "active" or bound.get("author") != author
            or current.get("phase") != "chunking"
            or bound.get("candidate_id") != autoformalize_state.formal_source(current).get("candidate_id")):
        raise ValueError("a chunker workspace requires its author's active chunking attempt")

    with _scratch_directory() as workspace, \
            tempfile.TemporaryFile(mode="w+b") as diagnostics, \
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        cwd, draft_path = workspace.cwd, workspace.draft_path
        (cwd / "README.md").write_text(
            "# Chunking workspace\n\n"
            "Write the proposed informal dependency DAG to dag.json here. "
            "Call validate_chunks to check it. Only the controller can accept the draft.\n\n"
            "Read-only project inputs (do not edit):\n"
            f"- Instructions: {paths.unity_md.resolve()}\n"
            f"- Sources: {(paths.unity / 'source').resolve()}\n"
            f"- Formalization plan: {(paths.unity / 'formalization-plan.json').resolve()}\n"
            "Use the supplied Forum tools for findings and source feedback. "
            "Do not import Unity internals to mutate state or run other Unity commands.\n",
            encoding="utf-8",
        )
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
        token = secrets.token_urlsafe(32)
        worker_env = {
            ENDPOINT_ENV: endpoint, TOKEN_ENV: token,
            "UNITY_AUTOFORMALIZE_PROFILE": "chunking",
        }
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "unity.autoformalize_chunking", "--serve",
                "--project-root", str(paths.project_root.resolve()),
                "--forum-dir", str(paths.forum.resolve()),
                "--draft-path", str(draft_path),
                "--author", author, "--run-id", current["run_id"],
                "--attempt-id", bound["attempt_id"],
                "--candidate-id", bound["candidate_id"],
                "--socket-fd", str(listener.fileno()),
                env={**os.environ, TOKEN_ENV: token, "UNITY_AGENT_NAME": author},
                pass_fds=(listener.fileno(),), stdout=diagnostics, stderr=diagnostics,
            )
            # Connect before dispatch: a broker failure must never silently launch
            # an unrestricted worker or fall back to an in-process Forum client.
            async def ready():
                while True:
                    if process.returncode is not None:
                        raise RuntimeError("chunker Forum service exited during startup")
                    try:
                        transport = StreamableHttpTransport(
                            endpoint, headers={"Authorization": f"Bearer {token}"},
                        )
                        async with Client(transport, timeout=1, init_timeout=1) as client:
                            await client.ping()
                        return
                    except Exception:
                        await asyncio.sleep(0.05)

            try:
                await asyncio.wait_for(ready(), timeout=20)
            except (TimeoutError, RuntimeError) as exc:
                diagnostics.seek(0)
                detail = diagnostics.read(8000).decode("utf-8", errors="replace").replace(token, "[token]")
                raise RuntimeError(f"chunker Forum service failed to start: {detail}") from exc
            workspace.env = worker_env
            workspace.mcp = {"unity-forum": {
                "url": endpoint, "headers": {"Authorization": f"Bearer {token}"},
            }}
            yield workspace
        finally:
            if process is not None:
                if process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()


def _serve(args) -> None:
    """Internal controller subprocess, not a worker-selectable service profile."""
    import uvicorn
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
    from .forum import autoformalize_server

    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        raise ValueError("chunker service requires a private per-attempt token")
    autoformalize_server.configure(
        Path(args.forum_dir), Path(args.project_root), "chunking",
        chunking_context={
            "run_id": args.run_id, "attempt_id": args.attempt_id,
            "candidate_id": args.candidate_id, "author": args.author,
            "draft_path": str(Path(args.draft_path)),
        },
    )
    autoformalize_server._require_current_chunker()
    auth = StaticTokenVerifier(tokens={token: {"client_id": args.attempt_id, "scopes": []}})
    app = autoformalize_server.build_server("chunking", auth=auth).http_app(
        path="/mcp", stateless_http=True,
    )
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    with socket.socket(fileno=args.socket_fd) as listener:
        server.run(sockets=[listener])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", required=True)
    for name in ("project-root", "forum-dir", "draft-path", "author", "run-id", "attempt-id", "candidate-id"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--socket-fd", type=int, required=True)
    _serve(parser.parse_args())


if __name__ == "__main__":
    main()
