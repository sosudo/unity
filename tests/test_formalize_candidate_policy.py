"""Reject build-input changes from immutable candidate metadata before applying it."""

from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unity import formalize_files as files, formalize_runtime as runtime
from test_formalize_manifest_repair import project_baseline


class CandidateProjectPolicyTests(unittest.TestCase):
    def setUp(self):
        baseline = project_baseline()
        baseline["files"] = {"Original.lean": "old", "lakefile.lean": "config", "notes.txt": "text"}
        self.state = {"project_baseline": baseline, "formal_tasks": {}, "formal_candidates": {},
                      "formalization": {"contract": {"version": 3, "project_baseline": baseline}}}
        self.candidate = {"task_id": "target", "outputs": [], "obsolete_files": []}

    def test_build_configuration_and_nonlean_inputs_cannot_be_candidates(self):
        for name in ("lakefile.lean", "lakefile.toml", "lake-manifest.json", "lean-toolchain",
                     "notes.txt", "config/new.json"):
            with self.subTest(path=name):
                result = files.validate_candidate_files(self.state, self.candidate, changed_paths=[name])
                self.assertIn("project_configuration_change", [row["code"] for row in result])

    def test_original_input_deletion_is_forbidden_even_with_cleanup_request(self):
        result = files.validate_candidate_files(self.state, self.candidate,
                                                 changed_paths=["Original.lean"], deleted_paths=["Original.lean"])
        self.assertIn("original_project_file_deleted", [row["code"] for row in result])

    def test_proof_sources_reach_semantic_validation_without_policy_mutation(self):
        original = deepcopy(self.state)
        self.assertEqual(files.validate_candidate_files(self.state, self.candidate,
                         changed_paths=["Original.lean", "Support.lean"]), [])
        self.assertEqual(self.state, original)

    def test_immutable_config_diff_stops_before_apply_layout_or_build(self):
        candidate = {**self.candidate, "author": "Ada", "commit_sha": "c" * 40,
                     "base_main_sha": "b" * 40, "diff_sha256": hashlib.sha256(b"fixture patch").hexdigest()}
        paths = SimpleNamespace(project_root=Path("/unused"), forum=Path("/unused/forum"))
        with patch.object(runtime.formalize_state, "load_state", return_value=self.state), \
             patch.object(runtime, "require_source_matches"), \
             patch.object(runtime, "_candidate_preflight", return_value=[]), \
             patch.object(runtime.formalize_state, "candidate_is_current", return_value=True), \
             patch("unity.formalize_project.require_pinned_inputs"), \
             patch.object(runtime.worktree, "verify_candidate_commit", return_value="c" * 40), \
             patch.object(runtime, "_git", return_value=SimpleNamespace(returncode=0, stdout="fixture patch")) as git, \
             patch.object(files, "immutable_git_paths", return_value={"changed_paths": ["lakefile.lean"], "deleted_paths": []}), \
             patch.object(runtime.formalize_jobs, "run") as apply, \
             patch.object(runtime.formalize_contract, "workspace_layout") as layout, \
             patch.object(runtime.formalize_contract, "build_sources") as build:
            result = runtime._apply_formal_candidate(paths, candidate, {"task_id": "target"})
        self.assertFalse(result["ok"])
        self.assertEqual(git.call_count, 1)  # read-only diff, never git apply
        apply.assert_not_called()
        layout.assert_not_called()
        build.assert_not_called()
