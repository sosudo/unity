"""Shell MCP routing for Bump; all clients/transports are mocked offline."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from asyncclick.testing import CliRunner

from unity.commands import mcp as command
from unity.config import Paths
from unity.forum import autoformalize_server, formalize_server, bump_server, solve_server


class BumpMcpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.paths = Paths.from_unity_dir(self.root / ".unity")
        self.paths.unity.mkdir()
        self.client = AsyncMock()
        self.client.__aenter__.return_value = self.client
        self.client.call_tool.return_value = SimpleNamespace(content=[])
        self.server = object()
        self.mocks = {}
        self.set_run()
        for target, options in {
            "unity.config.load_paths": {"return_value": self.paths},
            "fastmcp.Client": {"return_value": self.client},
            "fastmcp.client.transports.StdioTransport": {},
            "unity.bump_orchestrator.build_bump_mcp": {
                "return_value": {"lean-lsp": {"command": "offline-only", "args": ["fixture"]}}},
            "unity.orchestrator.build_mcp": {"side_effect": AssertionError("generic external route")},
            "unity.orchestrator.build_solve_mcp": {"side_effect": AssertionError("solve route")},
            "unity.autoformalize_orchestrator.build_autoformalize_mcp": {
                "side_effect": AssertionError("autoformalize route")},
            "unity.formalize_orchestrator.build_formalize_mcp": {
                "side_effect": AssertionError("formalize route")},
            "subprocess.Popen": {"side_effect": AssertionError("real server forbidden")},
        }.items():
            patcher = patch(target, **options)
            self.mocks[target.rsplit(".", 1)[-1]] = patcher.start()
            self.addCleanup(patcher.stop)
        for module, key in ((bump_server, "bump"), (autoformalize_server, "autoformalize"),
                            (formalize_server, "formalize"), (solve_server, "solve")):
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

    async def test_active_bump_routes_forum_to_owned_profile_and_main_root(self):
        result = await self.invoke(args={"task_id": "gap"})
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["bump_configure"].assert_called_once_with(
            self.paths.forum / "bump", self.root, "formalizing")
        self.mocks["bump_build_server"].assert_called_once_with("formalizing")
        self.mocks["Client"].assert_called_once_with(self.server)
        self.client.call_tool.assert_awaited_once_with("bump_task", {"task_id": "gap"})
        self.mocks["autoformalize_configure"].assert_not_called()
        self.mocks["formalize_configure"].assert_not_called()
        self.mocks["solve_configure"].assert_not_called()

    async def test_forum_alias_uses_same_owned_route(self):
        result = await self.invoke(server="forum")
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["bump_build_server"].assert_called_once_with("formalizing")

    async def test_profile_override_selects_critic_not_global_phase(self):
        with patch.dict("os.environ", {"UNITY_BUMP_PROFILE": "critic"}):
            result = await self.invoke(tool="submit_formalization_verdict")
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["bump_configure"].assert_called_once_with(
            self.paths.forum / "bump", self.root, "critic")
        self.mocks["bump_build_server"].assert_called_once_with("critic")

    async def test_repair_and_representation_profiles_are_supported(self):
        for profile in ("source_repair", "representation_review"):
            with self.subTest(profile=profile), patch.dict("os.environ", {"UNITY_BUMP_PROFILE": profile}):
                self.mocks["bump_build_server"].reset_mock()
                result = await self.invoke()
                self.assertEqual(result.exit_code, 0, result.output)
                self.mocks["bump_build_server"].assert_called_once_with(profile)

    async def test_invalid_override_fails_before_any_client_or_server(self):
        with patch.dict("os.environ", {"UNITY_BUMP_PROFILE": "solving"}):
            result = await self.invoke()
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("unknown bump tool profile", result.output)
        self.mocks["Client"].assert_not_called()
        self.mocks["bump_configure"].assert_not_called()

    async def test_other_workflow_environment_overrides_do_not_change_bump(self):
        with patch.dict("os.environ", {"UNITY_FORMALIZE_PROFILE": "critic", "UNITY_AUTOFORMALIZE_PROFILE": "critic", "UNITY_SOLVE_PROFILE": "solving"}):
            result = await self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["bump_build_server"].assert_called_once_with("formalizing")

    async def test_unknown_run_phase_defaults_to_chunking(self):
        self.set_run(phase="preparing")
        result = await self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["bump_build_server"].assert_called_once_with("chunking")

    async def test_linked_worktree_resolves_shared_bump_forum(self):
        worktree = self.root / "worker"
        worktree.mkdir()
        (worktree / ".unity").symlink_to(self.paths.unity, target_is_directory=True)
        self.mocks["load_paths"].return_value = Paths.from_unity_dir(worktree / ".unity")
        result = await self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["bump_configure"].assert_called_once_with(
            self.paths.forum / "bump", self.root, "formalizing")

    async def test_source_project_routes_to_bound_target_and_target_phase(self):
        self.set_run(phase="preparing")
        run_id = "bump-123456abcdef"
        target = self.paths.unity / "bump" / run_id / "target"
        (target / ".unity").mkdir(parents=True)
        origin = {"version": 1, "project_root": str(self.root), "run_id": run_id,
                  "target_path": str(target), "status": "ready"}
        (self.paths.unity / "bump" / "active.json").write_text(json.dumps(origin))
        (target / ".unity" / "bump-origin.json").write_text(json.dumps(origin))
        (target / ".unity" / "state.json").write_text(json.dumps({"command": "bump", "phase": "critic"}))
        result = await self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["bump_configure"].assert_called_once_with(target / ".unity/forum/bump", target, "critic")

    async def test_source_pointer_must_match_its_project_before_reading_target(self):
        (self.paths.unity / "bump").mkdir()
        (self.paths.unity / "bump" / "active.json").write_text(json.dumps({
            "run_id": "bump-123456abcdef", "project_root": "/foreign", "target_path": "/foreign/target"}))
        result = await self.invoke()
        self.assertNotEqual(result.exit_code, 0)
        self.mocks["bump_configure"].assert_not_called()
        self.mocks["Client"].assert_not_called()

    async def test_bound_target_cannot_select_another_pipeline(self):
        target = self.paths.unity / "bump/bump-123456abcdef/target"
        (target / ".unity").mkdir(parents=True)
        origin = {"project_root": str(self.root), "run_id": "bump-123456abcdef",
                  "target_path": str(target)}
        (self.paths.unity / "bump/active.json").write_text(json.dumps(origin))
        (target / ".unity/bump-origin.json").write_text(json.dumps(origin))
        (target / ".unity/state.json").write_text(json.dumps({"command": "formalize", "phase": "critic"}))
        result = await self.invoke()
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("incompatible command state", result.output)
        self.mocks["formalize_configure"].assert_not_called()
        self.mocks["bump_configure"].assert_not_called()
        self.mocks["Client"].assert_not_called()

    async def test_external_server_uses_owned_config_and_mock_client(self):
        result = await self.invoke(server="lean-lsp", tool="lean_diagnostic_messages", args={"file_path": "Existing.lean"})
        self.assertEqual(result.exit_code, 0, result.output)
        args = self.mocks["build_bump_mcp"].call_args.args
        self.assertEqual(args[0].forum, self.paths.forum / "bump")
        self.assertEqual(args[0].project_root, self.root)
        self.assertEqual(args[1], "formalizing")
        self.mocks["StdioTransport"].assert_called_once()
        self.client.call_tool.assert_awaited_once_with("lean_diagnostic_messages", {"file_path": "Existing.lean"})
        self.mocks["Popen"].assert_not_called()

    async def test_done_or_other_command_never_reconfigures_bump(self):
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
        self.set_run(command_name="autoformalize", phase="critic")
        result = await self.invoke(tool="autoformalize_task")
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["bump_configure"].assert_not_called()
        self.mocks["autoformalize_build_server"].assert_called_once_with("critic")
        self.set_run(command_name="formalize", phase="critic")
        result = await self.invoke(tool="formalize_task")
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["bump_configure"].assert_not_called()
        self.mocks["formalize_build_server"].assert_called_once_with("critic")


if __name__ == "__main__":
    unittest.main()
