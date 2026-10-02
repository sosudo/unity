"""Migration adaptations of ported Formalize state; no models or Lean."""
import os
from copy import deepcopy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unity import bump_state as state, bump_contract as contract, bump_files, bump_project


class MigrationStateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.forum = Path(temp.name)
        self.modules = {"A": {"path": "A.lean", "imports": []},
                        "B": {"path": "B.lean", "imports": ["A"]},
                        "Empty": {"path": "Empty.lean", "imports": []}}
        bindings = {key: ([] if key == "Empty" else [{"file": key + ".lean", "declaration": key + ".value"}])
                    for key in self.modules}
        reports = {key: {"declarations": {row["declaration"]: {} for row in rows},
                   "meanings": {row["declaration"]: {"meaning": {"name": ["str", ["anonymous"], row["declaration"]]}}
                                for row in rows}} for key, rows in bindings.items()}
        self.contract = {"version": 3, "migration_policy": 1, "migration_occurrence_policy": 1,
                         "solution_candidate": "original",
                         "solution_sha256": "b" * 64, "spec_sha256": "c" * 64,
                         "requirements": [], "environment": {}, "bindings": bindings,
                         "project_baseline": {"version": 5, "occurrence_policy": 1, "original_reports": reports},
                         "targets": {bump_project.migration_occurrence_id(key,
                             reports[key]["meanings"][row["declaration"]]["meaning"]["name"]): {
                                 "fingerprint": row["declaration"], "module": key,
                                 "declaration": row["declaration"],
                                 "native_name": reports[key]["meanings"][row["declaration"]]["meaning"]["name"]}
                                     for key, rows in bindings.items() for row in rows}}
        self.contract["sha256"] = state._contract_digest(self.contract)
        with state.transaction(self.forum) as current:
            current.update(phase="formalizing", run_id="bump-fixture",
                           input_source={"kind": "supplied_sources", "candidate_id": "original", "sha256": "b"*64})
            current["formalization"].update(contract=self.contract, revision=1, main_sha="a"*40,
                                            solution_candidate="original", solution_sha256="b"*64)
            current["formal_tasks"] = {key: {"task_id": key, "revision": 1, "status": "pending",
                                             "outputs": [], "dependencies": entry["imports"]}
                                       for key, entry in self.modules.items()}
        with patch.dict(os.environ, {"MAX_ATTEMPTS": "2"}):
            state.seed_migration_tasks(self.forum, self.modules, {key: {} for key in self.modules})

    def receipt(self, key):
        fingerprints = contract.output_fingerprints(self.contract, key)
        return {"status": "passed", "policy_sha256": contract.policy_hash(),
                "contract_sha256": self.contract["sha256"],
                "module_receipt": {"module": key, "passed": True, "policy_sha256": contract.policy_hash(),
                                   "occurrence_policy": 1, "verified_targets": fingerprints},
                "verified_targets": fingerprints}

    def test_original_interface_is_not_target_completion(self):
        current = state.load_state(self.forum)
        self.assertTrue(state.task_ready(current, "A"))
        self.assertFalse(state.interface_available(current, "A"))
        self.assertFalse(state.task_ready(current, "B"))

    def test_stale_controller_check_does_not_overwrite_concurrent_submission(self):
        revision = state.load_state(self.forum)["revision"]
        with state.transaction(self.forum) as current:
            current["formal_tasks"]["A"]["status"] = "submitted"
        before = state.state_path(self.forum).read_bytes()
        self.assertIs(state.record_migration_diagnostic(
            self.forum, "A", {"passed": False}, expected_revision=revision), False)
        self.assertIs(state.record_migration_module_check(
            self.forum, "A", self.receipt("A"), main_sha="a"*40,
            expected_revision=revision), False)
        self.assertEqual(state.state_path(self.forum).read_bytes(), before)

    def test_import_only_module_has_explicit_critic_obligation_without_fake_declaration(self):
        current = state.load_state(self.forum)
        current["formalization"].update(
            requirements=[{"id": "req-empty", "anchor_ids": ["original-empty"], "tasks": ["Empty"]}],
            spec={"arguments": [], "prerequisites": []}, review_snapshot={"declarations": {}})
        review = {"requirements": [{"requirement_id": "req-empty", "status": "pass",
                    "rationale": "Original imports and target imports checked.",
                    "argument_rationale": "No declarations in either module.",
                    "checked_anchor_ids": ["original-empty"], "checked_prerequisite_ids": [],
                    "declarations": []}], "scope_rationale": "Exact original module inventory.",
                  "repair_reviews": []}
        state._validate_semantic_review(current, review, approved=True, author="Critic")
        review["requirements"][0]["checked_anchor_ids"] = []
        with self.assertRaisesRegex(ValueError, "every requirement source anchor"):
            state._validate_semantic_review(current, review, approved=True, author="Critic")

    def test_controller_build_unlocks_dependency_without_model_attempt(self):
        state.record_migration_module_check(self.forum, "A", self.receipt("A"), main_sha="a"*40)
        current = state.load_state(self.forum)
        self.assertTrue(state.task_ready(current, "B"))
        accepted = current["formal_tasks"]["A"]["accepted_candidate"]
        self.assertEqual(current["formal_candidates"][accepted]["origin"], "controller_build")
        self.assertEqual(current["formal_tasks"]["A"]["migration_attempts"], 0)

    def test_empty_module_requires_real_receipt(self):
        invalid = self.receipt("Empty")
        invalid.pop("module_receipt")
        with self.assertRaises(ValueError):
            state.record_migration_module_check(self.forum, "Empty", invalid, main_sha="a"*40)
        state.record_migration_module_check(self.forum, "Empty", self.receipt("Empty"), main_sha="a"*40)
        self.assertEqual(state.load_state(self.forum)["formal_tasks"]["Empty"]["status"], "complete")

    def test_stale_wrong_module_or_policy_evidence_rejected(self):
        for field, value in (("module", "B"), ("passed", False), ("policy_sha256", "old")):
            invalid = self.receipt("A")
            invalid["module_receipt"][field] = value
            with self.assertRaises(ValueError):
                state.record_migration_module_check(self.forum, "A", invalid, main_sha="a"*40)
        with self.assertRaises(ValueError):
            state.record_migration_module_check(self.forum, "A", self.receipt("A"), main_sha="d"*40)
        self.assertFalse(state.load_state(self.forum)["formal_candidates"])

    def test_native_completion_receipt_binds_sealed_scope(self):
        bound = deepcopy(self.contract)
        bound["migration_scope_policy"] = 1
        bound["project_baseline"].update(
            policy="migration-v1", version=5, scope_policy=1,
            migration={"scope": {"sha256": "e" * 64}})
        bound["project_baseline"]["original_reports"]["A"]["evidence_sha256"] = "f" * 64
        check = self.receipt("A")
        check["source_identity"] = {"source_sha256": "1" * 64}
        check["compiled_receipt"] = {"artifact": "2" * 64}
        check["module_receipt"].update(
            scope_sha256="e" * 64, source_sha256="1" * 64,
            original_evidence="f" * 64, current_evidence="3" * 64,
            comparison_sha256="4" * 64, compiled_receipt=check["compiled_receipt"])
        task = state.load_state(self.forum)["formal_tasks"]["A"]
        state._require_migration_receipt(task, bound, check)
        for change in ("receipt_scope", "old_baseline", "missing_scope_policy", "missing_contract_policy"):
            altered, evidence = deepcopy(bound), deepcopy(check)
            if change == "receipt_scope":
                evidence["module_receipt"]["scope_sha256"] = "0" * 64
            elif change == "old_baseline":
                altered["project_baseline"]["version"] = 3
            elif change == "missing_scope_policy":
                altered["project_baseline"].pop("scope_policy")
            else:
                altered.pop("migration_scope_policy")
            with self.subTest(change=change), self.assertRaises(ValueError):
                state._require_migration_receipt(task, altered, evidence)

    def test_budget_persists_but_last_attempt_can_still_submit(self):
        state.begin_migration_attempt(self.forum, "A", "Luna1")
        state.begin_migration_attempt(self.forum, "A", "Luna2")
        self.assertTrue(state.task_ready(state.load_state(self.forum), "A"))
        with self.assertRaisesRegex(ValueError, "budget"):
            state.begin_migration_attempt(self.forum, "A", "Luna3")
        self.assertEqual(state.load_state(self.forum)["formal_tasks"]["A"]["migration_attempts"], 2)

    def test_downstream_invalidation_preserves_bindings_and_budget(self):
        with state.transaction(self.forum) as current:
            current["formal_tasks"]["B"].update(status="complete", accepted_candidate="prior", migration_attempts=2)
            state._invalidate_migration_dependents(current, "A")
        row = state.load_state(self.forum)["formal_tasks"]["B"]
        self.assertEqual((row["status"], row["revision"], row["migration_attempts"]), ("pending", 2, 2))
        self.assertEqual(row["outputs"], self.contract["bindings"]["B"])

    def test_replan_representation_and_reseed_are_blocked(self):
        with self.assertRaisesRegex(ValueError, "fixed"):
            state.refine_chunks(self.forum, "Luna1", state.load_state(self.forum)["revision"], {"upserts": [{"id": "A"}]})
        with self.assertRaisesRegex(ValueError, "complete"):
            state.submission_blockers(state.load_state(self.forum), "A", "representation")
        with self.assertRaisesRegex(ValueError, "already"):
            state.seed_migration_tasks(self.forum, self.modules, {key: {} for key in self.modules})

    def test_assigned_module_is_exact_write_boundary(self):
        current = state.load_state(self.forum)
        candidate = {"task_id": "A", "outputs": self.contract["bindings"]["A"], "changed_paths": ["B.lean"], "deleted_paths": []}
        self.assertIn("migration_file_boundary", {x["code"] for x in bump_files.validate_candidate_files(current, candidate)})
        candidate.update(changed_paths=["A.lean"], deleted_paths=["A.lean"])
        self.assertIn("migration_file_boundary", {x["code"] for x in bump_files.validate_candidate_files(current, candidate)})
        candidate.update(deleted_paths=[])
        self.assertEqual(bump_files.validate_candidate_files(current, candidate), [])
