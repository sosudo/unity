"""Offline Linux mount regressions; no model, service, or repository builds.

These tests use real Bubblewrap with Optimize's generated filesystem settings.
The mandatory-file mounts reproduce the relevant Claude 2.1.105 mount order;
they do not execute Claude or claim end-to-end native tool verification.
Only an already installed, usable local Bubblewrap is used. Nothing is installed.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from unity import optimize_spawn


# The pinned CLI appends these missing-file mounts after explicit denyWrite.
# This fixture deliberately models just that failing mount sequence, not the
# full vendor sandbox implementation, network policy, or tool-permission engine.
_MANDATORY_FILES = (
    ".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".zshrc",
    ".zprofile", ".profile", ".ripgreprc", ".mcp.json",
)
_ENVIRONMENT = {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"}


def _snapshot(root):
    result = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[relative] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
        elif path.is_dir():
            result[relative] = ("directory",)
    return result


@unittest.skipUnless(sys.platform.startswith("linux"), "requires local Linux Bubblewrap; no VM is started")
class OptimizeBubblewrapStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bwrap = shutil.which("bwrap")
        if cls.bwrap is None:
            raise unittest.SkipTest("requires already installed local Bubblewrap; never installs")
        # Check namespace availability before interpreting a mount failure as a
        # regression. Some Linux CI containers forbid the user/network namespace.
        check = subprocess.run(
            [cls.bwrap, "--die-with-parent", "--unshare-net", "--ro-bind", "/", "/",
             "--", "/bin/true"], capture_output=True, text=True, timeout=10,
            env=_ENVIRONMENT,
        )
        if check.returncode:
            reason = check.stderr.strip().splitlines()
            raise unittest.SkipTest("local Bubblewrap unavailable: " + (reason[0][:250] if reason else "namespace check failed"))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="optimize-bwrap-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source with spaces"
        self.source.mkdir()
        (self.source / "Main.lean").write_text("theorem fixture : True := by trivial\n")
        (self.source / "nested").mkdir()
        (self.source / "nested" / "Dependency.lean").write_text("def value : Nat := 1\n")
        (self.source / "alias.lean").symlink_to(self.source / "Main.lean")
        self.shared = self.root / "runtime"
        self.shared.mkdir()
        self.before = _snapshot(self.source)
        with patch.object(optimize_spawn, "_worktree_write_roots", return_value=(
                self.source, self.source / "nested", self.shared)):
            self.prepared = optimize_spawn._claude_options(
                self.source, "fixture", {}, "bypassPermissions", {
                    "UNITY_OPTIMIZE_PROFILE": "optimizing",
                    "UNITY_OPTIMIZE_SCORE_REQUEST_ID": "fixture-score",
                })
        self.addCleanup(optimize_spawn._finish_claude, self.prepared["scratch"], True, False)
        self.runner = Path(self.prepared["sdk_cwd"]).resolve()
        self.filesystem = json.loads(self.prepared["options"]["settings"])["sandbox"]["filesystem"]

    def _command(self, *, launch_cwd=None, filesystem=None):
        launch_cwd = launch_cwd or self.runner
        filesystem = filesystem or self.filesystem
        command = [self.bwrap, "--die-with-parent", "--unshare-net", "--ro-bind", "/", "/"]
        # Claude makes its process cwd writable even if it is absent from the
        # explicit allowWrite list. Keep that behavior in the regression fixture.
        allowed = list(dict.fromkeys([launch_cwd, *map(Path, filesystem["allowWrite"])]))
        for path in allowed:
            command += ["--bind", str(path), str(path)]
        for raw in filesystem.get("denyWrite", []):
            path = Path(raw)
            # Match the relevant native parent/child check. In the fixed case
            # source is already read-only through '/', not a writable subtree.
            if any(path == parent or path.is_relative_to(parent) for parent in allowed):
                command += ["--ro-bind", str(path), str(path)]
        for name in _MANDATORY_FILES:
            command += ["--ro-bind", "/dev/null", str(launch_cwd / name)]
        return command + ["--chdir", str(launch_cwd), "--"]

    def _run(self, argv, **kwargs):
        return subprocess.run(self._command(**kwargs) + argv, capture_output=True, text=True,
                              timeout=10, env=_ENVIRONMENT)

    def _assert_source_unchanged(self):
        self.assertEqual(_snapshot(self.source), self.before)
        self.assertTrue(all(not (self.source / name).exists() for name in _MANDATORY_FILES))

    def test_old_read_only_source_cwd_fails_before_command_start(self):
        old = {"allowWrite": [str(self.source)], "denyWrite": [str(self.source)]}
        result = self._run(["/bin/sh", "-c", "printf command-started"],
                           launch_cwd=self.source, filesystem=old)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("command-started", result.stdout)
        self.assertIn(".gitconfig", result.stderr)
        self.assertIn("Read-only file system", result.stderr)
        self._assert_source_unchanged()

    def test_generated_read_only_policy_starts_without_source_placeholders(self):
        self.assertFalse(self.runner.is_relative_to(self.source))
        self.assertIn(str(self.source), self.filesystem["denyWrite"])
        for raw in self.filesystem["allowWrite"]:
            self.assertFalse(Path(raw).is_relative_to(self.source))
        self.assertNotIn("add_dirs", self.prepared["options"])
        result = self._run(["/bin/sh", "-c", 'test ! -w "$1" && cat "$1"',
                            "fixture", str(self.source / "Main.lean")])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("theorem fixture", result.stdout)
        self._assert_source_unchanged()

    def test_two_source_cd_commands_keep_startup_files_in_runner(self):
        for _ in range(2):
            # Explicitly cd only inside the shell. The next sandbox still starts
            # in runner, matching the pinned CLI's process-vs-logical cwd split.
            result = self._run(["/bin/sh", "-c", 'cd -- "$1" && pwd && cat Main.lean',
                                "fixture", str(self.source)])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(self.source), result.stdout)
            self._assert_source_unchanged()

    def test_source_create_modify_delete_and_rename_are_denied(self):
        operations = (
            'printf changed > "$1/new.lean"',
            'printf changed > "$1/Main.lean"',
            'rm -- "$1/Main.lean"',
            'mv -- "$1/Main.lean" "$1/renamed.lean"',
            'printf changed > "$1/nested/Dependency.lean"',
        )
        for operation in operations:
            with self.subTest(operation=operation):
                result = self._run(["/bin/sh", "-c", operation, "fixture", str(self.source)])
                self.assertNotEqual(result.returncode, 0)
                self._assert_source_unchanged()

    def test_symlink_writes_and_symlink_creation_in_source_are_denied(self):
        outside_alias = self.shared / "source-alias.lean"
        outside_alias.symlink_to(self.source / "Main.lean")
        for alias in (self.source / "alias.lean", outside_alias):
            with self.subTest(alias=alias):
                result = self._run(["/bin/sh", "-c", 'printf changed > "$1"', "fixture", str(alias)])
                self.assertNotEqual(result.returncode, 0)
                self._assert_source_unchanged()
        result = self._run(["/bin/sh", "-c", 'ln -s -- "$1/Main.lean" "$1/new-alias"',
                            "fixture", str(self.source)])
        self.assertNotEqual(result.returncode, 0)
        self._assert_source_unchanged()


if __name__ == "__main__":
    unittest.main()
