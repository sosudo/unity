"""Compact routing/CAS controls. Pure fixtures, no compiler, services or models."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity import artifacts, bump_inventory as inventory
from unity import bump_state as state, bump_checker_v2 as checker, bump_planner as planner
from tests.test_bump_inventory import fixture_index
from tests.test_bump_diagnostics import fixture_diagnostics, fixture_imports


class MigrationStateV2Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.forum = Path(temporary.name)
        self.index = fixture_index()
        index_record = artifacts.store_text(self.forum / "artifacts", json.dumps(self.index), kind="bump_original_index")
        index_ref = {key: index_record[key] for key in ("artifact_id", "sha256")}
        self.source = {"kind": "supplied_sources", "sha256": "a" * 64,
                       "candidate_id": "source-" + "a" * 64,
                       "source_refs": [{"ref_id": ref, "path": ref.replace("source:", "source/"),
                                        "sha256": "b" * 64} for ref in
                           ["source:transition.json", "source:original-index.json", *[
                               "source:project/" + row["path"] for row in self.index["modules"].values()]]]}
        requirements, spec = checker.source_spec(self.index, self.source)
        self.mapping = checker.default_mapping(self.index)
        self.contract = checker.seal({"version": 4, "migration_policy": 2, "inspection_policy": 5,
            "migration_scope_policy": 1, "migration_occurrence_policy": 1,
            "original_index_sha256": self.index["index_sha256"],
            "artifact_root": str(self.forum / "artifacts"), "original_index_ref": index_ref,
            "task_bindings": checker.task_bindings(self.index), "targets": checker._targets(self.index),
            "bindings": {module: sorted([{"declaration": self.index["occurrences"][key]["display_name"],
                           "file": group["path"]} for key in group["occurrence_ids"]],
                           key=lambda row: (row["file"], row["declaration"]))
                         for module, group in self.index["modules"].items()},
            "obligation_ids": sorted(self.index["occurrences"]), "mapping": self.mapping,
            "mapping_sha256": checker.digest(self.mapping), "requirements": requirements,
            "spec": spec, "spec_sha256": checker.digest(spec), "environment": {},
            "solution_candidate": self.source["candidate_id"], "solution_sha256": self.source["sha256"]})
        self.plan = planner.plan_repairs(self.index, fixture_diagnostics(self.index, compiled=["A"]))
        state.initialize_source(self.forum, "f" * 64, "0" * 40, self.source, return_state=False)
        with patch.dict("os.environ", {"MAX_ATTEMPTS": "2"}):
            state.initialize_migration_plan(self.forum, self.index, self.plan,
                contract=self.contract, source=self.source, main_sha="0" * 40)

    def read(self):
        return state.load_state(self.forum)

    def claim(self, task="B"):
        row = state.register_strategy(self.forum, "Worker", "repair current compatibility", target=task)["strategy"]
        state.claim_strategy(self.forum, row["strategy_id"], "Worker")

    def refine(self, subtasks=None, **kwargs):
        current = self.read()
        return state.refine_migration(self.forum, "Worker", "B", expected_revision=current["revision"],
            subtasks=(current["formal_tasks"]["B"]["declaration_subtasks"] if subtasks is None else subtasks), **kwargs)

    def dependent_fixture(self, *, cycle=False):
        index = fixture_index()
        first = index["modules"]["B"]["occurrence_ids"][0]
        name = ["str", ["anonymous"], "other"]
        second = inventory.occurrence_id("B", name)
        index["occurrences"][second] = {**deepcopy(index["occurrences"][first]),
            "name_ast": name, "display_name": "other", "dependencies": [first] if cycle else []}
        index["occurrences"][first]["dependencies"] = [second]
        index["modules"]["B"]["occurrence_ids"] = sorted([first, second])
        index["index_sha256"] = inventory.digest({k: v for k, v in index.items() if k != "index_sha256"})
        inventory.validate_index(index)
        with patch(__name__ + ".fixture_index", return_value=index):
            self.setUp()
        return first, second

    def test_active_subset_and_immutable_coverage_are_distinct(self):
        current = self.read()
        self.assertEqual(set(current["formal_tasks"]), {"B", "C", "D"})
        self.assertEqual(set(current["formalization"]["contract"]["task_bindings"]), {"A", "B", "C", "D"})
        self.assertTrue(state.task_ready(current, "B"))
        self.assertFalse(state.task_ready(current, "C"))
        self.assertEqual(current["migration_plan"], self.plan)
        self.assertNotIn("original_reports", current["formalization"]["contract"])
        self.assertFalse(state.all_formal_tasks_complete(current))

    def test_empty_repair_plan_allows_final_check_not_acceptance(self):
        current = self.read()
        clean = planner.plan_repairs(self.index, fixture_diagnostics(self.index,
            compiled=list(self.index["modules"]), errors=()), self.plan)
        state.refresh_migration_plan(self.forum, self.index, clean,
            expected_revision=current["revision"], expected_main_sha="0" * 40)
        current = self.read()
        self.assertEqual(current["formal_tasks"], {})
        self.assertTrue(state.all_formal_tasks_complete(current))
        self.assertEqual(current["formalization"]["status"], "active")
        self.assertIsNone(current["formalization"]["review_snapshot"])
        self.assertEqual(len(current["formalization"]["contract"]["obligation_ids"]), 4)

    def test_requirement_universe_preserves_clean_original_source_coverage(self):
        from unity.bump_spec import normalize_requirements, normalize_spec
        current = self.read()
        universe = state._migration_task_universe(current)
        self.assertEqual(set(universe), set(self.index["modules"]))
        self.assertNotIn("A", current["formal_tasks"])
        normalized = normalize_requirements(self.contract["requirements"], universe,
            {row["ref_id"] for row in self.source["source_refs"]})
        self.assertEqual(normalized, self.contract["requirements"])
        self.assertEqual(normalize_spec(self.contract["spec"], source=self.source,
            requirements=normalized, tasks=universe), self.contract["spec"])

    def test_build_required_never_dispatches_a_model(self):
        current = self.read()
        current["formal_tasks"]["B"]["diagnostic_status"] = "build_required"
        self.assertFalse(state.task_ready(current, "B"))

    def test_unclassified_error_blocks_routing(self):
        current = self.read()
        current["migration_plan"]["unmapped_diagnostic_ids"] = ["diagnostic"]
        self.assertFalse(state.task_ready(current, "B"))
        self.assertFalse(state.all_formal_tasks_complete(current))

    def test_refresh_cas_preserves_bytes_on_stale_state(self):
        current = self.read()
        before = state.state_path(self.forum).read_bytes()
        next_plan = planner.plan_repairs(self.index, fixture_diagnostics(self.index, compiled=["A"]), self.plan)
        self.assertIs(state.refresh_migration_plan(self.forum, self.index, next_plan,
            expected_revision=current["revision"] - 1, expected_main_sha="0" * 40), False)
        self.assertEqual(state.state_path(self.forum).read_bytes(), before)

    def test_budget_survives_refinement_and_refresh(self):
        self.claim()
        state.begin_migration_attempt(self.forum, "B", "Worker")
        current = self.read()
        renamed = deepcopy(current["formal_tasks"]["B"]["declaration_subtasks"])
        renamed[0]["id"] = "renamed-subtask"
        self.refine(renamed)
        current = self.read()
        next_plan = planner.plan_repairs(self.index, fixture_diagnostics(self.index, compiled=["A"]), self.plan)
        state.refresh_migration_plan(self.forum, self.index, next_plan,
            expected_revision=current["revision"], expected_main_sha="0" * 40)
        current = self.read()
        self.assertEqual(current["formal_tasks"]["B"]["migration_attempts"], 1)
        self.assertEqual(current["migration_budgets"]["module:B"], {"attempts": 1, "limit": 2})
        state.begin_migration_attempt(self.forum, "B", "Worker")
        with self.assertRaisesRegex(ValueError, "budget"):
            state.begin_migration_attempt(self.forum, "B", "Worker")

    def test_budget_survives_compiled_retirement_and_later_regression(self):
        state.begin_migration_attempt(self.forum, "B", "Worker")
        current = self.read()
        clean = planner.plan_repairs(self.index, fixture_diagnostics(self.index,
            compiled=list(self.index["modules"]), errors=()), self.plan)
        state.refresh_migration_plan(self.forum, self.index, clean,
            expected_revision=current["revision"], expected_main_sha="0" * 40)
        current = self.read()
        self.assertNotIn("B", current["formal_tasks"])
        regression = planner.plan_repairs(self.index, fixture_diagnostics(self.index, compiled=["A"]), clean)
        state.refresh_migration_plan(self.forum, self.index, regression,
            expected_revision=current["revision"], expected_main_sha="0" * 40)
        self.assertEqual(self.read()["formal_tasks"]["B"]["migration_attempts"], 1)

    def test_refinement_cannot_drop_originals_or_errors(self):
        self.claim()
        for field in ("original_ids", "diagnostic_ids"):
            changed = deepcopy(self.read()["formal_tasks"]["B"]["declaration_subtasks"])
            for row in changed:
                row[field] = []
            before = state.state_path(self.forum).read_bytes()
            with self.assertRaisesRegex(ValueError, "partition"):
                self.refine(changed)
            self.assertEqual(state.state_path(self.forum).read_bytes(), before)

    def test_refinement_rejects_permission_or_status_fields(self):
        self.claim()
        changed = deepcopy(self.read()["formal_tasks"]["B"]["declaration_subtasks"])
        changed[0]["status"] = "complete"
        with self.assertRaisesRegex(ValueError, "shape"):
            self.refine(changed)

    def test_refinement_cannot_drop_module_prerequisite(self):
        self.claim()
        with self.assertRaisesRegex(ValueError, "dependencies"):
            self.refine(dependencies=[])

    def test_refinement_preserves_new_compiler_observed_prerequisite(self):
        diagnostic = fixture_diagnostics(self.index, compiled=["A", "D"], errors=("B",))
        diagnostic["target_imports"] = fixture_imports(self.index, edges={"B": ["A", "D"]})
        diagnostic["snapshot_sha256"] = inventory.digest({k: v for k, v in diagnostic.items() if k != "snapshot_sha256"})
        plan = planner.plan_repairs(self.index, diagnostic, self.plan)
        current = self.read()
        state.refresh_migration_plan(self.forum, self.index, plan,
            expected_revision=current["revision"], expected_main_sha="0" * 40)
        current = self.read()
        self.assertEqual(current["migration_plan"]["target_imports"], diagnostic["target_imports"])
        self.assertEqual(current["formal_tasks"]["B"]["dependencies"], ["A", "D"])
        self.assertTrue(state.task_ready(current, "B"))
        self.claim()
        with self.assertRaisesRegex(ValueError, "dependencies"):
            self.refine(dependencies=["A"])

    def test_refinement_cannot_drop_original_declaration_dependency(self):
        self.dependent_fixture()
        self.claim()
        changed = deepcopy(self.read()["formal_tasks"]["B"]["declaration_subtasks"])
        self.assertTrue(any(row["dependencies"] for row in changed))
        for row in changed:
            row["dependencies"] = []
        before = state.state_path(self.forum).read_bytes()
        with self.assertRaisesRegex(ValueError, "original declaration dependency"):
            self.refine(changed)
        self.assertEqual(state.state_path(self.forum).read_bytes(), before)

    def test_refinement_cannot_split_original_mutual_component(self):
        first, second = self.dependent_fixture(cycle=True)
        self.claim()
        current = self.read()["formal_tasks"]["B"]
        changed = [
            {"id": "first", "kind": "declaration", "original_ids": [first],
             "diagnostic_ids": current["diagnostic_ids"], "dependencies": ["second"]},
            {"id": "second", "kind": "declaration", "original_ids": [second],
             "diagnostic_ids": [], "dependencies": ["first"]}]
        before = state.state_path(self.forum).read_bytes()
        with self.assertRaisesRegex(ValueError, "dependency cycle"):
            self.refine(changed)
        self.assertEqual(state.state_path(self.forum).read_bytes(), before)

    def test_refinement_rechecks_original_artifact_bytes(self):
        self.claim()
        root = Path(self.contract["artifact_root"])
        (root / "blobs" / self.contract["original_index_ref"]["sha256"]).write_text("{}")
        with self.assertRaisesRegex(ValueError, "bytes changed"):
            self.refine()

    def test_mapping_proposal_blocks_until_controller_rejects_and_preserves_history(self):
        self.claim()
        self.refine(mapping_proposal=self.mapping)
        current = self.read()
        self.assertFalse(state.task_ready(current, "B"))
        self.assertEqual(current["formalization"]["contract"], self.contract)
        proposal_id = next(iter(current["migration_mapping_proposals"]))
        state.resolve_migration_mapping(self.forum, proposal_id, expected_revision=current["revision"],
                                       reason="identical mapping is unnecessary")
        current = self.read()
        self.assertEqual(current["migration_mapping_proposals"][proposal_id]["status"], "rejected")
        self.assertTrue(state.task_ready(current, "B"))

    def test_mapping_publication_cannot_change_original_source_or_scope(self):
        self.claim()
        self.refine(mapping_proposal=self.mapping)
        current = self.read()
        proposal_id = next(iter(current["migration_mapping_proposals"]))
        altered = checker.seal({**deepcopy(self.contract), "solution_sha256": "f" * 64})
        with self.assertRaisesRegex(ValueError, "cannot replace"):
            state.resolve_migration_mapping(self.forum, proposal_id,
                expected_revision=current["revision"], proposed_contract=altered)

    def test_mapping_publication_cannot_change_original_targets(self):
        self.claim()
        self.refine(mapping_proposal=self.mapping)
        current = self.read()
        proposal_id = next(iter(current["migration_mapping_proposals"]))
        altered = checker.seal({**deepcopy(self.contract), "targets": {}})
        with self.assertRaisesRegex(ValueError, "cannot replace"):
            state.resolve_migration_mapping(self.forum, proposal_id,
                expected_revision=current["revision"], proposed_contract=altered)

    def test_compiler_refresh_does_not_discharge_semantic_rejection(self):
        with state.transaction(self.forum) as current:
            current["formal_tasks"]["B"]["faithfulness"] = {"status": "changes_requested", "verdict_id": "v"}
        current = self.read()
        clean = planner.plan_repairs(self.index, fixture_diagnostics(self.index,
            compiled=list(self.index["modules"]), errors=()), self.plan)
        state.refresh_migration_plan(self.forum, self.index, clean,
            expected_revision=current["revision"], expected_main_sha="0" * 40)
        current = self.read()
        self.assertEqual(set(current["formal_tasks"]), {"B"})
        self.assertFalse(state.all_formal_tasks_complete(current))
        self.assertEqual(current["formal_tasks"]["B"]["faithfulness"]["verdict_id"], "v")

    def test_candidate_file_guard_is_complete_module_only(self):
        from unity import bump_files
        current = self.read()
        candidate = {"task_id": "B", "outputs": self.contract["bindings"]["B"]}
        self.assertEqual(bump_files.validate_candidate_files(current, candidate, changed_paths=["B.lean"]), [])
        for paths, deleted in ((["D.lean"], []), (["B.lean"], ["B.lean"]), (["lakefile.lean"], [])):
            self.assertTrue(bump_files.validate_candidate_files(current, candidate,
                changed_paths=paths, deleted_paths=deleted))

    def test_file_reservation_cannot_expand_or_share_execution_group(self):
        from unity import bump_files
        self.claim()
        for paths, sharing in ((["New.lean"], []), (["B.lean"], ["D"])):
            with self.assertRaisesRegex(ValueError, "cannot expand or share"):
                bump_files.reserve_files(self.forum, "Worker", "B", paths, share_with=sharing)

    def test_clean_group_reopens_from_explicit_machine_failure_not_build_status(self):
        with state.transaction(self.forum) as current:
            state._reopen_migration_groups(current, ["A"], evidence_id="snapshot-failure", kind="machine")
        current = self.read()
        self.assertEqual(current["formal_tasks"]["A"]["machine_repair"]["status"], "required")
        self.assertTrue(state.task_ready(current, "A"))
        clean = planner.plan_repairs(self.index, fixture_diagnostics(self.index,
            compiled=list(self.index["modules"]), errors=()), self.plan)
        state.refresh_migration_plan(self.forum, self.index, clean,
            expected_revision=current["revision"], expected_main_sha="0" * 40)
        current = self.read()
        self.assertEqual(set(current["formal_tasks"]), {"A"})
        self.assertFalse(state.all_formal_tasks_complete(current))
        self.assertEqual(current["formal_tasks"]["A"]["machine_repair"]["snapshot_id"], "snapshot-failure")

    def test_global_machine_failure_blocks_models_and_completion(self):
        current = self.read()
        current["migration_global_blocker"] = {"snapshot_id": "snapshot-failure"}
        self.assertFalse(state.task_ready(current, "B"))
        current["formal_tasks"] = {}
        self.assertFalse(state.all_formal_tasks_complete(current))
