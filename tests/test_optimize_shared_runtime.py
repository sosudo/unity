"""Offline shared-runtime contracts; fake SDKs and temporary Git fixtures only."""

import asyncio
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from unity import optimize_spawn, optimize_worktree, spawn, worktree
from unity.execution_policy import ExecutionPolicy
from unity.forum import optimize_server
from unity.roster import Agent


def agent(backend="claude_code"):
    return Agent("fixture", "fixture-model", "fixture-provider", backend, 1,
                 None, None, None, None, True)


def forum_config():
    return {"unity-forum": {"command": sys.executable, "args": [
        "-m", "unity.forum.optimize_server", "--forum-dir", "/fixture/forum",
        "--project-root", "/fixture", "--profile", "optimizing"]}}


class SharedExecutionAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_optimize_backend_entrypoints_delegate_to_shared_loops(self):
        for name, backend in (("claude_spawner", "claude_code"),
                              ("codex_spawner", "codex"),
                              ("antigravity_spawner", "antigravity")):
            with self.subTest(backend=backend), patch.object(spawn, name, new_callable=AsyncMock) as shared:
                shared.return_value = "done"
                callback = AsyncMock(return_value=None)
                self.assertEqual(await getattr(optimize_spawn, name)(
                    agent(backend), "system", "task", Path("/fixture"), {},
                    on_normal_completion=callback), "done")
                policy = shared.call_args.kwargs["execution_policy"]
                self.assertIsInstance(policy, ExecutionPolicy)
                self.assertIs(policy.hooks, optimize_spawn)
                self.assertIs(policy.on_normal_completion, callback)
                # An adapter must not quietly grow a second retry/stream loop.
                tree = ast.parse(Path(optimize_spawn.__file__).read_text())
                function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                                and node.name == name)
                self.assertFalse(any(isinstance(node, (ast.For, ast.AsyncFor, ast.While))
                                     for node in ast.walk(function)))

    async def test_no_policy_is_implicit_for_existing_dispatch_callers(self):
        for backend, name in (("claude_code", "claude_spawner"),
                              ("codex", "codex_spawner"),
                              ("antigravity", "antigravity_spawner")):
            with self.subTest(backend=backend), patch.object(spawn, name, new_callable=AsyncMock) as shared, \
                    patch.object(spawn, "_write_run_log"):
                await spawn.spawn(agent(backend), "system", "task", Path("/fixture"), {})
                self.assertNotIn("execution_policy", shared.call_args.kwargs)

    async def test_optimize_dispatch_binds_score_env_without_new_role_or_scratch(self):
        env = {"UNITY_OPTIMIZE_PROFILE": "optimizing",
               "UNITY_OPTIMIZE_SCORE_REQUEST_ID": "score-1",
               "UNITY_OPTIMIZE_SCRATCH": "/old-writer/scratch"}
        with patch.object(optimize_spawn, "_stop_requested", return_value=False), \
                patch.object(spawn, "spawn", new_callable=AsyncMock) as shared:
            await optimize_spawn.spawn(agent(), "system", "task", Path("/fixture"), forum_config(),
                                       env_overrides=env)
        options = shared.call_args.kwargs
        self.assertEqual(options["env_overrides"]["UNITY_OPTIMIZE_SCRATCH"], "")
        self.assertEqual(options["env_overrides"]["UNITY_OPTIMIZE_PROFILE"], "optimizing")
        servers = shared.call_args.args[4]
        self.assertEqual(servers["unity-forum"]["env"]["UNITY_OPTIMIZE_SCORE_REQUEST_ID"], "score-1")
        self.assertNotIn("External services enabled", shared.call_args.args[1])
        self.assertEqual(env["UNITY_OPTIMIZE_SCRATCH"], "/old-writer/scratch")

    async def test_optimize_prelaunch_stop_is_accounted_without_backend_launch(self):
        with patch.object(optimize_spawn, "_stop_requested", return_value=True), \
                patch.object(optimize_spawn, "_write_run_log") as account, \
                patch.object(spawn, "spawn", new_callable=AsyncMock) as shared:
            self.assertIsNone(await optimize_spawn.spawn(agent(), "system", "task", Path("/fixture"), {}))
        shared.assert_not_called()
        account.assert_called_once()
        self.assertEqual(account.call_args.args[:2], (agent(), Path("/fixture")))

    async def test_optimize_accounting_includes_adapter_preparation_time_once(self):
        with patch.object(optimize_spawn, "_stop_requested", return_value=False), \
                patch.object(optimize_spawn, "_write_run_log") as account, \
                patch.object(spawn, "claude_spawner", new_callable=AsyncMock, return_value="done"), \
                patch.object(optimize_spawn.time, "monotonic", side_effect=[10.0, 13.5]):
            self.assertEqual(await optimize_spawn.spawn(agent(), "system", "task", Path("/fixture"), {}), "done")
        account.assert_called_once_with(agent(), Path("/fixture"), 3.5, None)

    async def test_score_claude_is_read_only_but_keeps_socket_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(optimize_spawn, "_worktree_write_roots", return_value=(root,)):
                prepared = optimize_spawn._claude_options(root, "system", {}, "bypassPermissions", {
                    "UNITY_OPTIMIZE_PROFILE": "optimizing",
                    "UNITY_OPTIMIZE_SCORE_REQUEST_ID": "score-1"})
            try:
                options = prepared["options"]
                filesystem = json.loads(options["settings"])["sandbox"]["filesystem"]
                self.assertEqual(filesystem["denyWrite"], [str(root.resolve())])
                self.assertIn(str(prepared["scratch"].resolve()), filesystem["allowWrite"])
                self.assertNotIn("plan.json", prepared["system_prompt"])
                hook = options["hooks"]["PreToolUse"][0].hooks[0]
                result = await hook({"tool_name": "Write", "tool_input": {
                    "file_path": str(root / "Main.lean")}}, None, None)
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
            finally:
                optimize_spawn._finish_claude(prepared["scratch"], True, False)

    def test_claude_policy_failure_removes_unstarted_socket_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scratch = root / "scratch"
            scratch.mkdir()
            with patch.object(optimize_spawn, "_worktree_write_roots", return_value=(root,)), \
                    patch.object(optimize_spawn, "_claude_sandbox_tmpdir", return_value=scratch), \
                    patch("unity.optimize_permissions.claude_worktree_options", side_effect=ValueError("fixture")):
                with self.assertRaisesRegex(ValueError, "fixture"):
                    optimize_spawn._claude_options(root, "system", {}, "bypassPermissions", {})
            self.assertFalse(scratch.exists())

    async def test_antigravity_policy_safe_stop_uses_owned_receipt_not_raw_pid(self):
        stdout = SimpleNamespace(readline=AsyncMock(side_effect=[
            b'{"event":"result","result":{"response":"done","status":"SUCCESS"}}\n', b'']))
        proc = SimpleNamespace(stdout=stdout, returncode=0, pid=123456, wait=AsyncMock(return_value=0))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / "owned.pid"
            with patch("shutil.which", return_value="/fixture/agy"), \
                    patch.object(optimize_spawn, "_worktree_write_roots", return_value=()), \
                    patch.object(optimize_spawn.tempfile, "mkdtemp", return_value=str(root)), \
                    patch.object(optimize_spawn, "_process_group_wrapper", return_value=(root / "wrapper", receipt)), \
                    patch.object(asyncio, "create_subprocess_exec", new_callable=AsyncMock, return_value=proc), \
                    patch.object(optimize_spawn, "_agent_env", return_value={}), \
                    patch.object(optimize_spawn, "_stop_requested", side_effect=[False, True]), \
                    patch.object(optimize_spawn, "_terminate_process_group", new_callable=AsyncMock) as close, \
                    patch.object(os, "killpg") as raw_signal:
                result = await optimize_spawn.antigravity_spawner(agent("antigravity"), "system", "task",
                    root, {}, own_process_group=True)
            self.assertEqual(result, "done")
            raw_signal.assert_not_called()
            self.assertEqual(close.await_count, 2)
            self.assertTrue(all(call.args == (receipt,) for call in close.await_args_list))

    def test_score_codex_uses_server_score_catalog_and_no_proof_mutation_tools(self):
        servers = {**forum_config(),
                   "lean-lsp": {"command": "uvx", "args": ["lean-lsp-mcp"]},
                   "axle": {"command": "uvx", "args": ["--from", "axiom-axle-mcp", "axle-mcp-server"]},
                   "aristotle": {"command": sys.executable, "args": ["-m", "unity.aristotle"]}}
        policy = optimize_spawn._optimize_codex_tool_policy(servers, "optimizing", score_only=True)
        self.assertEqual(policy["unity-forum"], tuple(tool.__name__ for tool in optimize_server.SCORE_TOOLS))
        self.assertIn("submit_score_report", policy["unity-forum"])
        self.assertNotIn("submit_candidate", policy["unity-forum"])
        self.assertNotIn("lean_multi_attempt", policy["lean-lsp"])
        self.assertNotIn("repair_proofs", policy["axle"])
        self.assertNotIn("aristotle_submit", policy["aristotle"])
        normal = optimize_spawn._optimize_codex_tool_policy(servers, "optimizing")
        self.assertIn("submit_candidate", normal["unity-forum"])
        self.assertIn("repair_proofs", normal["axle"])


class SharedBackendDefaultTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_default_failed_status_and_sdk_options_are_unchanged(self):
        clients, events = [], []
        class Handle:
            async def stream(self):
                try:
                    yield SimpleNamespace(method="item/completed", payload=SimpleNamespace(
                        item=SimpleNamespace(root=SimpleNamespace(type="agentMessage", text="partial result"))))
                    yield SimpleNamespace(method="turn/completed", payload=SimpleNamespace(
                        turn=SimpleNamespace(status="failed", error=SimpleNamespace(message="fixture failure"))))
                finally:
                    events.append("stream closed")
        class Client:
            def __init__(self, config):
                self.config = config
                self.thread_start = AsyncMock(return_value=SimpleNamespace(turn=AsyncMock(return_value=Handle())))
                self.close = AsyncMock(side_effect=lambda: events.append("client closed"))
                clients.append(self)
        module = SimpleNamespace(AsyncCodex=Client, CodexConfig=lambda **kw: SimpleNamespace(**kw),
                                 Sandbox=SimpleNamespace(full_access="full", workspace_write="workspace"))
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(sys.modules, {"openai_codex": module}), \
                patch.object(spawn.tempfile, "mkdtemp", return_value=directory), \
                patch("shutil.which", return_value="/fixture/codex"), \
                patch("unity.config.find_unity_dir", return_value=Path("/fixture/.unity")), \
                patch.object(spawn, "_agent_env", return_value={"ONLY": "fixture"}), \
                patch.object(spawn, "_write_codex_config", return_value=None) as config, \
                patch.object(spawn, "_write_codex_agents") as agents, \
                patch.object(spawn, "_stop_requested", return_value=False), \
                patch.object(spawn, "_log"), \
                patch.object(spawn, "_terminate_process_group", new_callable=AsyncMock) as cleanup:
            result = await spawn.codex_spawner(agent("codex"), "system", "task", Path("/fixture"), {})
        self.assertEqual(result, "partial result")
        self.assertEqual(len(clients), 1)
        self.assertEqual(vars(clients[0].config), {
            "cwd": "/fixture", "env": {"ONLY": "fixture"}, "codex_bin": "/fixture/codex"})
        self.assertEqual(clients[0].thread_start.call_args.kwargs, {
            "model": "fixture-model", "model_provider": None, "sandbox": "full",
            "base_instructions": "system" + spawn._CODEX_MCP_NOTE, "cwd": "/fixture"})
        config.assert_called_once_with(Path(directory), agent("codex"), {}, writable_root=Path("/fixture"))
        agents.assert_called_once_with(Path(directory), ())
        cleanup.assert_awaited_once_with(None)
        self.assertEqual(events, ["stream closed", "client closed"])

    async def test_codex_default_retry_closes_transport_before_next_attempt(self):
        clients, events = [], []
        class Handle:
            def __init__(self, number):
                self.number = number
            async def stream(self):
                if self.number == 1:
                    raise RuntimeError("fixture transport failure")
                yield SimpleNamespace(method="turn/completed", payload=None)
        class Client:
            def __init__(self, config):
                number = len(clients) + 1
                clients.append(self)
                events.append(("start", number))
                self.thread_start = AsyncMock(return_value=SimpleNamespace(turn=AsyncMock(return_value=Handle(number))))
                self.close = AsyncMock(side_effect=lambda: events.append(("close", number)))
        module = SimpleNamespace(AsyncCodex=Client, CodexConfig=lambda **kw: kw,
                                 Sandbox=SimpleNamespace(full_access="full", workspace_write="workspace"))
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(sys.modules, {"openai_codex": module}), \
                patch.object(spawn.tempfile, "mkdtemp", return_value=directory), \
                patch("shutil.which", return_value="/fixture/codex"), \
                patch("unity.config.find_unity_dir", return_value=None), \
                patch.object(spawn, "_agent_env", return_value={}), \
                patch.object(spawn, "_write_codex_config", return_value=None), \
                patch.object(spawn, "_write_codex_agents"), \
                patch.object(spawn, "_stop_requested", return_value=False), \
                patch.object(spawn, "_log"), \
                patch.object(spawn, "_give_up", return_value=False) as give_up, \
                patch.object(spawn, "_retry_sleep", return_value=0), \
                patch.object(spawn, "_terminate_process_group", new_callable=AsyncMock):
            self.assertIsNone(await spawn.codex_spawner(agent("codex"), "system", "task", Path("/fixture"), {}))
        self.assertEqual(give_up.call_count, 1)
        self.assertEqual(give_up.call_args.args[1], 1)
        self.assertEqual(events, [("start", 1), ("close", 1), ("start", 2), ("close", 2)])

    async def test_codex_default_cancellation_drains_stream_before_group_cleanup(self):
        events, started, release = [], asyncio.Event(), asyncio.Event()
        class Handle:
            async def stream(self):
                try:
                    started.set()
                    await release.wait()
                    yield SimpleNamespace(method="turn/completed", payload=None)
                finally:
                    events.append("stream closed")
        class Client:
            def __init__(self, config):
                self.thread_start = AsyncMock(return_value=SimpleNamespace(turn=AsyncMock(return_value=Handle())))
            async def close(self):
                events.append("transport close")
                release.set()
        async def cleanup(_receipt):
            events.append("group cleanup")
        module = SimpleNamespace(AsyncCodex=Client, CodexConfig=lambda **kw: kw,
                                 Sandbox=SimpleNamespace(full_access="full", workspace_write="workspace"))
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(sys.modules, {"openai_codex": module}), \
                patch.object(spawn.tempfile, "mkdtemp", return_value=directory), \
                patch("shutil.which", return_value="/fixture/codex"), \
                patch("unity.config.find_unity_dir", return_value=None), \
                patch.object(spawn, "_agent_env", return_value={}), \
                patch.object(spawn, "_write_codex_config", return_value=None), \
                patch.object(spawn, "_write_codex_agents"), \
                patch.object(spawn, "_stop_requested", return_value=False), \
                patch.object(spawn, "_log"), \
                patch.object(spawn, "_terminate_process_group", side_effect=cleanup):
            task = asyncio.create_task(spawn.codex_spawner(agent("codex"), "system", "task", Path("/fixture"), {}))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        self.assertLess(events.index("transport close"), events.index("stream closed"))
        self.assertLess(events.index("stream closed"), events.index("group cleanup"))

    async def test_claude_default_sdk_options_and_result_semantics_are_unchanged(self):
        import claude_agent_sdk
        captured = []
        closed = []
        class ResultMessage:
            result, is_error, total_cost_usd, num_turns = "original result", True, 0.25, 1
        class Stream:
            def __aiter__(self):
                return self
            async def __anext__(self):
                if captured:
                    raise StopAsyncIteration
                captured.append(True)
                return ResultMessage()
            async def aclose(self):
                closed.append(True)
        with patch.object(claude_agent_sdk, "query", return_value=Stream()) as query, \
                patch.object(spawn, "_agent_env", return_value={"ONLY": "fixture"}), \
                patch.object(spawn, "_stop_requested", return_value=False), \
                patch.object(spawn, "_log"), \
                patch.object(spawn, "_terminate_process_group", new_callable=AsyncMock):
            result = await spawn.claude_spawner(agent(), "system", "task", Path("/fixture"), {},
                                               permission="bypassPermissions")
        self.assertEqual(result, "original result")
        options = query.call_args.kwargs["options"]
        self.assertEqual(options.system_prompt, "system")
        self.assertEqual(options.env, {"ONLY": "fixture"})
        self.assertEqual(options.permission_mode, "bypassPermissions")
        self.assertIsNone(options.settings)
        self.assertIsNone(options.hooks)
        self.assertEqual(closed, [True])

    async def test_antigravity_default_options_and_missing_success_status_are_unchanged(self):
        # Default callers historically accepted a result without explicit SUCCESS.
        # Optimize continuation's stronger result check must not leak to them.
        stdout = SimpleNamespace(readline=AsyncMock(side_effect=[
            b'{"event":"result","result":{"response":"done","usage":{"x":1}}}\n', b'']))
        proc = SimpleNamespace(stdout=stdout, returncode=0, wait=AsyncMock(return_value=0))
        with patch("shutil.which", return_value="/fixture/agy"), \
                patch("unity.config.find_unity_dir", return_value=Path("/fixture/.unity")), \
                patch.object(asyncio, "create_subprocess_exec", new_callable=AsyncMock, return_value=proc) as launch, \
                patch.object(spawn, "_agent_env", return_value={"ONLY": "fixture"}), \
                patch.object(spawn, "_stop_requested", return_value=False):
            result = await spawn.antigravity_spawner(agent("antigravity"), "system", "task",
                Path("/fixture"), {}, own_process_group=True)
        self.assertEqual(result, "done")
        args = launch.call_args.args
        self.assertEqual(args[:2], ("/fixture/agy", "--print"))
        self.assertEqual(args[-2:], ("--add-dir", "/fixture"))
        self.assertNotIn("--sandbox", args)
        self.assertEqual(launch.call_args.kwargs["start_new_session"], os.name == "posix")
        self.assertEqual(launch.call_args.kwargs["env"], {"ONLY": "fixture"})


class SharedWorktreeHookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shared-worktree-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "-q")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--allow-empty", "-qm", "fixture")
        (self.root / ".unity").mkdir()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True,
                              check=True).stdout.strip()

    def test_explicit_linker_uses_shared_git_creation(self):
        linker = Mock()
        path = worktree.create_worktree("A", self.root, preserve_existing=True, runtime_linker=linker)
        linker.assert_called_once_with(path, self.root)
        self.assertTrue((path / ".git").is_file())
        self.assertFalse((path / ".unity").exists())

    def test_preserve_existing_rejects_path_branch_and_dangling_symlink_without_pruning(self):
        path = worktree.agent_worktree(self.root, "A")
        path.parent.mkdir()
        for kind in ("path", "branch", "symlink"):
            with self.subTest(kind=kind):
                if kind == "path":
                    path.mkdir()
                    (path / "draft").write_text("keep")
                elif kind == "branch":
                    self.git("branch", worktree.agent_branch("A"))
                else:
                    path.symlink_to(self.root / "missing")
                before = self.git("show-ref")
                with patch.object(worktree.lake, "run") as add, \
                        patch.object(worktree, "ensure_git_excludes") as excludes:
                    with self.assertRaisesRegex(ValueError, "recovered"):
                        worktree.create_worktree("A", self.root, preserve_existing=True)
                    add.assert_not_called()
                    excludes.assert_not_called()
                self.assertEqual(self.git("show-ref"), before)
                if kind == "path":
                    self.assertEqual((path / "draft").read_text(), "keep")
                    (path / "draft").unlink()
                    path.rmdir()
                elif kind == "branch":
                    self.git("branch", "-D", worktree.agent_branch("A"))
                else:
                    self.assertTrue(path.is_symlink())
                    path.unlink()

    def test_default_keeps_existing_replacement_and_runtime_link(self):
        path = worktree.create_worktree("A", self.root)
        (path / "old-draft").write_text("fixture draft")
        self.assertEqual(worktree.create_worktree("A", self.root), path)
        self.assertFalse((path / "old-draft").exists())
        self.assertTrue((path / ".unity").is_symlink())
        self.assertEqual((path / ".unity").resolve(), (self.root / ".unity").resolve())

    def test_optimize_delegates_with_private_linker_and_collision_preservation(self):
        with patch.object(worktree, "create_worktree", return_value=Path("/fixture")) as shared:
            self.assertEqual(optimize_worktree.create_worktree("Ada", self.root), Path("/fixture"))
        shared.assert_called_once_with("optimize-Ada", self.root, preserve_existing=True,
                                       runtime_linker=optimize_worktree.link_runtime_state)


if __name__ == "__main__":
    unittest.main()
