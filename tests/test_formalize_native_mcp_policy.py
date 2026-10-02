"""Offline transport authorization/environment regressions; no providers or proofs."""

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from unity import formalize_spawn
from unity.forum import formalize_server
from unity.roster import Agent


def servers_for(phase):
    return {
        "unity-forum": {"command": sys.executable, "args": [
            "-m", "unity.forum.formalize_server", "--forum-dir", "/fixture/.unity/forum/formalize",
            "--project-root", "/fixture", "--profile", phase]},
        "lean-lsp": {"command": "uvx", "args": ["lean-lsp-mcp"]},
        "axle": {"command": "uvx", "args": ["--from", "axiom-axle-mcp", "axle-mcp-server"],
                 "env": {"AXLE_API_KEY": "fixture-only"}},
        "aristotle": {"command": sys.executable, "args": ["-m", "unity.aristotle"],
                      "env": {"ARISTOTLE_API_KEY": "fixture-only"}},
    }


def agent():
    return Agent("Luna1", "fixture", "openai", "codex", 1., None, None, None, None, True)


class FormalizeNativePolicyTests(unittest.TestCase):
    def test_forum_authorization_matches_registered_phase_exactly(self):
        for phase, tools in formalize_server.PROFILE_TOOLS.items():
            with self.subTest(phase=phase):
                config = servers_for(phase)
                before = deepcopy(config)
                policy = formalize_spawn._formalize_codex_tool_policy(config, phase)
                self.assertEqual(policy["unity-forum"], tuple(tool.__name__ for tool in tools))
                self.assertEqual(config, before)
                self.assertEqual("validate_chunks" in policy["unity-forum"], phase == "chunking")
                self.assertEqual("finalize_formalization" in policy["unity-forum"], phase == "formalizing")
                self.assertEqual("submit_formalization_verdict" in policy["unity-forum"], phase == "critic")

    def test_external_catalog_is_explicit_and_phase_scoped(self):
        prompt_dir = Path(formalize_spawn.__file__).parent / "prompts"
        for phase in formalize_server.PROFILES:
            policy = formalize_spawn._formalize_codex_tool_policy(servers_for(phase), phase)
            with self.subTest(phase=phase):
                self.assertIn("lean_diagnostic_messages", policy["lean-lsp"])
                self.assertNotIn("lean_build", policy["lean-lsp"])
                self.assertIn("list_environments", policy["axle"])
                self.assertEqual("aristotle_submit" in policy["aristotle"], phase == "formalizing")
                self.assertEqual("aristotle_result" in policy["aristotle"], phase == "formalizing")
                self.assertEqual("repair_proofs" in policy["axle"], phase == "formalizing")
                self.assertEqual("lean_multi_attempt" in policy["lean-lsp"], phase == "formalizing")
                for name, filename in (("lean-lsp", "LEAN"), ("axle", "AXLE"), ("aristotle", "ARISTOTLE")):
                    prompt = (prompt_dir / f"FORMALIZE_{filename}_TOOLS.md").read_text()
                    for tool in policy[name]:
                        self.assertRegex(prompt, r"\b" + re.escape(tool) + r"\b")

    def test_unknown_server_phase_command_module_or_profile_is_rejected(self):
        cases = []
        config = servers_for("chunking")
        config["unexpected"] = {"command": "uvx", "args": ["untrusted"]}
        cases.append(config)
        for name in servers_for("chunking"):
            config = servers_for("chunking")
            config[name]["command"] = "/untrusted/command"
            cases.append(config)
        config = servers_for("chunking")
        config["unity-forum"]["args"][1] = "unity.forum.server"
        cases.append(config)
        config = servers_for("critic")
        cases.append(config)
        config = servers_for("chunking")
        config["lean-lsp"]["url"] = "https://untrusted.invalid"
        cases.append(config)
        for config in cases:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    formalize_spawn._formalize_codex_tool_policy(config, "chunking")
        with self.assertRaisesRegex(ValueError, "unknown formalize MCP phase"):
            formalize_spawn._formalize_codex_tool_policy({}, "unrecognized")

    def test_existing_tool_restrictions_are_not_broadened(self):
        config = servers_for("formalizing")
        config["axle"]["enabled_tools"] = ["check", "list_environments", "unknown_new_tool"]
        config["axle"]["disabled_tools"] = ["check"]
        policy = formalize_spawn._formalize_codex_tool_policy(config, "formalizing")
        self.assertEqual(policy["axle"], ("list_environments",))

    def test_config_and_cli_only_preapprove_explicit_enabled_tools(self):
        for phase in formalize_server.PROFILES:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                servers = servers_for(phase)
                with patch.object(Path, "home", return_value=home):
                    formalize_spawn._write_codex_config(home / "isolated", agent(), servers,
                        writable_roots=(home / "worker",), formalize_phase=phase)
                doc = tomllib.loads((home / "isolated/config.toml").read_text())
                policy = formalize_spawn._formalize_codex_tool_policy(servers, phase)
                overrides = formalize_spawn._formalize_codex_policy_overrides(policy, servers)
                cli = tomllib.loads("\n".join(overrides))
                for name, names in policy.items():
                    for layer in (doc, cli):
                        spec = layer["mcp_servers"][name]
                        self.assertEqual(spec["enabled_tools"], list(names))
                        self.assertEqual(spec["default_tools_approval_mode"], "prompt")
                        self.assertEqual(set(spec["tools"]), set(names))
                        self.assertTrue(all(value == {"approval_mode": "approve"}
                                            for value in spec["tools"].values()))
                        self.assertEqual(spec["command"], servers[name]["command"])
                        self.assertEqual(spec["args"], servers[name]["args"])
                self.assertEqual(doc["sandbox_workspace_write"]["writable_roots"], [str(home / "worker")])
                self.assertTrue(doc["sandbox_workspace_write"]["exclude_slash_tmp"])
                self.assertNotIn("approval_policy", cli)
                self.assertNotIn("sandbox_workspace_write", cli)

    def test_validation_precedes_auth_copy_or_config_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "not-created"
            with self.assertRaises(ValueError):
                formalize_spawn._write_codex_config(dest, agent(), {"unknown": {}}, formalize_phase="critic")
            self.assertFalse(dest.exists())

    def test_adapter_does_not_claim_subscription_native_tools_are_absent(self):
        for phase in formalize_server.PROFILES:
            note = formalize_spawn._codex_mcp_note("formalize", phase)
            self.assertNotIn("does NOT receive MCP tools", note)
            self.assertIn("native", note)
        self.assertIn("configuration blocker", formalize_spawn._FORMALIZE_CODEX_MCP_NOTE)


