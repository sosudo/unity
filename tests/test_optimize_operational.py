"""Offline Optimize namespace, worktree, Lake and readonly-review contracts."""
import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from unity import optimize_jobs, optimize_lake_guard, optimize_permissions
from unity import optimize_processes, optimize_spawn, optimize_worktree
from unity import optimize_mcp_bridge
from unity import optimize_tasks


class OptimizeOperationalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="optimize-offline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        (self.root / "A.lean").write_text("theorem a : True := by trivial\n")
        self.git("add", "A.lean")
        self.git("commit", "-qm", "fixture")
        self.unity = self.root / ".unity"
        self.unity.mkdir()
        (self.unity / "agents.yaml").write_text("fixture roster")
        (self.unity / "metrics").mkdir()
        (self.unity / "metrics" / "fixture.md").write_text("fixture metric")
        (self.root / ".lake/packages").mkdir(parents=True)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, text=True,
                              capture_output=True, check=True).stdout.strip()

    def env(self, **extra):
        return {"UNITY_REAL_LAKE": sys.executable,
                "UNITY_OPTIMIZE_PROJECT_ROOT": str(self.root),
                "UNITY_OPTIMIZE_LAKE_ARGS": '["-Kenv=dev"]', **extra}

    def test_worktree_has_private_configuration_shared_state_and_stop(self):
        tree = optimize_worktree.create_worktree("A", self.root)
        runtime = tree / ".unity"
        self.assertFalse(runtime.is_symlink())
        for child in ("forum", "artifacts", "jobs", "logs", "tmp"):
            self.assertEqual((runtime / child).resolve(), self.unity / child)
        self.assertEqual((runtime / "agents.yaml").read_text(), "fixture roster")
        (runtime / "agents.yaml").write_text("private change")
        self.assertEqual((self.unity / "agents.yaml").read_text(), "fixture roster")
        self.assertEqual((runtime / "metrics/fixture.md").read_text(), "fixture metric")
        self.assertFalse(optimize_spawn._stop_requested(tree))
        (self.unity / "stop-requested").write_text("stop")
        self.assertTrue(optimize_spawn._stop_requested(tree))

    def test_existing_worktrees_are_not_replaced_or_reset(self):
        tree = optimize_worktree.create_worktree("A", self.root)
        (tree / "A.lean").write_text("private candidate")
        with self.assertRaisesRegex(ValueError, "recovered"):
            optimize_worktree.create_worktree("A", self.root)
        with self.assertRaisesRegex(ValueError, "retains"):
            optimize_worktree.force_sync_from_main(self.root, "A")
        optimize_worktree.cleanup_worktree("A", tree, self.root)
        self.assertEqual((tree / "A.lean").read_text(), "private candidate")
        self.assertNotEqual(optimize_worktree.agent_branch("A"), "worktree/bump-A")

    def test_private_runtime_gets_exact_roots_not_main_checkout(self):
        tree = optimize_worktree.create_worktree("A", self.root)
        optimize_worktree.symlink_lake_cache(tree, self.root)
        with patch("shutil.which", return_value=None):
            roots = optimize_spawn._worktree_write_roots(tree)
        self.assertIn(tree, roots)
        self.assertNotIn(self.root, roots)
        self.assertNotIn(self.unity, roots)
        self.assertIn(self.unity / "forum", roots)
        self.assertIn(self.root / ".lake/packages", roots)

    def test_redirected_shared_runtime_is_rejected(self):
        tree = optimize_worktree.create_worktree("A", self.root)
        forum = tree / ".unity/forum"
        forum.unlink()
        forum.symlink_to(self.root)
        with patch("shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "binding changed"):
                optimize_spawn._worktree_write_roots(tree)

    def test_worker_build_allowed_but_cache_mutation_rejected(self):
        env = self.env(UNITY_OPTIMIZE_PROFILE="optimizing")
        with patch.object(optimize_jobs, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertEqual(optimize_lake_guard.run(["build"], environ=env, cwd=self.root), 0)
            self.assertTrue(run.call_args.kwargs["serialize_build"])
            for args in (["update"], ["clean"], ["exe", "cache", "get"], ["upgrade"]):
                self.assertEqual(optimize_lake_guard.run(args, environ=env, cwd=self.root), 2)
            self.assertEqual(run.call_count, 1)

    def test_architect_bootstrap_only_in_isolated_package_tree(self):
        tree = optimize_worktree.role_view(self.root, "architect")
        self.assertFalse((tree / ".lake/packages").is_symlink())
        env = self.env(UNITY_OPTIMIZE_PROFILE="architect")
        with patch.object(optimize_jobs, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertEqual(optimize_lake_guard.run(["update", "LeanArchitect"], environ=env, cwd=tree), 0)
            self.assertEqual(optimize_lake_guard.run(["update"], environ=env, cwd=tree), 2)
            self.assertEqual(optimize_lake_guard.run(["update", "unrelated"], environ=env, cwd=tree), 2)
            optimize_worktree.symlink_lake_cache(tree, self.root)
            self.assertEqual(optimize_lake_guard.run(["update", "LeanArchitect"], environ=env, cwd=tree), 2)
            self.assertEqual(run.call_count, 1)

    def test_lsp_keeps_stdio_and_never_takes_build_lock(self):
        with patch.object(optimize_jobs, "run", return_value=subprocess.CompletedProcess([], 0, None, None)) as run:
            self.assertEqual(optimize_lake_guard.run(["serve"], environ=self.env(), cwd=self.root), 0)
            self.assertEqual(run.call_args.args[1], [sys.executable, "-Kenv=dev", "serve"])
            self.assertTrue(run.call_args.kwargs["passthrough_stdio"])
            self.assertFalse(run.call_args.kwargs["serialize_build"])

    def test_real_lake_and_lean_are_resolved_without_migration_setup(self):
        with patch.dict(os.environ, {"UNITY_REAL_LAKE": sys.executable, "UNITY_REAL_LEAN": sys.executable}):
            for executable in ("lake", "lean"):
                result = optimize_jobs.run(self.root, [executable, "-c", "print('fixture')"], serialize_build=True)
                self.assertEqual(result.args[0], sys.executable)
                self.assertEqual(result.stdout, "fixture\n")
        self.assertFalse(list((self.unity / "jobs/optimize").glob("*.json")))

    def test_readonly_claude_blocks_file_edits_and_denies_source_bash_writes(self):
        options = optimize_permissions.claude_worktree_options(self.root, (self.root,), read_only=True)
        sandbox = json.loads(options["settings"])["sandbox"]
        self.assertEqual(sandbox["filesystem"]["denyWrite"], [str(self.root)])
        hook = options["hooks"]["PreToolUse"][0].hooks[0]
        result = asyncio.run(hook({"tool_name": "Edit", "tool_input": {"file_path": str(self.root / "A.lean")}}, None, None))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_retrospective_library_access_is_exact_and_phase_scoped(self):
        tree = optimize_worktree.create_worktree("retrospective", self.root)
        library = self.root / ".unity/library"
        library.mkdir()
        with patch("unity.library.library_dir", return_value=library), patch("shutil.which", return_value=None):
            roots = optimize_spawn._worktree_write_roots(tree, profile="retrospective")
            self.assertIn(library, roots)
            self.assertNotIn(self.root, roots)
            self.assertNotIn(self.unity, roots)
            for profile in ("architect", "preparation", "exploration", "chunking", "optimizing", "critic"):
                self.assertNotIn(library, optimize_spawn._worktree_write_roots(tree, profile=profile))
            options = optimize_permissions.claude_worktree_options(tree, roots, retrospective=True)
            hook = options["hooks"]["PreToolUse"][0].hooks[0]
            def decision(path):
                result = asyncio.run(hook({"tool_name": "Write", "tool_input": {"file_path": str(path)}}, None, None))
                return result["hookSpecificOutput"]["permissionDecision"]
            self.assertEqual(decision(library / "tactics/lesson.md"), "allow")
            self.assertEqual(decision(tree / "local.md"), "allow")
            self.assertEqual(decision(self.unity / "outside.md"), "deny")
            self.assertEqual(decision(self.root / "A.lean"), "deny")
            (library / "escape").symlink_to(self.root)
            self.assertEqual(decision(library / "escape/A.lean"), "deny")
            with self.assertRaisesRegex(ValueError, "not granted"):
                optimize_permissions.claude_worktree_options(tree, (tree,), retrospective=True)

    def test_library_in_other_shared_roots_does_not_grant_native_edits_or_weaken_critic(self):
        library = self.root / ".unity/library"
        library.mkdir()
        tree = optimize_worktree.create_worktree("review", self.root)
        with patch("unity.library.library_dir", return_value=library):
            for options in (
                optimize_permissions.claude_worktree_options(tree, (tree, library)),
                optimize_permissions.claude_worktree_options(tree, (tree, library), read_only=True),
                optimize_permissions.claude_worktree_options(tree, (tree, library), read_only=True, retrospective=True),
            ):
                hook = options["hooks"]["PreToolUse"][0].hooks[0]
                result = asyncio.run(hook({"tool_name": "Edit", "tool_input": {"file_path": str(library / "lesson.md")}}, None, None))
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_resume_closure_is_readonly_never_signals_saved_pids(self):
        directory = self.unity / "jobs/optimize"
        directory.mkdir(parents=True)
        record = directory / "old.json"
        record.write_text(json.dumps({"pid": 123456, "pgid": 123456}))
        before = record.read_bytes()
        with patch.object(os, "kill", side_effect=ProcessLookupError) as kill, \
                patch.object(os, "killpg", side_effect=ProcessLookupError) as killpg:
            optimize_processes.assert_closed(self.root)
            self.assertTrue(all(call.args[1] == 0 for call in kill.call_args_list + killpg.call_args_list))
        self.assertEqual(record.read_bytes(), before)

    def test_bridge_rejects_nested_pipeline_commands(self):
        with self.assertRaises(SystemExit) as exited:
            optimize_mcp_bridge.main(["prove", "x", "y"])
        self.assertEqual(exited.exception.code, 2)

    def test_bridge_requires_exact_private_worktree_binding(self):
        tree = optimize_worktree.create_worktree("A", self.root)
        env = self.env(UNITY_OPTIMIZE_WORKFLOW="metric-optimization-v1",
            UNITY_OPTIMIZE_PROFILE="optimizing", UNITY_AGENT_NAME="A",
            UNITY_OPTIMIZE_WORKTREE=str(tree))
        with patch("unity.optimize_workflow.paths_for", return_value="fixture paths"):
            paths, actual_tree, profile, author = optimize_mcp_bridge.worker_context(env, tree)
            self.assertEqual((paths, actual_tree, profile, author), ("fixture paths", tree, "optimizing", "A"))
            with self.assertRaisesRegex(ValueError, "assigned private"):
                optimize_mcp_bridge.worker_context(env, self.root)
            with self.assertRaisesRegex(ValueError, "binding"):
                optimize_mcp_bridge.worker_context({**env, "UNITY_OPTIMIZE_WORKFLOW": "solve"}, tree)

    def test_bridge_rejects_non_object_args_before_any_mcp(self):
        with self.assertRaisesRegex(ValueError, "JSON object"):
            asyncio.run(optimize_mcp_bridge.call_tool("forum", "optimize_brief", []))

    def test_critic_shell_bridge_can_publish_verdict_but_not_edit_candidates(self):
        tree = optimize_worktree.create_worktree("review", self.root)
        head = self.git("rev-parse", "HEAD")
        forum = self.unity / "forum/optimize"
        optimize_tasks.initialize(forum, self.root, run_id="fixture", base_sha=head,
            metric={"sha256": "fixture-metric"}, config={"primary": "A", "agents": ["A", "B"]})
        snapshot = {"snapshot_id": "fixture-review", "main_sha": head,
                    "metric_sha256": "fixture-metric", "config_revision": 0, "passed": True}
        snapshot["sha256"] = optimize_tasks.digest(snapshot)
        with optimize_tasks.transaction(forum) as state:
            state["review"] = {"reviewer": "A", "snapshot": snapshot}
        env = self.env(UNITY_OPTIMIZE_WORKFLOW="metric-optimization-v1",
            UNITY_OPTIMIZE_PROFILE="critic", UNITY_AGENT_NAME="A", UNITY_OPTIMIZE_WORKTREE=str(tree))
        before = (tree / "A.lean").read_bytes()
        with redirect_stdout(io.StringIO()):
            asyncio.run(optimize_mcp_bridge.call_tool("forum", "submit_optimization_verdict",
                {"author": "A", "snapshot_id": "fixture-review", "approved": True,
                 "reasoning": "fixture judgment"}, environ=env, cwd=tree))
        self.assertTrue(optimize_tasks.load(forum)["review"]["verdict"]["approved"])
        self.assertEqual((tree / "A.lean").read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "not exposed"):
            asyncio.run(optimize_mcp_bridge.call_tool("forum", "submit_candidate", {}, environ=env, cwd=tree))
        with self.assertRaisesRegex(ValueError, "author does not match"):
            asyncio.run(optimize_mcp_bridge.call_tool("forum", "submit_optimization_verdict",
                {"author": "B"}, environ=env, cwd=tree))


if __name__ == "__main__":
    unittest.main()
