"""Real-Git rollback safety with fake compiler/inspector; no Lean/providers."""

from contextlib import ExitStack
import hashlib
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unity import formalize_runtime as runtime


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


class FormalizeRollbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.name", "Unity Test")
        git(self.root, "config", "user.email", "unity@example.test")
        (self.root / ".gitignore").write_text(".unity/\n")
        (self.root / "Target.lean").write_text("theorem target : True := by sorry\n")
        (self.root / "Context.lean").write_text("def stable : Nat := 1\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "original user project")
        self.before = git(self.root, "rev-parse", "HEAD")
        git(self.root, "switch", "-c", "worker/Ada")
        self.proof = "theorem target : True := by trivial\n"
        (self.root / "Target.lean").write_text(self.proof)
        git(self.root, "add", "Target.lean")
        git(self.root, "commit", "-qm", "candidate")
        self.candidate_sha = git(self.root, "rev-parse", "HEAD")
        git(self.root, "switch", "main")
        diff = subprocess.run(["git", "diff", "--no-ext-diff", "--no-textconv", "--binary", "--full-index",
                               self.before, self.candidate_sha], cwd=self.root, capture_output=True, text=True,
                              check=True).stdout
        self.candidate = {"candidate_id": "candidate-1", "author": "Ada", "task_id": "target",
                          "base_main_sha": self.before, "commit_sha": self.candidate_sha,
                          "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
                          "outputs": [{"declaration": "target", "file": "Target.lean"}]}
        self.task = {"task_id": "target", "status": "pending"}
        self.current = {"formalization": {"contract": {"version": 3}},
                        "formal_tasks": {"target": self.task}, "formal_candidates": {}}
        self.paths = SimpleNamespace(project_root=self.root, forum=self.root / ".unity/forum",
                                     artifacts=self.root / ".unity/artifacts")
        self.paths.forum.mkdir(parents=True)

    def identity(self, root, **kwargs):
        return {"main_sha": git(root, "rev-parse", "HEAD"), "environment": {},
                "source_sha256": hashlib.sha256(b"".join(path.read_bytes() for path in sorted(root.glob("*.lean")))).hexdigest()}

    def integrate(self, build=None, *, preflight=None, review=None):
        with ExitStack() as stack:
            stack.enter_context(patch.object(runtime.formalize_state, "load_state", return_value=self.current))
            stack.enter_context(patch.object(runtime.formalize_state, "candidate_is_current", return_value=True))
            stack.enter_context(patch.object(runtime, "require_source_matches", side_effect=preflight))
            stack.enter_context(patch.object(runtime, "_candidate_preflight", return_value=[]))
            stack.enter_context(patch.object(runtime.worktree, "verify_candidate_commit", return_value=self.candidate_sha))
            stack.enter_context(patch.object(runtime.formalize_files, "validate_candidate_files", return_value=[]))
            stack.enter_context(patch.object(runtime.formalize_contract, "workspace_layout", return_value={}))
            stack.enter_context(patch.object(runtime.formalize_contract, "source_identity", side_effect=self.identity))
            stack.enter_context(patch.object(runtime.formalize_contract, "build_sources", side_effect=build,
                                             return_value={"returncode": 1, "output": "normal compiler rejection"}))
            stack.enter_context(patch.object(runtime, "_review_new_declaration", side_effect=review,
                                             return_value={"status": "passed", "issues": []}))
            return runtime._integrate_checked(self.paths, self.candidate, self.task)

    def test_ordinary_compiler_rejection_restores_only_controller_application(self):
        result = self.integrate()
        self.assertFalse(result["ok"])
        self.assertIn("normal compiler rejection", result["error"])
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), self.before)
        self.assertEqual(git(self.root, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertIn("sorry", (self.root / "Target.lean").read_text())
        self.assertEqual(git(self.root, "rev-parse", "worker/Ada"), self.candidate_sha)

    def test_build_edit_is_preserved_and_stops_integration(self):
        def build(*args, **kwargs):
            (self.root / "Context.lean").write_text("def stable : Nat := 99 -- user edit\n")
            return {"returncode": 1, "output": "build failed"}
        with self.assertRaisesRegex(ValueError, "all edits/commits preserved"):
            self.integrate(build)
        self.assertIn("99", (self.root / "Context.lean").read_text())
        self.assertEqual((self.root / "Target.lean").read_text(), self.proof)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), self.before)

    def test_external_commit_is_preserved_without_head_reset(self):
        external = []
        def build(*args, **kwargs):
            (self.root / "Context.lean").write_text("def stable : Nat := 88\n")
            git(self.root, "add", "Context.lean")
            git(self.root, "commit", "-qm", "external concurrent commit")
            external.append(git(self.root, "rev-parse", "HEAD"))
            return {"returncode": 1, "output": "build failed"}
        with self.assertRaisesRegex(ValueError, "all edits/commits preserved"):
            self.integrate(build)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), external[0])
        self.assertIn("88", (self.root / "Context.lean").read_text())

    def test_external_index_change_is_preserved(self):
        def build(*args, **kwargs):
            (self.root / "Context.lean").write_text("def stable : Nat := 77\n")
            git(self.root, "add", "Context.lean")
            return {"returncode": 1, "output": "build failed"}
        with self.assertRaisesRegex(ValueError, "all edits/commits preserved"):
            self.integrate(build)
        self.assertIn("Context.lean", git(self.root, "diff", "--cached", "--name-only"))
        self.assertIn("77", (self.root / "Context.lean").read_text())

    def test_external_assume_unchanged_edit_is_not_erased(self):
        def build(*args, **kwargs):
            git(self.root, "update-index", "--assume-unchanged", "Context.lean")
            (self.root / "Context.lean").write_text("def stable : Nat := 66\n")
            return {"returncode": 1, "output": "build failed"}
        with self.assertRaisesRegex(ValueError, "all edits/commits preserved"):
            self.integrate(build)
        self.assertIn("66", (self.root / "Context.lean").read_text())

    def test_exception_before_application_never_resets_unknown_work(self):
        def preflight(*args):
            (self.root / "Context.lean").write_text("def stable : Nat := 55\n")
            raise ValueError("preflight failed")
        with self.assertRaisesRegex(ValueError, "before controller application.*preserved"):
            self.integrate(preflight=preflight)
        self.assertIn("55", (self.root / "Context.lean").read_text())
        self.assertIn("sorry", (self.root / "Target.lean").read_text())

    def test_clean_preflight_exception_does_not_invoke_rollback_write(self):
        with patch.object(runtime, "_git", wraps=runtime._git) as commands:
            result = self.integrate(preflight=ValueError("preflight failed"))
        self.assertFalse(result["ok"])
        self.assertFalse(any(call.args[1:3] in (("reset", "--hard"), ("read-tree", "-u"))
                             for call in commands.call_args_list))
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), self.before)

    def test_conflicts_are_detected_in_temporary_index_without_touching_main(self):
        (self.root / "Target.lean").write_text("theorem target : False := by sorry\n")
        git(self.root, "add", "Target.lean")
        git(self.root, "commit", "-qm", "other accepted target update")
        head = git(self.root, "rev-parse", "HEAD")
        result = self.integrate()
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "merge_conflict")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.root, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertIn("False", (self.root / "Target.lean").read_text())

    def test_untracked_user_file_survives_normal_rollback(self):
        def build(*args, **kwargs):
            (self.root / "keep-user-notes.txt").write_text("new user notes")
            return {"returncode": 1, "output": "build failed"}
        result = self.integrate(build)
        self.assertFalse(result["ok"])
        self.assertEqual((self.root / "keep-user-notes.txt").read_text(), "new user notes")
        self.assertIn("sorry", (self.root / "Target.lean").read_text())

    def test_successful_candidate_still_commits_verified_tree(self):
        result = self.integrate(lambda *a, **k: {"returncode": 0, "output": "ok"})
        self.assertTrue(result["ok"], result)
        self.assertNotEqual(git(self.root, "rev-parse", "HEAD"), self.before)
        self.assertEqual(git(self.root, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual((self.root / "Target.lean").read_text(), self.proof)

    def test_last_moment_index_edit_is_not_committed(self):
        calls = 0
        def preflight(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.root / "Context.lean").write_text("def stable : Nat := 101 -- new user edit\n")
                git(self.root, "add", "Context.lean")
        with self.assertRaisesRegex(ValueError, "all edits/commits preserved"):
            self.integrate(lambda *a, **k: {"returncode": 0, "output": "ok"}, preflight=preflight)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), self.before)
        self.assertIn("101", (self.root / "Context.lean").read_text())
        self.assertIn("Context.lean", git(self.root, "diff", "--cached", "--name-only"))

    def test_last_moment_branch_switch_is_not_committed(self):
        calls = 0
        def preflight(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                git(self.root, "switch", "-c", "user/new-branch")
        with self.assertRaisesRegex(ValueError, "all edits/commits preserved"):
            self.integrate(lambda *a, **k: {"returncode": 0, "output": "ok"}, preflight=preflight)
        self.assertEqual(git(self.root, "branch", "--show-current"), "user/new-branch")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), self.before)


if __name__ == "__main__":
    unittest.main()