class FormalizeShellEnvironmentTests(unittest.TestCase):
    def runtime(self, phase):
        return {"PATH": "/worker/lake-shim:/exact/runtime/bin:/usr/bin", "TMPDIR": "/worker/tmp",
                "TMP": "/worker/tmp", "TEMP": "/worker/tmp", "UV_TOOL_DIR": "/worker/uv-tools",
                "UV_TOOL_BIN_DIR": "/worker/uv-bin", "LAKE_CACHE_DIR": "/worker/lake-cache",
                "UNITY_FORMALIZE_PROJECT_ROOT": "/fixture", "UNITY_FORMALIZE_TASK_ID": "task-1",
                "UNITY_FORMALIZE_PROFILE": phase, "UNITY_FORMALIZE_DRAFT_PATH": "/worker/draft.json",
                "UNITY_AGENT_NAME": "Luna1"}

    def test_every_phase_pins_worker_runtime_without_secrets_or_lake_shim_requirement(self):
        for phase in formalize_server.PROFILES:
            runtime = self.runtime(phase)
            with self.subTest(phase=phase):
                overrides = formalize_spawn._formalize_codex_shell_overrides({**runtime,
                    "CODEX_API_KEY": "secret-not-written", "AXLE_API_KEY": "secret-not-written"})
                config = tomllib.loads("\n".join(overrides))
                self.assertFalse(config["allow_login_shell"])
                self.assertFalse(config["features"]["shell_snapshot"])
                self.assertEqual(config["shell_environment_policy"]["set"], runtime)
                self.assertNotIn("secret-not-written", "\n".join(overrides))
                self.assertNotIn("UNITY_REAL_LAKE", config["shell_environment_policy"]["set"])

    def test_native_forum_and_lean_receive_profile_author_draft_and_cache(self):
        servers = servers_for("chunking")
        servers["lean-lsp"]["env"] = {"LAKE_CACHE_DIR": "/stale/cache"}
        before = deepcopy(servers)
        runtime = self.runtime("chunking")
        rebound = formalize_spawn.formalize_mcp_with_runtime_env(servers, {
            **runtime, "UNRELATED_SECRET": "not-forwarded", "CODEX_API_KEY": "not-forwarded"})
        self.assertEqual(servers, before)
        for name in ("unity-forum", "lean-lsp"):
            self.assertEqual(rebound[name]["env"], runtime)
        self.assertEqual(rebound["axle"]["env"]["AXLE_API_KEY"], "fixture-only")
        self.assertNotIn("LAKE_CACHE_DIR", rebound["axle"]["env"])
        cleared = formalize_spawn.formalize_mcp_with_runtime_env(rebound, {})
        self.assertEqual(cleared["unity-forum"]["env"], {})
        self.assertEqual(cleared["lean-lsp"]["env"], {})

    def test_actual_spawner_applies_all_phase_policy_without_requesting_a_turn(self):
        for phase in formalize_server.PROFILES:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                worker = Path(directory) / "worker"
                home = Path(directory) / "isolated"
                client = SimpleNamespace(thread_start=AsyncMock(return_value=SimpleNamespace()),
                                         close=AsyncMock())
                config_ctor = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
                sdk = SimpleNamespace(AsyncCodex=Mock(return_value=client), CodexConfig=config_ctor,
                    Sandbox=SimpleNamespace(workspace_write="workspace-write", full_access="full-access"),
                    ApprovalMode=SimpleNamespace(deny_all="never"))
                with patch.dict(sys.modules, {"openai_codex": sdk}), \
                        patch.object(formalize_spawn.tempfile, "mkdtemp", return_value=str(home)), \
                        patch.object(formalize_spawn, "_worktree_write_roots", return_value=(worker,)), \
                        patch.object(formalize_spawn, "_stop_requested", return_value=True), \
                        patch.object(Path, "home", return_value=Path(directory)):
                    result = asyncio.run(formalize_spawn.codex_spawner(
                        agent(), "system", "task", worker, servers_for(phase),
                        env_overrides=self.runtime(phase)))
                self.assertIsNone(result)
                config = tomllib.loads("\n".join(config_ctor.call_args.kwargs["config_overrides"]))
                self.assertEqual(config["approval_policy"], "never")
                self.assertEqual(config["sandbox_mode"], "workspace-write")
                self.assertEqual(config["sandbox_workspace_write"]["writable_roots"], [str(worker)])
                self.assertEqual(config["shell_environment_policy"]["set"], self.runtime(phase))
                self.assertEqual(config["mcp_servers"]["unity-forum"]["enabled_tools"],
                    [tool.__name__ for tool in formalize_server.PROFILE_TOOLS[phase]])
                self.assertEqual(client.thread_start.call_args.kwargs["approval_mode"], "never")
                client.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
