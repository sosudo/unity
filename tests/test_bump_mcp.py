"""Bump shell MCP routing; all clients and transports are mocked offline."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from asyncclick.testing import CliRunner

from unity.commands import mcp as command
from unity.config import Paths
from unity import artifacts, bump_orchestrator
from unity.forum import autoformalize_server, bump_server, formalize_server, solve_server


class BumpMcpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "private-target"
        self.paths = Paths.from_unity_dir(self.root / ".unity")
        self.paths.unity.mkdir(parents=True)
        self.client = AsyncMock()
        self.client.__aenter__.return_value = self.client
        self.client.call_tool.return_value = SimpleNamespace(content=[])
        self.server = object()
        self.actual_bump_builder = bump_orchestrator.build_bump_mcp
        self.mocks = {}
        self.set_run()
        for target, options in {
            "unity.config.load_paths": {"return_value": self.paths},
            "fastmcp.Client": {"return_value": self.client},
            "fastmcp.client.transports.StdioTransport": {},
            "unity.bump_orchestrator.build_bump_mcp": {
                "return_value": {"lean-lsp": {"command": "offline-only", "args": ["fixture"]}}},
            "unity.orchestrator.build_mcp": {"side_effect": AssertionError("unexpected generic external route")},
            "unity.orchestrator.build_solve_mcp": {"side_effect": AssertionError("unexpected solve external route")},
            "unity.formalize_orchestrator.build_formalize_mcp": {"side_effect": AssertionError("unexpected Formalize external route")},
            "unity.autoformalize_orchestrator.build_autoformalize_mcp": {"side_effect": AssertionError("unexpected Autoformalize external route")},
            "subprocess.Popen": {"side_effect": AssertionError("real server forbidden")},
        }.items():
            patcher = patch(target, **options)
            self.mocks[target.rsplit(".", 1)[-1]] = patcher.start()
            self.addCleanup(patcher.stop)
        for module, key in ((bump_server, "bump"), (formalize_server, "formalize"),
                            (autoformalize_server, "autoformalize"), (solve_server, "solve")):
            for name in ("configure", "build_server"):
                patcher = patch.object(module, name, return_value=self.server if key == "bump" else object())
                self.mocks[key + "_" + name] = patcher.start()
                self.addCleanup(patcher.stop)
        environment = patch.dict("os.environ", {
            "UNITY_BUMP_PROFILE": "", "UNITY_FORMALIZE_PROFILE": "",
            "UNITY_AUTOFORMALIZE_PROFILE": "", "UNITY_SOLVE_PROFILE": "",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def set_run(self, command_name="bump", phase="formalizing"):
        (self.paths.unity / "state.json").write_text(json.dumps({"command": command_name, "phase": phase}))

    async def invoke(self, server="unity-forum", tool="bump_task", args=None):
        return await CliRunner().invoke(command.command, [server, tool, json.dumps(args or {})])

    async def test_active_bump_routes_to_private_target_owned_forum(self):
        result = await self.invoke(args={"task_id": "Project.Module"})
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["bump_configure"].assert_called_once_with(self.paths.forum / "bump", self.root, "formalizing")
        self.mocks["bump_build_server"].assert_called_once_with("formalizing")
        self.mocks["Client"].assert_called_once_with(self.server)
        self.client.call_tool.assert_awaited_once_with("bump_task", {"task_id": "Project.Module"})
        for key in ("formalize", "autoformalize", "solve"):
            self.mocks[key + "_configure"].assert_not_called()

    async def test_forum_alias_uses_same_bump_route(self):
        result = await self.invoke(server="forum")
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["bump_build_server"].assert_called_once_with("formalizing")

    async def test_active_bump_compacts_large_result_into_exact_artifact(self):
        content = "X" * 2000
        self.client.call_tool.return_value = SimpleNamespace(content=[SimpleNamespace(text=content)])
        with patch.dict("os.environ", {"UNITY_ARTIFACT_THRESHOLD_BYTES": "100",
                                       "UNITY_ARTIFACT_PREVIEW_BYTES": "60", "UNITY_AGENT_NAME": "Ada"}):
            result = await self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Full output: artifact-", result.output)
        self.assertNotIn(content, result.output)
        records = list((self.paths.artifacts / "records").glob("artifact-*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertEqual(record["kind"], "mcp_output")
        self.assertEqual(record["producer"], "Ada")
        self.assertEqual(record["source"], "unity-forum.bump_task")
        self.assertEqual(artifacts.artifact_bytes(self.paths.artifacts, record["artifact_id"]), content.encode())
        self.mocks["Popen"].assert_not_called()

    async def test_each_supported_phase_uses_its_exact_profile(self):
        self.assertEqual(bump_server.PROFILES, {"formalizing", "critic", "retrospective"})
        for phase in sorted(bump_server.PROFILES):
            with self.subTest(phase=phase):
                self.set_run(phase=phase)
                self.mocks["bump_build_server"].reset_mock()
                result = await self.invoke()
                self.assertEqual(result.exit_code, 0, result.output)
                self.mocks["bump_build_server"].assert_called_once_with(phase)

    async def test_critic_override_beats_global_formalizing_phase(self):
        with patch.dict("os.environ", {"UNITY_BUMP_PROFILE": "critic"}):
            result = await self.invoke(tool="submit_formalization_verdict")
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["bump_configure"].assert_called_once_with(self.paths.forum / "bump", self.root, "critic")
        self.mocks["bump_build_server"].assert_called_once_with("critic")

    async def test_removed_or_invalid_explicit_phases_rejected_before_routing(self):
        for phase in ("chunking", "source_repair", "representation_review", "solving", "invalid"):
            for server in ("unity-forum", "lean-lsp"):
                with self.subTest(phase=phase, server=server), patch.dict("os.environ", {"UNITY_BUMP_PROFILE": phase}):
                    result = await self.invoke(server=server)
                    self.assertNotEqual(result.exit_code, 0)
                    self.assertIn("unknown bump tool profile", result.output)
        self.mocks["Client"].assert_not_called()
        self.mocks["StdioTransport"].assert_not_called()
        self.mocks["bump_configure"].assert_not_called()
        self.mocks["build_bump_mcp"].assert_not_called()

    async def test_unknown_active_state_phase_fails_instead_of_old_chunking_default(self):
        self.set_run(phase="preparing")
        result = await self.invoke()
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("unknown bump tool profile", result.output)
        self.mocks["bump_build_server"].assert_not_called()
        self.mocks["Client"].assert_not_called()

    async def test_missing_state_phase_defaults_to_formalizing_not_chunking(self):
        (self.paths.unity / "state.json").write_text(json.dumps({"command": "bump"}))
        result = await self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["bump_build_server"].assert_called_once_with("formalizing")

    async def test_other_pipeline_overrides_cannot_change_bump(self):
        with patch.dict("os.environ", {"UNITY_FORMALIZE_PROFILE": "source_repair",
                                       "UNITY_AUTOFORMALIZE_PROFILE": "critic", "UNITY_SOLVE_PROFILE": "solving"}):
            result = await self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["bump_build_server"].assert_called_once_with("formalizing")

    async def test_linked_worker_forum_resolves_target_shared_root(self):
        worker = self.root / ".worktrees/bump-worker"
        worker.mkdir(parents=True)
        (worker / ".unity").symlink_to(self.paths.unity, target_is_directory=True)
        self.mocks["load_paths"].return_value = Paths.from_unity_dir(worker / ".unity")
        result = await self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["bump_configure"].assert_called_once_with(self.paths.forum / "bump", self.root, "formalizing")

    async def test_linked_worker_external_server_uses_target_owned_configuration(self):
        worker = self.root / ".worktrees/bump-worker"
        worker.mkdir(parents=True)
        (worker / ".unity").symlink_to(self.paths.unity, target_is_directory=True)
        self.mocks["load_paths"].return_value = Paths.from_unity_dir(worker / ".unity")
        result = await self.invoke(server="lean-lsp", tool="lean_diagnostic_messages", args={"file_path": "Project.lean"})
        self.assertEqual(result.exit_code, 0, result.output)
        configured, phase = self.mocks["build_bump_mcp"].call_args.args
        self.assertEqual(configured.forum, self.paths.forum / "bump")
        self.assertEqual(configured.project_root, self.root)
        self.assertEqual(phase, "formalizing")
        self.mocks["StdioTransport"].assert_called_once()
        self.client.call_tool.assert_awaited_once_with("lean_diagnostic_messages", {"file_path": "Project.lean"})
        self.mocks["Popen"].assert_not_called()

    async def test_critic_external_service_request_rejected_before_client(self):
        self.mocks["build_bump_mcp"].side_effect = self.actual_bump_builder
        with patch.dict("os.environ", {"UNITY_BUMP_PROFILE": "critic", "AXLE_API_KEY": "fixture-only",
                                       "ARISTOTLE_API_KEY": "fixture-only"}):
            for server in ("lean-lsp", "axle", "aristotle"):
                with self.subTest(server=server):
                    result = await self.invoke(server=server)
                    self.assertNotEqual(result.exit_code, 0)
                    self.assertIn("unknown server", result.output)
        self.mocks["Client"].assert_not_called()
        self.mocks["StdioTransport"].assert_not_called()

    async def test_bump_override_does_not_reconfigure_other_pipelines(self):
        for pipeline, phase in (("formalize", "chunking"), ("autoformalize", "critic"), ("solve", "solving")):
            with self.subTest(pipeline=pipeline), patch.dict("os.environ", {"UNITY_BUMP_PROFILE": "invalid-but-inactive"}):
                self.set_run(command_name=pipeline, phase=phase)
                result = await self.invoke(tool=pipeline + "_task")
                self.assertEqual(result.exit_code, 0, result.output)
                self.mocks[pipeline + "_build_server"].assert_called_once_with(phase)
        self.mocks["bump_configure"].assert_not_called()
        self.mocks["build_bump_mcp"].assert_not_called()

    async def test_done_bump_uses_generic_route_without_old_profile_validation(self):
        from unity.forum import server as generic
        before = (generic.FORUM_DIR, generic.PROJECT_ROOT, generic.ICRL_ENABLED)
        try:
            self.set_run(phase="done")
            with patch.dict("os.environ", {"UNITY_BUMP_PROFILE": "invalid-but-inactive"}):
                result = await self.invoke()
            self.assertEqual(result.exit_code, 0, result.output)
            self.mocks["bump_configure"].assert_not_called()
            self.mocks["Client"].assert_called_once_with(generic.mcp)
        finally:
            generic.FORUM_DIR, generic.PROJECT_ROOT, generic.ICRL_ENABLED = before


if __name__ == "__main__":
    unittest.main()
