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

from unity import optimize_spawn
from unity.forum import optimize_server
from unity.roster import Agent


def servers_for(phase, *, workflow=False):
    return {
        "unity-forum": {"command": sys.executable, "args": [
            "-m", "unity.forum.optimize_server",
            "--forum-dir", "/fixture/.unity/forum/optimize",
            "--project-root", "/fixture", "--profile", phase]},
        "lean-lsp": {"command": "uvx", "args": ["lean-lsp-mcp"]},
        "axle": {"command": "uvx", "args": ["--from", "axiom-axle-mcp", "axle-mcp-server"],
                 "env": {"AXLE_API_KEY": "fixture-only"}},
        "aristotle": {"command": sys.executable, "args": ["-m", "unity.aristotle"],
                      "env": {"ARISTOTLE_API_KEY": "fixture-only"}},
    }


def agent():
    return Agent("Luna1", "fixture", "openai", "codex", 1., None, None, None, None, True)


class OptimizeNativePolicyTests(unittest.TestCase):
    def test_workflow_transport_authorizes_exact_new_catalog(self):
        for phase, tools in optimize_server.PROFILE_TOOLS.items():
            with self.subTest(phase=phase):
                policy = optimize_spawn._optimize_codex_tool_policy(servers_for(phase, workflow=True), phase)
                self.assertEqual(policy["unity-forum"], tuple(tool.__name__ for tool in tools))
                self.assertNotIn("finalize_formalization", policy["unity-forum"])
                self.assertNotIn("validate_chunks", policy["unity-forum"])
                self.assertEqual("request_review_repair" in policy["unity-forum"], phase == "critic")
                self.assertIn("optimize_inventory", policy["unity-forum"])
                self.assertEqual("report_phase_result" in policy["unity-forum"],
                                 phase in {"architect", "exploration", "chunking"})
                self.assertEqual("submit_candidate" in policy["unity-forum"], phase == "optimizing")

    def test_external_catalog_is_explicit_and_phase_scoped(self):
        prompt_dir = Path(optimize_spawn.__file__).parent / "prompts"
        for phase in optimize_server.PROFILES:
            policy = optimize_spawn._optimize_codex_tool_policy(servers_for(phase), phase)
            with self.subTest(phase=phase):
                self.assertIn("lean_diagnostic_messages", policy["lean-lsp"])
                self.assertNotIn("lean_build", policy["lean-lsp"])
                self.assertIn("list_environments", policy["axle"])
                repair = phase == "optimizing"
                self.assertEqual("aristotle_submit" in policy["aristotle"], repair)
                self.assertEqual("aristotle_result" in policy["aristotle"], repair)
                self.assertEqual("repair_proofs" in policy["axle"], repair)
                self.assertEqual("lean_multi_attempt" in policy["lean-lsp"], repair)
                for name, filename in (("lean-lsp", "LEAN"), ("axle", "AXLE"), ("aristotle", "ARISTOTLE")):
                    prompt = (prompt_dir / f"OPTIMIZE_{filename}_TOOLS.md").read_text()
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
                    optimize_spawn._optimize_codex_tool_policy(config, "chunking")
        with self.assertRaisesRegex(ValueError, "unknown optimize MCP phase"):
            optimize_spawn._optimize_codex_tool_policy({}, "unrecognized")

    def test_existing_tool_restrictions_are_not_broadened(self):
        config = servers_for("optimizing")
        config["axle"]["enabled_tools"] = ["check", "list_environments", "unknown_new_tool"]
        config["axle"]["disabled_tools"] = ["check"]
        policy = optimize_spawn._optimize_codex_tool_policy(config, "optimizing")
        self.assertEqual(policy["axle"], ("list_environments",))

    def test_config_and_cli_only_preapprove_explicit_enabled_tools(self):
        for phase in optimize_server.PROFILES:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                servers = servers_for(phase)
                with patch.object(Path, "home", return_value=home):
                    optimize_spawn._write_codex_config(home / "isolated", agent(), servers,
                        writable_roots=(home / "worker",), optimize_phase=phase)
                doc = tomllib.loads((home / "isolated/config.toml").read_text())
                policy = optimize_spawn._optimize_codex_tool_policy(servers, phase)
                overrides = optimize_spawn._optimize_codex_policy_overrides(policy, servers)
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
                optimize_spawn._write_codex_config(dest, agent(), {"unknown": {}}, optimize_phase="critic")
            self.assertFalse(dest.exists())

    def test_adapter_does_not_claim_subscription_native_tools_are_absent(self):
        for phase in optimize_server.PROFILES:
            note = optimize_spawn._codex_mcp_note("optimize", phase)
            self.assertNotIn("does NOT receive MCP tools", note)
            self.assertIn("native", note)
        self.assertIn("configuration blocker", optimize_spawn._OPTIMIZE_CODEX_MCP_NOTE)

    def test_transport_note_has_no_unconditional_task_or_candidate_instruction(self):
        for phase in optimize_server.PROFILES:
            with self.subTest(phase=phase):
                note = optimize_spawn._codex_mcp_note("optimize", phase)
                self.assertNotIn("Read optimize_brief and optimize_task", note)
                self.assertNotIn("submit_candidate", note)
                self.assertIn("only for a real bound task", note)
                self.assertIn("unity mcp unity-forum", note)
                self.assertIn("alias forum", note)
                if phase == "critic":
                    self.assertNotIn("--args-file", note)
                    self.assertIn("inline JSON", note)
                    self.assertIn("do not create or modify planning files", note)
                else:
                    self.assertIn('--args-file "$UNITY_OPTIMIZE_SCRATCH/plan.json"', note)
                    self.assertIn("not the source root or an ad hoc .unity-tmp", note)
                self.assertIn("artifact_read", note)
                self.assertIn("next_offset", note)
                self.assertIn("Use only tools advertised for your phase", note)

    def test_worktree_guidance_distinguishes_planning_scratch_from_source_and_review(self):
        for phase in optimize_server.PROFILES:
            with self.subTest(phase=phase):
                note = optimize_spawn._worktree_prompt(Path("/worker"), profile=phase)
                self.assertIn("When your turn permits source changes", note)
                self.assertIn("Administrative planning files are not source edits", note)
                if phase == "critic":
                    self.assertNotIn("UNITY_OPTIMIZE_SCRATCH", note)
                    self.assertIn("Review is read-only", note)
                    self.assertIn("inline JSON", note)
                else:
                    self.assertIn("UNITY_OPTIMIZE_SCRATCH", note)
                    self.assertIn("source-read-only phase", note)


