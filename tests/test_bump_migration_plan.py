"""Offline plan-writer regressions for the sealed migration baseline schema."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unity import bump_inventory as inventory, bump_migration as migration
from unity import bump_runtime as runtime
from test_bump_migration_checks import declaration, lean_name, report


def migration_baseline():
    """A valid compact inventory, not native proof or acceptance evidence."""
    files = {"Fixture.lean": "a" * 64, "Empty.lean": "b" * 64,
             "Notes.lean": "c" * 64}
    selected = {"Fixture": "Fixture.lean", "Empty": "Empty.lean"}
    scope = migration.seal({
        "version": 1, "mode": "build", "selected_modules": selected,
        "excluded_files": {"Notes.lean": files["Notes.lean"]},
        "default_modules": {path: module for module, path in selected.items()},
        "build_dir": ".lake/build",
    })
    rows = [declaration("Example", "visible", kind="theorem"),
            declaration("_private", "Fixture", 0, "Example", "hidden"),
            declaration("Example", "visible", "_match", 1),
            declaration("Example", "a.b", display="Example.«a.b»"),
            declaration("Example", "a", "b")]
    for row in rows:
        row.pop("meaning")
        row.pop("axioms")
    rows[1].update(is_internal=True, direct_sorry=True, dependencies=[
        {"module": "Fixture", "name_ast": rows[0]["name_ast"]},
        {"module": "Init", "name_ast": lean_name("Nat")},
    ])
    rows[2].update(is_internal=True, dependencies=[
        {"module": "Fixture", "name_ast": rows[1]["name_ast"]},
    ])
    original_environment = {
        "config": {"lean-toolchain": "d" * 64},
        "dependencies": {"mathlib": {"rev": "e" * 40}},
    }
    target_environment = {
        "config": {"lean-toolchain": "f" * 64},
        "dependencies": {"mathlib": {"rev": "1" * 40}},
    }
    index = inventory.assemble_index(
        {"Fixture": report(*rows, mode="index"),
         "Empty": {**report(mode="index"), "module": "Empty"}},
        {"Fixture": {"path": "Fixture.lean", "imports": [],
                     "line_lengths": [20, 0, 25]},
         "Empty": {"path": "Empty.lean", "imports": ["Fixture"],
                   "line_lengths": [14, 0]}},
        files, scope_sha256=scope["sha256"], environment=original_environment,
        native_refs={"Fixture": {"artifact_id": "artifact-index-fixture",
                                 "sha256": "2" * 64}},
    )
    info = {
        "version": 1, "policy": "migration-v1", "run_id": "bump-123456abcdef",
        "source_root": "/fixture/source", "original_root": "/fixture/original",
        "target_root": "/fixture/target", "original_commit": "3" * 40,
        "target_version": "v4.34.1", "dependency_pins": {"mathlib": "1" * 40},
        "original_files": files, "original_index": index, "scope": scope,
        "selected_modules": selected, "excluded_files": scope["excluded_files"],
        "index_ref": {"artifact_id": "artifact-original-index", "sha256": "4" * 64},
        "original_environment": original_environment,
        "target_environment": target_environment,
        "manifest": {"withheld": "not scheduling metadata"},
        "architect": {"withheld": "not scheduling metadata"},
    }
    return migration.seal({
        "version": 3, "policy": "migration-v1", "project_root": "/fixture/target",
        "project_scope": "build", "branch": "eval/bump-fixture", "head": "5" * 40,
        "files": files, "tracked_files": sorted(files), "environment": target_environment,
        "layout": {"build_dir": ".lake/build", "modules": {
            "Fixture.lean": "Fixture", "Empty.lean": "Empty", "Notes.lean": "Notes"}},
        "target_scope": "Preserve every selected original declaration and excluded source.",
        "scope": {"mode": "migration", "bound": True,
                  "existing_targets": sorted(index["occurrences"])},
        "migration": info, "manifest": {"withheld": "not scheduling metadata"},
        "architect": {"withheld": "not scheduling metadata"},
    })


class MigrationPlanWriterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="unity-migration-plan-")
        self.addCleanup(temporary.cleanup)
        self.paths = SimpleNamespace(forum=Path(temporary.name) / "forum")
        self.baseline = migration_baseline()
        self.assertEqual(migration.baseline_errors(self.baseline), [])
        self.state = {"formalization": {}, "project_baseline": self.baseline}
        self.source = {
            "candidate_id": "source-" + "6" * 64, "sha256": "6" * 64,
            "source_refs": [{"ref_id": "source:project/Fixture.lean",
                             "path": ".unity/source/project/Fixture.lean",
                             "sha256": "a" * 64}],
        }

    def write_plan(self):
        with patch.object(runtime.bump_state, "load_state", return_value=self.state):
            path = runtime.write_formalization_plan(self.paths, self.source)
        self.assertEqual(path, self.paths.forum / "formalization-plan.json")
        return json.loads(path.read_text())

    def test_migration_without_formalize_declarations_table_writes_plan(self):
        self.assertNotIn("declarations", self.baseline)
        plan = self.write_plan()["project_baseline"]
        self.assertEqual(plan["policy"], "migration-v1")
        self.assertEqual(plan["sha256"], self.baseline["sha256"])
        self.assertNotIn("existing_incomplete_declarations", plan)
        self.assertEqual(plan["protected_modules"], ["Empty", "Fixture", "Notes"])

    def test_complete_index_retains_structural_names_edges_and_empty_modules(self):
        original = self.baseline["migration"]["original_index"]
        planned = self.write_plan()["project_baseline"]["migration"]["original_index"]
        self.assertEqual(planned, original)
        inventory.validate_index(planned)
        self.assertEqual(planned["modules"]["Empty"]["occurrence_ids"], [])
        self.assertEqual(planned["modules"]["Empty"]["line_lengths"], [14, 0])
        visible = inventory.occurrence_id("Fixture", lean_name("Example", "visible"))
        private = inventory.occurrence_id(
            "Fixture", lean_name("_private", "Fixture", 0, "Example", "hidden"))
        generated = inventory.occurrence_id(
            "Fixture", lean_name("Example", "visible", "_match", 1))
        quoted = inventory.occurrence_id("Fixture", lean_name("Example", "a.b"))
        separate = inventory.occurrence_id("Fixture", lean_name("Example", "a", "b"))
        self.assertEqual(set(planned["occurrences"]),
                         {visible, private, generated, quoted, separate})
        self.assertNotEqual(quoted, separate)
        self.assertTrue(planned["occurrences"][private]["direct_sorry"])
        self.assertTrue(planned["occurrences"][generated]["is_internal"])
        self.assertEqual(planned["occurrences"][private]["dependencies"], [visible])
        self.assertEqual(planned["occurrences"][generated]["dependencies"], [private])
        self.assertEqual(planned["occurrences"][private]["external_dependencies"],
                         [{"module": "Init", "name_ast": lean_name("Nat")}])

    def test_plan_whitelists_metadata_without_dropping_index_environment(self):
        planned = self.write_plan()["project_baseline"]
        self.assertEqual(set(planned), {
            "sha256", "version", "policy", "project_scope", "branch", "head",
            "target_scope", "scope", "migration", "protected_modules",
        })
        self.assertEqual(set(planned["migration"]), {
            "version", "policy", "run_id", "source_root", "original_root", "target_root",
            "original_commit", "target_version", "dependency_pins", "scope",
            "selected_modules", "excluded_files", "index_ref", "original_index",
        })
        for field in ("original_environment", "target_environment", "original_files",
                      "manifest", "architect"):
            self.assertNotIn(field, planned["migration"])
        self.assertNotIn("environment", planned)
        # The entire sealed scheduling index, including its binding metadata, remains intact.
        self.assertEqual(planned["migration"]["original_index"]["environment"],
                         self.baseline["migration"]["original_environment"])
        for key, value in planned["migration"].items():
            self.assertEqual(value, self.baseline["migration"][key])

    def test_writer_leaves_state_source_and_nested_seals_unchanged(self):
        original_state, original_source = deepcopy(self.state), deepcopy(self.source)
        plan = self.write_plan()
        self.assertEqual(self.state, original_state)
        self.assertEqual(self.source, original_source)
        self.assertEqual(migration.baseline_errors(self.baseline), [])
        self.assertEqual(self.baseline["sha256"], original_state["project_baseline"]["sha256"])
        self.assertEqual(self.baseline["migration"]["scope"]["sha256"],
                         original_state["project_baseline"]["migration"]["scope"]["sha256"])
        self.assertEqual(plan["project_baseline"]["migration"]["original_index"]["index_sha256"],
                         original_state["project_baseline"]["migration"]["original_index"]["index_sha256"])

    def test_replan_and_source_bindings_remain_available(self):
        self.state.update(
            replan={"request": {"request_id": "replan-one", "task_ids": ["pending-one"]},
                    "previous_tasks": {"completed-one": {"status": "complete",
                                                          "accepted_candidate": "merged-one"}},
                    "previous_formalization": {
                        "spec": {"version": 1, "anchors": []},
                        "requirements": [{"id": "original-obligation"}],
                        "contract": {"withheld": "not scheduling metadata"},
                    }},
            source_issues={"issue-one": {"issue_id": "issue-one", "status": "open",
                                        "source_sha256": self.source["sha256"],
                                        "unrelated": "not scheduling metadata"}},
            source_repairs={"repair-one": {"status": "accepted"}},
        )
        original = deepcopy(self.state)
        plan = self.write_plan()
        self.assertEqual(plan["solution_candidate"], self.source["candidate_id"])
        self.assertEqual(plan["solution_sha256"], self.source["sha256"])
        self.assertEqual(plan["source_refs"], self.source["source_refs"])
        self.assertEqual(plan["replan"]["request"], self.state["replan"]["request"])
        self.assertEqual(plan["replan"]["previous_tasks"], self.state["replan"]["previous_tasks"])
        self.assertEqual(plan["replan"]["previous_formalization"], {
            key: self.state["replan"]["previous_formalization"][key]
            for key in ("spec", "requirements")})
        self.assertEqual(plan["source_issues"]["issue-one"]["source_sha256"], self.source["sha256"])
        self.assertNotIn("unrelated", plan["source_issues"]["issue-one"])
        self.assertEqual(plan["source_repairs"], self.state["source_repairs"])
        self.assertEqual(self.state, original)

    def test_contract_baseline_takes_precedence_and_optional_metadata_is_optional(self):
        baseline = deepcopy(self.baseline)
        for field in ("target_version", "dependency_pins", "index_ref"):
            baseline["migration"].pop(field)
        baseline = migration.seal(baseline)
        self.assertEqual(migration.baseline_errors(baseline), [])
        self.state.update(project_baseline={"policy": "unrelated-sentinel"},
                          formalization={"contract": {"project_baseline": baseline}})
        plan = self.write_plan()["project_baseline"]
        self.assertEqual(plan["sha256"], baseline["sha256"])
        self.assertEqual(plan["migration"]["original_index"], baseline["migration"]["original_index"])
        for field in ("target_version", "dependency_pins", "index_ref"):
            self.assertNotIn(field, plan["migration"])

    def test_legacy_nonmigration_plan_still_filters_editable_declarations(self):
        baseline = {
            "scope": {"existing_targets": [], "bound": False},
            "project_axioms": ["readonly-hole"], "project_sorries": ["target"],
            "declarations": {
                "readonly-hole": {"module": "Readonly"},
                "target": {"module": "Fixture", "target_kind": "theorem",
                           "type": ["sort", ["zero"]], "level_params": ["u"],
                           "value": "proof-body-not-needed-for-planning"}},
            "layout": {"modules": {"Fixture.lean": "Fixture", "Readonly.lean": "Readonly"}},
            "verification_scope": {"editable_modules": {"Fixture.lean": "Fixture"}},
        }
        self.state = {"formalization": {}, "project_baseline": baseline}
        original = deepcopy(self.state)
        planned = self.write_plan()["project_baseline"]
        self.assertEqual(planned["existing_incomplete_declarations"], {
            "target": {key: baseline["declarations"]["target"][key]
                       for key in ("module", "target_kind", "type", "level_params")}})
        self.assertEqual(planned["verification_scope"], baseline["verification_scope"])
        self.assertEqual(planned["protected_modules"], ["Fixture", "Readonly"])
        self.assertNotIn("migration", planned)
        self.assertEqual(self.state, original)


if __name__ == "__main__":
    unittest.main()
