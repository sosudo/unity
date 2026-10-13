"""Offline compiler-workflow evidence tests; no Lean/model/provider invocation."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from unity import bump_verification as v


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bump-verification-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "--quiet")
        self.git("config", "user.email", "test@localhost")
        self.git("config", "user.name", "Test")
        self.write(".gitignore", ".lake/\n.unity/\n.env\n")
        self.write("lean-toolchain", "leanprover/lean4:v4.34.1\n")
        self.write("lakefile.toml", 'name = "Example"\n')
        self.write("Example.lean", "theorem first : True := by sorry\ntheorem second : True := by trivial\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "baseline")
        self.base = self.git("rev-parse", "HEAD")
        self.scope = {"default_modules": {"Example": "Example.lean"},
                      "selected_modules": {"Example": "Example.lean"}, "excluded_files": {}, "build_dir": ".lake/build"}

    def git(self, *args):
        p = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def run_tool(self, root, args, **kwargs):
        if args[-1] == "--version":
            return subprocess.CompletedProcess(args, 0, "Lean/Lake fixture 4.34.1\n", "")
        return subprocess.CompletedProcess(args, 0, "✔ Built Example\nBuild completed successfully.\n", "")

    def test_mask_nested_comments_strings_and_quoted_identifiers(self):
        text = '/- sorry /- axiom -/ admit -/\n-- sorry\ndef «sorry» := "axiom \\\" sorry"\ntheorem t : True := by sorry\n'
        rows, _ = v._source_tokens(text, "A.lean")
        self.assertEqual([(r["kind"], r["line"]) for r in rows], [("sorry", 4)])

    def test_unterminated_source_is_explicit_error(self):
        with self.assertRaisesRegex(ValueError, "unterminated"):
            v.mask_source("/- unfinished")

    def test_invalid_candidate_syntax_is_repairable_audit_result(self):
        self.write("Example.lean", "def broken := 1\n/- unfinished")
        audit = v.audit_sources(self.root, self.base, self.scope)
        self.assertFalse(audit["passed"])
        self.assertTrue(any(i["kind"] == "source_audit_parse_error" for i in audit["issues"]))

    def test_old_syntax_error_does_not_require_old_successful_build(self):
        self.write("Example.lean", "def old := 1\n/- unfinished")
        self.git("add", "Example.lean")
        self.git("commit", "--quiet", "-m", "old broken source")
        base = self.git("rev-parse", "HEAD")
        self.write("Example.lean", "def old := 1\n")
        audit = v.audit_sources(self.root, base, self.scope)
        self.assertTrue(audit["passed"])
        self.assertEqual(audit["limitations"][0]["snapshot"], "baseline")

    def test_inherited_hole_survives_line_moves(self):
        self.write("Example.lean", "-- new explanation\n\ntheorem first : True := by\n  sorry\ntheorem second : True := by trivial\n")
        audit = v.audit_sources(self.root, self.base, self.scope)
        self.assertTrue(audit["passed"])
        self.assertEqual(audit["inherited"][0]["line"], 4)

    def test_equal_counts_cannot_hide_hole_transfer(self):
        self.write("Example.lean", "theorem first : True := by trivial\ntheorem second : True := by sorry\n")
        audit = v.audit_sources(self.root, self.base, self.scope)
        self.assertFalse(audit["passed"])
        self.assertEqual(audit["introduced"][0]["declaration"], "second")

    def test_anonymous_example_cannot_inherit_previous_theorems_hole(self):
        self.write("Example.lean", "theorem first : True := by trivial\nexample : True := by sorry\n")
        audit = v.audit_sources(self.root, self.base, self.scope)
        self.assertFalse(audit["passed"])
        self.assertEqual(audit["introduced"][0]["declaration"], "<anonymous-example>")

    def test_constant_assumptions_are_reported_as_new_trust(self):
        self.write("Example.lean", "constant unchecked : False\n")
        audit = v.audit_sources(self.root, self.base, self.scope)
        self.assertFalse(audit["passed"])
        self.assertEqual(audit["introduced"][0]["kind"], "constant")

    def test_new_axiom_and_admit_are_rejected(self):
        with (self.root / "Example.lean").open("a") as f:
            f.write("axiom invented : False\ntheorem bad : True := by admit\n")
        self.assertEqual({r["kind"] for r in v.audit_sources(self.root, self.base, self.scope)["introduced"]}, {"axiom", "admit"})

    def test_new_exit_is_not_success(self):
        self.write("Example.lean", "#exit\ntheorem first : True := by sorry\ntheorem second : True := by trivial\n")
        audit = v.audit_sources(self.root, self.base, self.scope)
        self.assertFalse(audit["passed"])
        self.assertTrue(any(i["kind"] == "newly_skipped_declaration" for i in audit["issues"]))

    def test_removed_source_not_silent(self):
        (self.root / "Example.lean").unlink()
        self.assertEqual(v.audit_sources(self.root, self.base, self.scope)["removed_files"], ["Example.lean"])

    def test_vendor_holes_reported_separately(self):
        self.write("vendor/dependency/Dep.lean", "theorem inherited : True := by sorry\n")
        audit = v.audit_sources(self.root, self.base, self.scope)
        self.assertTrue(audit["passed"])
        self.assertEqual(len(audit["dependency_holes"]), 1)

    def test_new_hole_in_existing_vendored_source_is_not_inherited(self):
        self.write("vendor/dependency/Dep.lean", "theorem existing : True := by trivial\n")
        self.git("add", "vendor")
        self.git("commit", "--quiet", "-m", "existing vendored dependency")
        base = self.git("rev-parse", "HEAD")
        self.write("vendor/dependency/Dep.lean", "theorem existing : True := by sorry\n")
        audit = v.audit_sources(self.root, base, self.scope)
        self.assertFalse(audit["passed"])
        self.assertEqual(audit["introduced"][0]["path"], "vendor/dependency/Dep.lean")

    def test_unexplained_scope_exclusion_fails(self):
        scope = {**self.scope, "excluded_files": {"Old.lean": ""}}
        self.assertFalse(v.audit_sources(self.root, self.base, scope)["passed"])

    def test_ignored_credentials_do_not_enter_identity_or_patch(self):
        self.write(".env", "fixture-private\n")
        self.assertNotIn(".env", v._files(self.root))
        result = v._patch_replay(self.root, self.base, v._files(self.root), self.root / ".unity/artifacts")
        patch_text = v.artifacts.artifact_bytes(self.root / ".unity/artifacts", result["patch_ref"]["artifact_id"])
        self.assertNotIn(b"fixture-private", patch_text)

    def test_external_source_symlink_fails_closed(self):
        (self.root / "external.lean").symlink_to("/tmp/outside.lean")
        with self.assertRaisesRegex(ValueError, "escapes"):
            v._files(self.root)

    @patch.object(v.bump_jobs, "run")
    def test_identity_binds_actual_executables_config_scope_vendor(self, run):
        run.side_effect = self.run_tool
        one = v.source_identity(self.root, scope=self.scope)
        self.write("vendor/Dep.lean", "def value := 1\n")
        two = v.source_identity(self.root, scope=self.scope)
        self.assertNotEqual(one["sha256"], two["sha256"])
        self.assertIn("vendor/Dep.lean", two["vendor"])
        self.assertIn("lean-toolchain", two["config"])
        self.assertEqual(two["lean"]["returncode"], 0)

    @patch.object(v.bump_jobs, "run")
    def test_broken_executable_not_accepted_from_toolchain_text(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "", "missing")
        with self.assertRaisesRegex(ValueError, "does not run"):
            v.source_identity(self.root)

    @patch.object(v.bump_jobs, "run")
    def test_default_success_covers_default_modules_only(self, run):
        run.side_effect = self.run_tool
        scope = deepcopy(self.scope)
        scope["selected_modules"]["Extra"] = "Extra.lean"
        result = v.build(self.root, scope=scope)
        self.assertTrue(result["completed"])
        self.assertEqual(result["checked_files"], ["Example.lean"])
        self.assertNotIn("Extra.lean", result["checked_files"])

    @patch.object(v.bump_jobs, "run")
    def test_build_failure_does_not_mark_unreached_files_clean(self, run):
        def execute(root, args, **kw):
            if args[-1] == "--version":
                return self.run_tool(root, args, **kw)
            return subprocess.CompletedProcess(args, 1, "Example.lean:1:1: error: unknown module 'Missing'\nerror: build failed\n", "")
        run.side_effect = execute
        result = v.build(self.root, scope=self.scope)
        self.assertFalse(result["completed"])
        self.assertEqual(result["checked_files"], [])
        self.assertEqual(result["blocked_files"], ["Example.lean"])

    @patch.object(v.bump_jobs, "run")
    def test_unlocated_failure_preserved(self, run):
        def execute(root, args, **kw):
            return self.run_tool(root, args, **kw) if args[-1] == "--version" else subprocess.CompletedProcess(args, 1, "", "")
        run.side_effect = execute
        self.assertEqual(len(v.build(self.root, scope=self.scope)["unmapped"]), 1)

    @patch.object(v.bump_jobs, "run")
    def test_source_rewrite_during_build_invalidates_receipt(self, run):
        def execute(root, args, **kw):
            if args[-1] != "--version":
                self.write("Example.lean", "def changed := 3\n")
            return self.run_tool(root, args, **kw)
        run.side_effect = execute
        self.assertFalse(v.build(self.root, scope=self.scope)["passed"])

    @patch.object(v.bump_jobs, "run")
    def test_fresh_build_retains_only_project_outputs_dependencies_untouched(self, run):
        run.side_effect = self.run_tool
        self.write(".lake/build/lib/lean/Example.olean", "old-output")
        dep = self.write(".lake/packages/mathlib/.lake/build/lib/Mathlib.olean", "dependency-cache")
        result = v.build(self.root, scope=self.scope, fresh_project=True)
        self.assertEqual(dep.read_text(), "dependency-cache")
        retained = self.root / result["fresh_project"]["retained_outputs"]
        self.assertEqual((retained / "lib/lean/Example.olean").read_text(), "old-output")

    def test_unsafe_fresh_directory_rejected(self):
        for name in (".", ".lake", ".lake/packages", "vendor", "../out", "/tmp/out", ".unity"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                v._fresh_project_outputs(self.root, name)

    def test_fresh_directory_cannot_contain_tracked_source(self):
        self.write("build/Source.lean", "def x := 1")
        self.git("add", "build/Source.lean")
        with self.assertRaisesRegex(ValueError, "tracked"):
            v._fresh_project_outputs(self.root, "build")

    def test_binary_patch_checks_all_current_files(self):
        (self.root / "image.bin").write_bytes(bytes(range(256)))
        self.write("Example.lean", "theorem first : True := by trivial\ntheorem second : True := by trivial\n")
        result = v._patch_replay(self.root, self.base, v._files(self.root), self.root / ".unity/artifacts")
        self.assertTrue(result["passed"])
        self.assertEqual(result["checked_files"], 5)

    @patch.object(v.bump_jobs, "run")
    def test_final_builds_supplemental_modules_and_fresh_snapshot(self, run):
        run.side_effect = self.run_tool
        self.write("Extra.lean", "def extra := 1\n")
        self.scope["selected_modules"]["Extra"] = "Extra.lean"
        snapshot = v.verify_final(self.root, base_sha=self.base, scope=self.scope)
        self.assertTrue(snapshot["passed"])
        self.assertEqual(len(snapshot["builds"]), 2)
        self.assertIn("Extra.lean", snapshot["checked_files"])
        self.assertTrue(snapshot["builds"][0]["fresh_project"]["fresh"])

    def candidate(self):
        old = {"id": "a", "path": "Example.lean", "kind": "declaration", "severity": "error", "line": 1, "message": "first error"}
        task = {"files": ["Example.lean"], "chunk_ids": ["c"]}
        inventory = {"chunks": {"c": {"path": "Example.lean", "start_line": 1, "end_line": 2}},
                     "files": {"Example.lean": {"imports": []}, "Other.lean": {"imports": []}, "Dep.lean": {"imports": ["Example"]}}}
        return old, task, inventory

    def test_partial_same_file_progress_can_integrate(self):
        old, task, inventory = self.candidate()
        sibling = {**old, "id": "b", "line": 4, "message": "sibling error"}
        build = {"passed": False, "errors": [sibling], "checked_files": [], "unmapped": []}
        self.assertTrue(v.candidate_progress([old, sibling], build, task, inventory)["accepted"])

    def test_disappeared_diagnostic_behind_import_failure_not_progress(self):
        old, task, inventory = self.candidate()
        error = {**old, "kind": "import", "message": "unknown module"}
        build = {"passed": False, "errors": [error], "checked_files": [], "unmapped": []}
        self.assertFalse(v.candidate_progress([old], build, task, inventory)["accepted"])

    def test_unreached_chunk_cannot_be_marked_repaired(self):
        old, task, inventory = self.candidate()
        build = {"passed": False, "errors": [], "checked_files": [], "unmapped": []}
        self.assertFalse(v.candidate_progress([old], build, task, inventory)["accepted"])

    def test_unrelated_regression_rejected(self):
        old, task, inventory = self.candidate()
        build = {"passed": False, "errors": [{**old, "path": "Other.lean"}], "checked_files": ["Example.lean"]}
        self.assertFalse(v.candidate_progress([old], build, task, inventory)["accepted"])

    def test_newly_exposed_dependent_error_allowed(self):
        old, task, inventory = self.candidate()
        build = {"passed": False, "errors": [{**old, "path": "Dep.lean"}], "checked_files": ["Example.lean"]}
        self.assertTrue(v.candidate_progress([old], build, task, inventory)["accepted"])
        before = {"diagnostics": [old], "checked_files": ["Dep.lean"]}
        self.assertFalse(v.candidate_progress(before, build, task, inventory)["accepted"])

    def report_fixture(self):
        identity = {"main_sha": "head", "scope": self.scope}
        identity["sha256"] = v.digest(identity)
        snapshot = {"snapshot_id": "s", "passed": True, "main_sha": "head", "scope": self.scope,
                    "authors": ["Writer"],
                    "source_identity": identity, "checked_files": ["Example.lean"], "hole_audit": {"passed": True, "issues": []},
                    "patch_replay": {"passed": True}, "issues": [], "builds": [{"fresh_project": {"fresh": True},
                        "passed": True, "completed": True, "returncode": 0, "source_stable": True, "errors": []}]}
        snapshot["sha256"] = v.digest(snapshot)
        state = {"phase": "complete", "main_sha": "head", "candidates": {"c": {"status": "merged", "author": "Writer"}},
                 "review": {"reviewer": "Writer"},
                 "workflow_changes": [{"problem": "race", "change": "serialize", "result": "passed"}]}
        verdict = {"verdict": "approved", "author": "Writer", "snapshot_id": "s", "snapshot_sha256": snapshot["sha256"]}
        return state, snapshot, verdict

    def test_primary_contributor_review_is_accepted_and_snapshot_bound(self):
        state, snapshot, verdict = self.report_fixture()
        self.assertEqual(v.completion_report(state, snapshot, verdict)["status"], "accepted")
        for key, value in (("author", "Other"), ("snapshot_id", "other"),
                           ("snapshot_sha256", "stale"), ("verdict", "rejected")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                v.completion_report(state, snapshot, {**verdict, key: value})

    def test_report_requires_controller_assigned_primary_not_verdict_author(self):
        for reviewer in (None, "", "Other"):
            state, snapshot, verdict = self.report_fixture()
            state["review"] = {} if reviewer is None else {"reviewer": reviewer}
            with self.subTest(reviewer=reviewer), self.assertRaisesRegex(ValueError, "primary review"):
                v.completion_report(state, snapshot, verdict)

    def test_primary_approval_does_not_bypass_reopened_tasks(self):
        for field in ("reopen_task_ids", "reopen_tasks"):
            state, snapshot, verdict = self.report_fixture()
            verdict[field] = ["task"]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "primary review"):
                v.completion_report(state, snapshot, verdict)

    def test_mutated_passed_snapshot_rejected(self):
        state, snapshot, verdict = self.report_fixture()
        snapshot["scope"] = {}
        with self.assertRaises(ValueError):
            v.completion_report(state, snapshot, verdict)

    def test_pass_flag_cannot_replace_build_audit_and_patch_receipts(self):
        for field in ("builds", "hole_audit", "patch_replay", "checked_files"):
            state, snapshot, verdict = self.report_fixture()
            snapshot[field] = [] if field in {"builds", "checked_files"} else {}
            snapshot["sha256"] = v.digest({k: value for k, value in snapshot.items() if k != "sha256"})
            verdict["snapshot_sha256"] = snapshot["sha256"]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "lacks complete"):
                v.completion_report(state, snapshot, verdict)

    def test_documented_excluded_removal_is_reviewable_not_frozen_bytes(self):
        self.write("Old.lean", "def old := 1\n")
        self.git("add", "Old.lean")
        self.git("commit", "--quiet", "-m", "archive")
        base = self.git("rev-parse", "HEAD")
        (self.root / "Old.lean").unlink()
        scope = {**self.scope, "excluded_files": {"Old.lean": "obsolete archive; deletion requires diff review"}}
        audit = v.audit_sources(self.root, base, scope)
        self.assertTrue(audit["passed"])
        self.assertEqual(audit["removed_files"], ["Old.lean"])

    def test_new_unclassified_module_requires_scope_review(self):
        self.write("Unknown.lean", "def newSource := 3\n")
        audit = v.audit_sources(self.root, self.base, self.scope)
        self.assertTrue(any(i["kind"] == "unclassified_source_module" for i in audit["issues"]))

    @patch.object(v.bump_jobs, "run")
    def test_final_failure_is_repairable_evidence_not_exception(self, run):
        def execute(root, args, **kw):
            if args[-1] == "--version":
                return self.run_tool(root, args, **kw)
            return subprocess.CompletedProcess(args, 1, "Example.lean:2:1: error: tactic failed\n", "")
        run.side_effect = execute
        snapshot = v.verify_final(self.root, base_sha=self.base, scope=self.scope, config_revision=3)
        self.assertFalse(snapshot["passed"])
        self.assertEqual(snapshot["config_revision"], 3)
        self.assertEqual(snapshot["diagnostics"][0]["message"], "tactic failed")

    def test_two_records_are_separate_unique_artifacts(self):
        state, snapshot, verdict = self.report_fixture()
        one = v.persist_report(self.root, state, snapshot, verdict)
        two = v.persist_report(self.root, state, snapshot, verdict)
        self.assertEqual(set(one["artifacts"]), {"project_changes", "workflow_changes", "final_report"})
        self.assertNotEqual(one["artifacts"]["project_changes"]["artifact_id"], two["artifacts"]["project_changes"]["artifact_id"])


if __name__ == "__main__":
    unittest.main()
