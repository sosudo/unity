"""Offline read-only runner policy tests. No native agent or model calls."""

import copy
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from unity import optimize_permissions, optimize_spawn, optimize_verification


class ReadOnlyRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="optimize-runner-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "source with ' quotes"
        self.source.mkdir()
        self.runtime = self.base / "shared-runtime"
        self.runtime.mkdir()
        self.child = self.source / "real-cache"
        self.child.mkdir()
        self.env = {"UNITY_OPTIMIZE_PROFILE": "optimizing",
                    "UNITY_OPTIMIZE_SCORE_REQUEST_ID": "score-1",
                    "UNITY_OPTIMIZE_WORKTREE": str(self.source),
                    "UNITY_OPTIMIZE_SCRATCH": ""}

    def prepare(self, *, env=None, servers=None, roots=None):
        with patch.object(optimize_spawn, "_worktree_write_roots", return_value=(
                (self.source, self.child, self.runtime) if roots is None else roots)):
            result = optimize_spawn._claude_options(self.source, "system", servers or {},
                "bypassPermissions", self.env if env is None else env)
        self.addCleanup(optimize_spawn._finish_claude, result["scratch"], True, False)
        return result

    def test_score_and_review_launch_outside_source_with_unchanged_source_binding(self):
        for env in (self.env, {**self.env, "UNITY_OPTIMIZE_PROFILE": "critic",
                               "UNITY_OPTIMIZE_SCORE_REQUEST_ID": ""}):
            with self.subTest(profile=env["UNITY_OPTIMIZE_PROFILE"]):
                before = copy.deepcopy(env)
                prepared = self.prepare(env=env)
                runner = Path(prepared["sdk_cwd"])
                self.assertEqual(runner, prepared["scratch"].resolve() / "runner")
                self.assertTrue(runner.is_dir())
                self.assertFalse(runner.is_relative_to(self.source))
                self.assertFalse(self.source.is_relative_to(runner))
                self.assertEqual(stat.S_IMODE(runner.stat().st_mode), 0o700)
                self.assertEqual(list(runner.iterdir()), [])
                self.assertEqual(prepared["env_overrides"]["PWD"], str(runner))
                self.assertEqual(prepared["env_overrides"]["UNITY_OPTIMIZE_WORKTREE"], str(self.source))
                self.assertEqual(env, before)
                self.assertIn("absolute paths", prepared["system_prompt"])
                self.assertIn("cd -- " + shlex.quote(str(self.source)), prepared["system_prompt"])

    async def test_source_and_real_descendants_are_not_writable_and_hooks_stay_closed(self):
        alias = self.runtime / "source-alias"
        alias.symlink_to(self.source, target_is_directory=True)
        prepared = self.prepare(roots=(self.source, self.child, alias, self.runtime))
        options = prepared["options"]
        sandbox = json.loads(options["settings"])["sandbox"]
        roots = [Path(path) for path in sandbox["filesystem"]["allowWrite"]]
        self.assertFalse(any(path.is_relative_to(self.source) for path in roots))
        self.assertEqual(roots, [self.runtime, prepared["scratch"].resolve()])
        self.assertEqual(sandbox["filesystem"]["denyWrite"], [str(self.source)])
        self.assertFalse(options.get("add_dirs"))
        self.assertNotIn("permissions", json.loads(options["settings"]))
        self.assertEqual(options["permission_mode"], "dontAsk")
        self.assertTrue(sandbox["failIfUnavailable"])
        self.assertFalse(sandbox["allowUnsandboxedCommands"])
        hook = options["hooks"]["PreToolUse"][0].hooks[0]
        for tool, arguments in (("Write", {}), ("Edit", {}), ("MultiEdit", {}),
                                ("NotebookEdit", {}), ("EnterWorktree", {}), ("ExitWorktree", {}),
                                ("Bash", {"dangerouslyDisableSandbox": True})):
            result = await hook({"tool_name": tool, "tool_input": arguments}, None, None)
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_readonly_without_private_worktree_access_fails_before_scratch_creation(self):
        with patch.object(optimize_spawn, "_claude_sandbox_tmpdir") as scratch:
            with self.assertRaisesRegex(ValueError, "read-only.*worktree"):
                self.prepare(roots=())
        scratch.assert_not_called()

    def test_writable_ancestor_remains_rejected_before_filtering(self):
        with self.assertRaisesRegex(ValueError, "ancestor"):
            optimize_permissions.claude_worktree_options(self.source, (self.base,), read_only=True)

    def test_runner_cannot_be_redirected_into_source(self):
        inside = self.source / "scratch"
        inside.mkdir()
        with patch.object(optimize_spawn, "_claude_sandbox_tmpdir", return_value=inside):
            with self.assertRaisesRegex(ValueError, "outside.*source"):
                self.prepare()
        self.assertFalse(inside.exists())

    def test_runner_creation_rejects_existing_symlink(self):
        scratch = self.base / "scratch"
        scratch.mkdir()
        (scratch / "runner").symlink_to(self.source, target_is_directory=True)
        with patch.object(optimize_spawn, "_claude_sandbox_tmpdir", return_value=scratch):
            with self.assertRaises(FileExistsError):
                self.prepare()
        self.assertFalse(scratch.exists())
        self.assertTrue(self.source.is_dir())

    def test_local_mcp_cwd_is_preserved_without_changing_http_or_input(self):
        servers = {
            "lean-lsp": {"command": "uvx", "args": ["lean-lsp-mcp", "two words"],
                         "env": {"FIXTURE": "unchanged"}},
            "http": {"type": "http", "url": "https://fixture.invalid/mcp"},
        }
        before = copy.deepcopy(servers)
        prepared = self.prepare(servers=servers)
        local = prepared["mcp_servers"]["lean-lsp"]
        self.assertEqual(local["command"], sys.executable)
        adapter = str(Path(optimize_spawn.__file__).with_name("optimize_stdio_cwd.py").resolve())
        self.assertEqual(local["args"], ["-I", adapter, "--cwd",
            str(self.source), "--", "uvx", "lean-lsp-mcp", "two words"])
        self.assertEqual(local["env"], before["lean-lsp"]["env"])
        self.assertEqual(prepared["mcp_servers"]["http"], before["http"])
        self.assertEqual(servers, before)

    def test_generated_stdio_bootstrap_ignores_runner_and_pythonpath_imports(self):
        prepared = self.prepare(servers={"fixture": {"command": sys.executable, "args": [
            "-I", "-c", "import os; print(os.getcwd()); print(os.environ['PYTHONPATH'])"]}})
        runner = Path(prepared["sdk_cwd"])
        (runner / "unity").mkdir()
        (runner / "unity" / "__init__.py").write_text("raise RuntimeError('untrusted bootstrap')\n")
        (runner / "argparse.py").write_text("raise RuntimeError('untrusted argparse')\n")
        server = prepared["mcp_servers"]["fixture"]
        result = subprocess.run([server["command"], *server["args"]], cwd=runner,
            env={**os.environ, "PYTHONPATH": str(runner)}, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [str(self.source), str(runner)])

    def test_writer_keeps_original_cwd_mcp_and_source_permissions(self):
        servers = {"lean-lsp": {"command": "uvx", "args": ["lean-lsp-mcp"]}}
        prepared = self.prepare(env={"UNITY_OPTIMIZE_PROFILE": "optimizing"}, servers=servers)
        self.assertNotIn("sdk_cwd", prepared)
        self.assertNotIn("PWD", prepared["env_overrides"])
        self.assertEqual(prepared["mcp_servers"], servers)
        fs = json.loads(prepared["options"]["settings"])["sandbox"]["filesystem"]
        self.assertIn(str(self.source), fs["allowWrite"])
        self.assertNotIn("denyWrite", fs)

    def test_runtime_placeholders_do_not_change_live_source_identity(self):
        def git(*args):
            subprocess.run(["git", *args], cwd=self.source, check=True, capture_output=True)
        git("init", "--quiet")
        (self.source / "Example.lean").write_text("theorem t : True := by trivial\n")
        git("add", "Example.lean")
        git("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "fixture")
        before = optimize_verification.file_identity(self.source)
        prepared = self.prepare()
        runner = Path(prepared["sdk_cwd"])
        # Simulate mandatory mount targets while the session is still open.
        for name in (".gitconfig", ".gitmodules", ".bashrc", ".mcp.json"):
            (runner / name).touch()
            self.assertFalse((self.source / name).exists())
        self.assertEqual(optimize_verification.file_identity(self.source), before)
        self.assertTrue(runner.exists())


if __name__ == "__main__":
    unittest.main()
