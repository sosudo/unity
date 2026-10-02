"""Offline regressions for uvx scratch inside Formalize's worker sandbox.

The stdio integration uses only an in-process fixture server, no uv downloads,
external services, model calls, credentials, or changes to writable roots.
"""

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from unity.config import Paths
from unity import formalize_orchestrator, formalize_runtime, formalize_spawn
from unity.roster import Agent, Roster


class FormalizeUvRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = Paths.from_unity_dir(Path(temporary.name).resolve() / ".unity")

    def runtime(self, run="run-1", agent="Luna1"):
        with patch("unity.formalize_runtime.shutil.which", return_value=None):
            return formalize_runtime._agent_runtime_env(
                self.paths, {"run_id": run, "phase": "formalizing"}, agent,
                task_id="target-1",
            )

    def test_uv_tool_paths_are_in_existing_worker_scratch(self):
        before = dict(os.environ)
        env = self.runtime()
        scratch = self.paths.unity / "tmp" / "run-1" / "Luna1"
        self.assertEqual(env["TMPDIR"], str(scratch))
        for key, name in (("UV_TOOL_DIR", "uv-tools"), ("UV_TOOL_BIN_DIR", "uv-bin")):
            self.assertEqual(Path(env[key]), scratch / name)
            self.assertTrue(Path(env[key]).is_dir())
        self.assertEqual(dict(os.environ), before)

    def test_uv_tool_paths_are_run_and_worker_scoped(self):
        paths = {self.runtime(run, agent)["UV_TOOL_DIR"]
                 for run, agent in (("run-1", "Luna1"), ("run-1", "Luna2"), ("run-2", "Luna1"))}
        self.assertEqual(len(paths), 3)
        self.assertEqual(self.runtime()["UV_TOOL_DIR"], self.runtime()["UV_TOOL_DIR"])

    def test_both_uvx_services_receive_scratch_without_credentials_or_global_mutation(self):
        runtime = {**self.runtime(), "PATH": "/worker/bin", "UNITY_REAL_LAKE": "/real/lake",
                   "CODEX_API_KEY": "not-forwarded", "ARISTOTLE_API_KEY": "not-forwarded"}
        servers = {
            "lean-lsp": {"command": "uvx", "args": ["lean-lsp-mcp"],
                         "env": {"UV_TOOL_DIR": "/old-global-tools"}},
            "axle": {"command": "uvx", "args": ["--from", "axiom-axle-mcp", "axle-mcp-server"],
                     "env": {"AXLE_API_KEY": "fixture-only", "UV_TOOL_DIR": "/old-global-tools"}},
            "aristotle": {"command": sys.executable, "env": {"ARISTOTLE_API_KEY": "fixture-only"}},
        }
        before = deepcopy(servers)
        rebound = formalize_spawn.formalize_mcp_with_runtime_env(servers, runtime)
        self.assertEqual(servers, before)
        for name in ("lean-lsp", "axle"):
            for key in formalize_spawn._FORMALIZE_UVX_ENV_KEYS:
                self.assertEqual(rebound[name]["env"][key], runtime[key])
            self.assertNotIn("CODEX_API_KEY", rebound[name]["env"])
            self.assertNotIn("ARISTOTLE_API_KEY", rebound[name]["env"])
        self.assertEqual(rebound["axle"]["env"]["AXLE_API_KEY"], "fixture-only")
        self.assertNotIn("UNITY_REAL_LAKE", rebound["axle"]["env"])
        self.assertEqual(rebound["lean-lsp"]["env"]["UNITY_REAL_LAKE"], "/real/lake")
        self.assertIs(rebound["aristotle"], servers["aristotle"])

    def test_axle_only_and_absent_worker_values_remove_stale_runtime(self):
        servers = {"axle": {"command": "uvx", "env": {
            "AXLE_API_KEY": "fixture-only", "UV_TOOL_DIR": "/previous-worker",
            "UV_TOOL_BIN_DIR": "/previous-worker-bin", "TMPDIR": "/previous-worker-tmp",
        }}}
        rebound = formalize_spawn.formalize_mcp_with_runtime_env(servers, {})
        self.assertEqual(rebound["axle"]["env"], {"AXLE_API_KEY": "fixture-only"})

    def test_remote_services_and_unknown_services_are_not_rebound(self):
        servers = {"axle": {"url": "https://fixture.invalid"},
                   "other": {"command": "uvx", "env": {"UV_TOOL_DIR": "/untouched"}}}
        self.assertIs(formalize_spawn.formalize_mcp_with_runtime_env(servers, self.runtime()), servers)

    def test_shell_bridge_configuration_forwards_uv_environment_to_both_services(self):
        runtime = {**self.runtime(), "AXLE_API_KEY": "fixture-only", "CODEX_API_KEY": "not-forwarded"}
        with patch.dict(os.environ, runtime, clear=True):
            servers = formalize_orchestrator.build_formalize_mcp(self.paths, "formalizing")
        for name in ("lean-lsp", "axle"):
            self.assertEqual(servers[name]["env"]["UV_TOOL_DIR"], runtime["UV_TOOL_DIR"])
            self.assertEqual(servers[name]["env"]["UV_TOOL_BIN_DIR"], runtime["UV_TOOL_BIN_DIR"])
            self.assertNotIn("CODEX_API_KEY", servers[name]["env"])

    def test_native_spawn_rebinds_controller_config_to_worker_uv_paths(self):
        runtime = self.runtime()
        servers = {name: {"command": "uvx", "env": {"UV_TOOL_DIR": "/controller-tools"}}
                   for name in ("lean-lsp", "axle")}
        backend = AsyncMock(return_value="done")
        agent = Agent(name="Luna1", model="fixture", provider="openai", backend="codex",
                      strength=1.0, base_url=None, api_key=None, auth_token=None,
                      budget=None, is_primary=True)
        with patch.object(formalize_spawn, "codex_spawner", backend), \
                patch.object(formalize_spawn, "_stop_requested", return_value=False), \
                patch.object(formalize_spawn, "_write_run_log"):
            asyncio.run(formalize_spawn.spawn(
                agent, "system", "prompt", self.paths.project_root, servers,
                env_overrides=runtime,
            ))
        sent = backend.call_args.args[4]
        for name in ("lean-lsp", "axle"):
            self.assertEqual(sent[name]["env"]["UV_TOOL_DIR"], runtime["UV_TOOL_DIR"])
            self.assertEqual(sent[name]["env"]["UV_TOOL_BIN_DIR"], runtime["UV_TOOL_BIN_DIR"])

    def test_nonformalizing_dispatch_uses_same_scratch_and_preserves_phase_overrides(self):
        self.paths.unity.mkdir()
        agent = Agent(name="Luna1", model="fixture", provider="openai", backend="codex",
                      strength=1.0, base_url=None, api_key=None, auth_token=None,
                      budget=None, is_primary=True)
        backend = AsyncMock(return_value="done")
        with patch.object(formalize_orchestrator, "spawn", backend), \
                patch.object(formalize_orchestrator, "stop_requested", return_value=False), \
                patch.object(formalize_orchestrator, "load_prompt", return_value="fixture"), \
                patch.object(formalize_orchestrator.library, "library_context", return_value=""), \
                patch.object(formalize_orchestrator.library, "library_subagents", return_value=()):
            for phase in ("chunking", "critic", "retrospective"):
                with self.subTest(phase=phase):
                    asyncio.run(formalize_orchestrator.dispatch(
                        [agent], Roster([agent]), "system", "task", self.paths.project_root, {},
                        brief_provider=lambda name: "", log_context={"run_id": "run-1", "phase": phase},
                        env_overrides={"UNITY_FORMALIZE_DRAFT_PATH": "/fixture/draft"},
                    ))
                    env = backend.call_args.kwargs["env_overrides"]
                    self.assertEqual(env["UV_TOOL_DIR"], self.runtime()["UV_TOOL_DIR"])
                    self.assertEqual(env["UV_TOOL_BIN_DIR"], self.runtime()["UV_TOOL_BIN_DIR"])
                    self.assertEqual(env["UNITY_FORMALIZE_DRAFT_PATH"], "/fixture/draft")


class FormalizeUvStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_transport_preserves_explicit_uv_paths_for_both_services(self):
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        fixture = (
            "import os\n"
            "from mcp.server.fastmcp import FastMCP\n"
            "server = FastMCP('offline-runtime-probe')\n"
            "@server.tool()\n"
            "def runtime_probe() -> str:\n"
            "    import json\n"
            "    return json.dumps({k: os.environ.get(k) for k in "
            "('UV_TOOL_DIR', 'UV_TOOL_BIN_DIR', 'TMPDIR', 'UNRELATED_SECRET')})\n"
            "server.run()\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths.from_unity_dir(Path(directory).resolve() / ".unity")
            runtime = formalize_runtime._agent_runtime_env(paths, {"run_id": "probe"}, "Luna1")
            specs = {name: {"command": sys.executable, "args": ["-c", fixture]}
                     for name in ("lean-lsp", "axle")}
            with patch.dict(os.environ, {"UV_TOOL_DIR": "/forbidden-global-tools",
                                         "UNRELATED_SECRET": "not-forwarded"}):
                rebound = formalize_spawn.formalize_mcp_with_runtime_env(specs, runtime)
                for name, spec in rebound.items():
                    with self.subTest(service=name):
                        transport = StdioTransport(command=spec["command"], args=spec["args"],
                                                   env=spec["env"], cwd=directory, keep_alive=False)
                        async with Client(transport) as client:
                            result = await client.call_tool("runtime_probe", {})
                        observed = json.loads(result.content[0].text)
                        for key in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "TMPDIR"):
                            self.assertEqual(observed[key], runtime[key])
                        self.assertIsNone(observed["UNRELATED_SECRET"])


if __name__ == "__main__":
    unittest.main()
