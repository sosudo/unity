"""Frozen build/all migration boundaries; no compilers, providers or services."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity import bump_bootstrap, bump_migration, bump_report, bump_scope
from unity.bump_input import bump_paths
from unity.config import Paths
import test_bump_command as existing
from test_bump_migration_checks import baseline as migration_baseline


class ProjectScopeCommandTests(unittest.IsolatedAsyncioTestCase):
    setUp = existing.BumpCommandTests.setUp
    invoke = existing.BumpCommandTests.invoke

    async def test_fresh_omitted_scope_selects_build(self):
        result = await self.invoke("v4.34.1")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.prepare.call_args.kwargs["project_scope"], "build")

    async def test_fresh_all_is_forwarded(self):
        result = await self.invoke("v4.34.1", "--project-scope", "all")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.prepare.call_args.kwargs["project_scope"], "all")

    async def test_continue_omitted_scope_is_not_reinterpreted(self):
        result = await self.invoke("--continue")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIsNone(self.resume.call_args.kwargs["project_scope"])
        self.prepare.assert_not_called()

    async def test_continue_explicit_scope_is_checked_by_saved_adapter(self):
        result = await self.invoke("--continue", "--project-scope", "build")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.resume.call_args.kwargs["project_scope"], "build")

    async def test_scope_mismatch_stops_before_jobs_and_recovery(self):
        self.resume.side_effect = ValueError("--continue cannot change the saved scope")
        result = await self.invoke("--continue", "--project-scope", "all")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("cannot change the saved scope", result.output)
        self.terminate.assert_not_called()
        self.recover_interrupted_formal_merges.assert_not_called()
        self._run_migration_loop.assert_not_awaited()

    async def test_unknown_and_removed_scope_policies_reject_before_loading_project(self):
        for mode in ("executables", "libraries", "changes"):
            with self.subTest(mode=mode):
                result = await self.invoke("v4.34.1", "--project-scope", mode)
                self.assertNotEqual(result.exit_code, 0)
        self.load_paths.assert_not_called()
        self.prepare.assert_not_called()

    async def test_english_targets_are_not_migration_scope(self):
        result = await self.invoke("v4.34.1", "--targets", "target")
        self.assertNotEqual(result.exit_code, 0)
        self.load_paths.assert_not_called()

    async def test_exact_pin_version_and_architect_reach_resume_validation(self):
        result = await self.invoke("v4.34.1", "--continue", "--dependency", "mathlib=" + "a" * 40,
                                   "--architect", "off")
        self.assertEqual(result.exit_code, 0, result.output)
        self.resume.assert_called_once_with(self.source, "v4.34.1", {"mathlib": "a" * 40},
                                            project_scope=None, architect="off")


class SavedMigrationBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.paths = bump_paths(Paths.from_unity_dir(self.root / ".unity"))
        self.paths.unity.mkdir()
        self.run_id = "bump-123456abcdef"
        self.target = self.root / ".unity/bump" / self.run_id / "target"
        self.target_paths = bump_paths(Paths.from_unity_dir(self.target / ".unity"))
        self.target_paths.unity.mkdir(parents=True)
        self.baseline = migration_baseline(self.root)
        self.baseline["project_root"] = str(self.target)
        self.baseline["migration"].update(source_root=str(self.root), target_root=str(self.target))
        self.baseline = bump_migration.seal(self.baseline)
        self.current = {"project_baseline": self.baseline, "formalization": {"main_sha": "b" * 40,
            "contract": {"migration_policy": 1, "project_baseline": self.baseline}}}
        hashes = {}
        for name in ("UNITY.md", "agents.yaml"):
            payload = ("immutable " + name + "\n").encode()
            (self.paths.unity / name).write_bytes(payload)
            (self.target_paths.unity / name).write_bytes(payload)
            hashes[name] = hashlib.sha256(payload).hexdigest()
        self.pointer = {"version": 1, "status": "ready", "run_id": self.run_id,
            "project_root": str(self.root), "target_path": str(self.target),
            "runtime_hashes": hashes, "baseline_sha256": self.baseline["sha256"]}
        self.saved = {**self.baseline["migration"], "target_version": "leanprover/lean4:v4.34.1",
                      "dependency_pins": {"mathlib": "a" * 40}, "architect_mode": "off"}
        self.write_receipts()
        for module, name, value in (
                (bump_bootstrap.bump_state, "load_state", self.current),
                (bump_bootstrap.bump_worktree, "main_commit", "b" * 40),
                (bump_bootstrap.bump_project, "require_pinned_inputs", None),
                (bump_bootstrap.bump_project, "_require_clean", None),
                (bump_bootstrap, "require_source_matches", None)):
            patcher = patch.object(module, name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def write_receipts(self):
        bump_bootstrap._json(bump_bootstrap.active_path(self.paths), self.pointer)
        bump_bootstrap._json(self.target_paths.unity / "bump-origin.json",
            {key: self.pointer[key] for key in ("project_root", "run_id", "target_path")})
        bump_bootstrap._json(self.target_paths.unity / "bump/migration.json", self.saved)

    def test_omitted_and_matching_saved_scope_resume_without_recapture(self):
        for mode in (None, "build"):
            resumed = bump_bootstrap.resume(self.paths, project_scope=mode)
            self.assertEqual(resumed.project_root, self.target)
        self.assertEqual(self.require_pinned_inputs.call_count, 2)

    def test_scope_cannot_expand_before_input_jobs(self):
        with self.assertRaisesRegex(ValueError, "saved scope"):
            bump_bootstrap.resume(self.paths, project_scope="all")
        self.require_pinned_inputs.assert_not_called()

    def test_version_and_dependency_pins_cannot_change(self):
        for kwargs in ({"version": "v4.34.0"}, {"dependency_pins": {"mathlib": "c" * 40}}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "cannot change"):
                bump_bootstrap.resume(self.paths, **kwargs)
        self.require_pinned_inputs.assert_not_called()

    def test_architect_policy_cannot_change_on_resume(self):
        with self.assertRaisesRegex(ValueError, "Architect policy"):
            bump_bootstrap.resume(self.paths, architect="auto")
        self.require_pinned_inputs.assert_not_called()

    def test_original_contract_baseline_cannot_be_swapped(self):
        changed = deepcopy(self.baseline)
        changed["target_scope"] = "weakened"
        self.current["formalization"]["contract"]["project_baseline"] = bump_migration.seal(changed)
        with self.assertRaisesRegex(ValueError, "original migration baseline"):
            bump_bootstrap.resume(self.paths)
        self.require_pinned_inputs.assert_not_called()

    def test_pointer_baseline_identity_cannot_be_swapped(self):
        self.pointer["baseline_sha256"] = "f" * 64
        self.write_receipts()
        with self.assertRaisesRegex(ValueError, "does not match the workspace"):
            bump_bootstrap.resume(self.paths)
        self.require_pinned_inputs.assert_not_called()

    def test_target_origin_cannot_redirect_to_another_attempt(self):
        bump_bootstrap._json(self.target_paths.unity / "bump-origin.json",
            {"project_root": str(self.root), "target_path": str(self.target), "run_id": "bump-ffffffffffff"})
        with self.assertRaisesRegex(ValueError, "target origin"):
            bump_bootstrap.resume(self.paths)
        self.require_pinned_inputs.assert_not_called()

    def test_runtime_configuration_must_remain_identical(self):
        (self.target_paths.unity / "agents.yaml").write_text("changed")
        with self.assertRaisesRegex(ValueError, "runtime configuration changed"):
            bump_bootstrap.resume(self.paths)
        self.require_pinned_inputs.assert_not_called()

    def test_runtime_head_must_match_integrated_main(self):
        self.main_commit.return_value = "f" * 40
        with self.assertRaisesRegex(ValueError, "target HEAD changed"):
            bump_bootstrap.resume(self.paths)
        self.require_source_matches.assert_not_called()

    def test_malformed_workspace_id_is_rejected_before_target_access(self):
        self.pointer["run_id"] = "../../other"
        self.write_receipts()
        with self.assertRaisesRegex(ValueError, "identifier is invalid"):
            bump_bootstrap.resume(self.paths)
        self.require_pinned_inputs.assert_not_called()


class ProjectScopeReportTests(unittest.TestCase):
    def setUp(self):
        self.baseline = migration_baseline()
        self.index = self.baseline["migration"]["original_index"]
        correspondences = {key: {"module": row["module"], "declaration": row["display_name"],
                                 "name_ast": row["name_ast"]} for key, row in self.index["occurrences"].items()}
        self.current = {"phase": "formalizing", "project_baseline": self.baseline, "formal_tasks": {},
            "formalization": {"contract": {"project_baseline": self.baseline,
                                           "requirements": [{"id": "requirement-" + key,
                                               "anchor_ids": ["anchor-" + key]}
                                               for key in self.index["occurrences"]],
                                           "migration_correspondences": correspondences}}}
        self.snapshot = {"passed": True, "compiled_receipt": {"fixture": True},
            "project_verification": bump_migration.coverage(self.baseline),
            "migration_review": {"policy": bump_migration.POLICY, "native_complete": True,
                "original_occurrences": sorted(self.index["occurrences"]), "verified_occurrences": sorted(self.index["occurrences"]),
                "occurrence_declarations": {key: row["declaration"] for key, row in correspondences.items()},
                "scope_sha256": self.baseline["migration"]["scope"]["sha256"],
                "correspondences_sha256": bump_migration.digest(correspondences), "helper_modules": [],
                "native_reports": [{"module": "Fixture", "side": side} for side in ("original", "target")]}}

    def test_incomplete_report_distinguishes_original_obligations_and_excluded_bytes(self):
        coverage = bump_report.completion_report(self.current, accepted=False)["project_verification"]
        self.assertEqual(coverage["mode"], "build")
        self.assertEqual(coverage["verification_modules"], {"Fixture.lean": "Fixture"})
        self.assertEqual(coverage["excluded_files"], {"Notes.lean": "b" * 64})
        self.assertEqual(coverage["original_occurrences"], sorted(self.index["occurrences"]))
        self.assertIsNone(coverage["current_snapshot_coverage"])
        self.assertIn("not claimed compiled or migrated", coverage["qualification"])

    def test_accepted_coverage_requires_exact_current_original_universe(self):
        for key, value in (("scope_sha256", "bad"), ("correspondences_sha256", "bad"),
                           ("original_occurrences", []), ("verified_occurrences", [])):
            snapshot = deepcopy(self.snapshot)
            snapshot["migration_review"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                bump_report._project_verification(self.current, snapshot, accepted=True)

    def test_provisional_compiler_receipts_do_not_count_as_native_coverage(self):
        snapshot = deepcopy(self.snapshot)
        snapshot["migration_review"]["native_complete"] = False
        with self.assertRaises(ValueError):
            bump_report._project_verification(self.current, snapshot, accepted=True)

    def test_missing_compiled_binding_is_rejected(self):
        snapshot = deepcopy(self.snapshot)
        snapshot["compiled_receipt"] = None
        with self.assertRaises(ValueError):
            bump_report._project_verification(self.current, snapshot, accepted=True)

    def test_excluded_files_cannot_be_added_to_selected_snapshot_coverage(self):
        snapshot = deepcopy(self.snapshot)
        snapshot["project_verification"]["verification_modules"]["Notes.lean"] = "Notes"
        with self.assertRaises(ValueError):
            bump_report._project_verification(self.current, snapshot, accepted=True)

    def test_matching_native_scope_still_does_not_replace_final_critic_gate(self):
        coverage = bump_report._project_verification(self.current, self.snapshot, accepted=True)
        self.assertEqual(coverage["current_snapshot_coverage"], self.snapshot["project_verification"])
        with self.assertRaisesRegex(ValueError, "has not been accepted"):
            bump_report.completion_report(self.current, accepted=True)

    def test_resealed_outer_baseline_cannot_downgrade_scope(self):
        changed = deepcopy(self.baseline)
        changed["project_scope"] = "all"
        with self.assertRaises(ValueError):
            bump_scope.mode(bump_migration.seal(changed))
