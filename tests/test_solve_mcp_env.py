import asyncio
import copy
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from unity.orchestrator import build_mcp
from unity.solve_formal_orchestrator import build_solve_formal_mcp
from unity.roster import Agent
from unity.solve_formal_spawn import _write_codex_config, solve_mcp_with_runtime_env, spawn
from unity.spawn import _write_codex_config as prove_write_codex_config, spawn as prove_spawn


def worker(name="Ada", backend="codex"):
    return Agent(
        name=name, model="test-model", provider="test", backend=backend,
        strength=1, base_url=None, api_key="fake-agent-key", auth_token=None,
        budget=None, is_primary=True,
    )


def runtime_env(name):
    return {
        "PATH": f'/tmp/solve "{name}"/bin:/usr/bin:/bin',
        "UNITY_REAL_LAKE": "/test/real-lake",
        "UNITY_SOLVE_PROJECT_ROOT": "/test/project",
        "UNITY_SOLVE_TASK_ID": f"task-{name}",
        "UNITY_AGENT_NAME": name,
        "TMPDIR": f"/tmp/solve-{name}",
        "TMP": f"/tmp/solve-{name}",
        "TEMP": f"/tmp/solve-{name}",
    }


class SolveMcpEnvironmentTests(unittest.TestCase):
    def test_shell_builder_forwards_allowlist_to_local_lean_only(self):
        paths = SimpleNamespace(forum=Path("/test/forum"), project_root=Path("/test/project"))
        expected = runtime_env("Ada")
        environment = {
            **expected,
            "AXLE_API_KEY": "fake-axle-key",
            "ARISTOTLE_API_KEY": "fake-aristotle-key",
            "CODEX_API_KEY": "fake-model-key",
            "UNITY_UNRELATED_SECRET": "fake-unrelated-secret",
            "PIP_REQUIRE_VIRTUALENV": "true",
        }
        with patch.dict(os.environ, environment, clear=True):
            servers = build_solve_formal_mcp(paths, "formalizing")
            prove_servers = build_mcp(paths)
            self.assertEqual(dict(os.environ), environment)
        self.assertEqual(servers["lean-lsp"]["env"], expected)
        self.assertNotIn("env", servers["unity-forum"])
        self.assertEqual(servers["axle"]["env"], {"AXLE_API_KEY": "fake-axle-key"})
        self.assertEqual(servers["aristotle"]["env"], {"ARISTOTLE_API_KEY": "fake-aristotle-key"})
        self.assertNotIn("env", prove_servers["lean-lsp"])
        self.assertEqual(prove_servers["axle"], servers["axle"])

    def test_binding_is_copy_on_write_and_clears_stale_runtime_values(self):
        servers = {
            "lean-lsp": {"command": "uvx", "args": ["lean-lsp-mcp"], "env": {"LEAN_SETTING": "keep"}},
            "axle": {"command": "uvx", "env": {"AXLE_API_KEY": "fake-axle-key"}},
        }
        snapshot = copy.deepcopy(servers)
        ada = solve_mcp_with_runtime_env(servers, runtime_env("Ada"))
        grace = solve_mcp_with_runtime_env(ada, {"PATH": "/grace/bin", "UNITY_AGENT_NAME": "Grace"})
        self.assertEqual(servers, snapshot)
        self.assertEqual(ada["lean-lsp"]["env"], {"LEAN_SETTING": "keep", **runtime_env("Ada")})
        self.assertEqual(grace["lean-lsp"]["env"], {
            "LEAN_SETTING": "keep", "PATH": "/grace/bin", "UNITY_AGENT_NAME": "Grace",
        })
        self.assertIsNot(ada["lean-lsp"], servers["lean-lsp"])
        self.assertIsNot(ada["lean-lsp"]["env"], servers["lean-lsp"]["env"])
        self.assertIsNot(grace["lean-lsp"]["env"], ada["lean-lsp"]["env"])
        self.assertIs(ada["axle"], servers["axle"])

    def test_remote_or_absent_lean_server_is_not_reconfigured(self):
        for servers in ({}, {"lean-lsp": {"url": "https://example.test/mcp"}}):
            with self.subTest(servers=servers):
                self.assertIs(solve_mcp_with_runtime_env(servers, runtime_env("Ada")), servers)

    def test_codex_mcp_env_toml_round_trips_special_characters(self):
        environment = {**runtime_env("Ada🧪"), "LEAN_SETTING": 'quoted "value"\\path\n∀🧪'}
        servers = {"lean-lsp": {"command": "uvx", "args": ["lean-lsp-mcp"], "env": environment}}
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            _write_codex_config(home, worker(), servers)
            config = tomllib.loads((home / "config.toml").read_text())
        self.assertEqual(config["mcp_servers"]["lean-lsp"]["env"], environment)

    def test_codex_ordinary_prove_config_output_is_unchanged(self):
        servers = {"axle": {"command": "uvx", "args": ["axle-server"], "env": {"AXLE_API_KEY": "test-token"}}}
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            prove_write_codex_config(home, worker(), servers)
            actual = (home / "config.toml").read_text()
        self.assertEqual(actual, (
            '[sandbox_workspace_write]\nnetwork_access = true\n\n'
            '[mcp_servers.axle]\ncommand = "uvx"\nargs = ["axle-server"]\n\n'
            '[mcp_servers.axle.env]\nAXLE_API_KEY = "test-token"\n'
        ))


class SolveNativeMcpEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_workers_get_isolated_actual_environments_on_every_backend(self):
        paths = SimpleNamespace(forum=Path("/test/forum"), project_root=Path("/test/project"))
        controller_env = {**runtime_env("Controller"), "UNRELATED_SECRET": "fake-controller-secret"}
        for backend_name, backend_function in (
            ("claude_code", "claude_spawner"),
            ("codex", "codex_spawner"),
            ("antigravity", "antigravity_spawner"),
        ):
            with self.subTest(backend=backend_name), patch.dict(os.environ, controller_env, clear=True):
                servers = build_solve_formal_mcp(paths, "formalizing")
                snapshot = copy.deepcopy(servers)
                with patch(f"unity.solve_formal_spawn.{backend_function}", new_callable=AsyncMock, return_value="done") as backend, \
                     patch("unity.solve_formal_spawn._write_run_log"):
                    await asyncio.gather(*(
                        spawn(
                            worker(name, backend_name), "system", "task", Path("/test/project"), servers,
                            mcp_profile="solve", env_overrides={
                                # Agent identity must come from the real worker, not the controller config.
                                **{key: value for key, value in runtime_env(name).items() if key != "UNITY_AGENT_NAME"},
                                "UNRELATED_SECRET": "fake-worker-secret",
                            },
                        )
                        for name in ("Ada", "Grace")
                    ))
                sent = {call.args[0].name: call.args[4] for call in backend.await_args_list}
                for name in ("Ada", "Grace"):
                    self.assertEqual(sent[name]["lean-lsp"]["env"], runtime_env(name))
                    self.assertIsNot(sent[name], servers)
                    self.assertNotIn("env", sent[name]["unity-forum"])
                self.assertIsNot(sent["Ada"]["lean-lsp"]["env"], sent["Grace"]["lean-lsp"]["env"])
                self.assertEqual(servers, snapshot)
                self.assertEqual(dict(os.environ), controller_env)

    async def test_prove_dispatch_keeps_original_mcp_configuration(self):
        servers = {"lean-lsp": {"command": "uvx", "args": ["lean-lsp-mcp"]}}
        with patch("unity.spawn.codex_spawner", new_callable=AsyncMock, return_value="done") as backend, \
             patch("unity.spawn._write_run_log"):
            await prove_spawn(
                worker(), "system", "task", Path("/test/project"), servers,
                mcp_profile="prove", env_overrides=runtime_env("Ada"),
            )
        self.assertIs(backend.call_args.args[4], servers)
        self.assertNotIn("env", servers["lean-lsp"])


if __name__ == "__main__":
    unittest.main()