class OptimizeShellEnvironmentTests(unittest.TestCase):
    def runtime(self, phase):
        return {"PATH": "/worker/lake-shim:/exact/runtime/bin:/usr/bin", "TMPDIR": "/worker/tmp",
                "TMP": "/worker/tmp", "TEMP": "/worker/tmp", "UV_TOOL_DIR": "/worker/uv-tools",
                "UV_TOOL_BIN_DIR": "/worker/uv-bin", "LAKE_CACHE_DIR": "/worker/lake-cache",
                "UNITY_OPTIMIZE_PROJECT_ROOT": "/fixture",
                "UNITY_OPTIMIZE_TASK_ID": "task-1" if phase == "optimizing" else "",
                "UNITY_OPTIMIZE_PHASE_DISPATCH_ID": "" if phase == "optimizing" else "dispatch-1",
                "UNITY_OPTIMIZE_PROFILE": phase, "UNITY_OPTIMIZE_DRAFT_PATH": "/worker/draft.json",
                "UNITY_OPTIMIZE_SCRATCH": "" if phase == "critic" else "/worker/.unity/scratch/optimize",
                "UNITY_AGENT_NAME": "Luna1"}

    def test_every_phase_pins_worker_runtime_without_secrets_or_lake_shim_requirement(self):
        for phase in optimize_server.PROFILES:
            runtime = self.runtime(phase)
            with self.subTest(phase=phase):
                overrides = optimize_spawn._optimize_codex_shell_overrides({**runtime,
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
        rebound = optimize_spawn.optimize_mcp_with_runtime_env(servers, {
            **runtime, "UNRELATED_SECRET": "not-forwarded", "CODEX_API_KEY": "not-forwarded"})
        self.assertEqual(servers, before)
        for name in ("unity-forum", "lean-lsp"):
            self.assertEqual(rebound[name]["env"], runtime)
        self.assertEqual(rebound["axle"]["env"]["AXLE_API_KEY"], "fixture-only")
        self.assertNotIn("LAKE_CACHE_DIR", rebound["axle"]["env"])
        cleared = optimize_spawn.optimize_mcp_with_runtime_env(rebound, {})
        self.assertEqual(cleared["unity-forum"]["env"], {})
        self.assertEqual(cleared["lean-lsp"]["env"], {})

    def test_critic_clears_stale_planning_scratch_in_both_transports(self):
        runtime = self.runtime("critic")
        servers = servers_for("critic")
        for name in ("unity-forum", "lean-lsp"):
            servers[name]["env"] = {"UNITY_OPTIMIZE_SCRATCH": "/old-worker/scratch"}
        rebound = optimize_spawn.optimize_mcp_with_runtime_env(servers, runtime)
        shell = tomllib.loads("\n".join(optimize_spawn._optimize_codex_shell_overrides(runtime)))
        for env in (rebound["unity-forum"]["env"], rebound["lean-lsp"]["env"],
                    shell["shell_environment_policy"]["set"]):
            self.assertEqual(env["UNITY_OPTIMIZE_SCRATCH"], "")

    def test_phase_binding_explicitly_clears_a_stale_task_in_both_transports(self):
        runtime = self.runtime("chunking")
        servers = servers_for("chunking")
        for name in ("unity-forum", "lean-lsp"):
            servers[name]["env"] = {"UNITY_OPTIMIZE_TASK_ID": "old-task",
                                     "UNITY_OPTIMIZE_PHASE_DISPATCH_ID": "old-dispatch"}
        rebound = optimize_spawn.optimize_mcp_with_runtime_env(servers, runtime)
        shell = tomllib.loads("\n".join(optimize_spawn._optimize_codex_shell_overrides(runtime)))
        for env in (rebound["unity-forum"]["env"], rebound["lean-lsp"]["env"],
                    shell["shell_environment_policy"]["set"]):
            self.assertEqual(env["UNITY_OPTIMIZE_TASK_ID"], "")
            self.assertEqual(env["UNITY_OPTIMIZE_PHASE_DISPATCH_ID"], "dispatch-1")

    def test_actual_spawner_applies_all_phase_policy_without_requesting_a_turn(self):
        for phase in optimize_server.PROFILES:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                worker = Path(directory) / "worker"
                home = Path(directory) / "isolated"
                client = SimpleNamespace(thread_start=AsyncMock(return_value=SimpleNamespace()),
                                         close=AsyncMock())
                config_ctor = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
                sdk = SimpleNamespace(AsyncCodex=Mock(return_value=client), CodexConfig=config_ctor,
                    Sandbox=SimpleNamespace(workspace_write="workspace-write", full_access="full-access", read_only="read-only"),
                    ApprovalMode=SimpleNamespace(deny_all="never"))
                with patch.dict(sys.modules, {"openai_codex": sdk}), \
                        patch.object(optimize_spawn.tempfile, "mkdtemp", return_value=str(home)), \
                        patch.object(optimize_spawn, "_worktree_write_roots", return_value=(worker,)), \
                        patch.object(optimize_spawn, "_stop_requested", return_value=True), \
                        patch.object(Path, "home", return_value=Path(directory)):
                    result = asyncio.run(optimize_spawn.codex_spawner(
                        agent(), "system", "task", worker, servers_for(phase),
                        env_overrides=self.runtime(phase)))
                self.assertIsNone(result)
                config = tomllib.loads("\n".join(config_ctor.call_args.kwargs["config_overrides"]))
                self.assertEqual(config["approval_policy"], "never")
                self.assertEqual(config["sandbox_mode"], "workspace-write")
                self.assertEqual(config["sandbox_workspace_write"]["writable_roots"], [str(worker)])
                self.assertEqual(config["shell_environment_policy"]["set"], self.runtime(phase))
                self.assertEqual(config["mcp_servers"]["unity-forum"]["enabled_tools"],
                    [tool.__name__ for tool in optimize_server.PROFILE_TOOLS[phase]])
                self.assertEqual(client.thread_start.call_args.kwargs["approval_mode"], "never")
                client.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
