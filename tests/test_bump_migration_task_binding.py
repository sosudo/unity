"""Offline identity-binding regressions; no Lean, Git, models or services."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from unity import bump_inventory as inventory, bump_migration as migration, bump_planner as planner
from test_bump_migration_checks import declaration, report


def binding_baseline(groups=None):
    if groups is None:
        rows = [declaration(name) for name in ("first", "second", "third")]
        for row, line in zip(rows, (1, 3, 5)):
            row["range"] = {"start_line": line, "end_line": line,
                            "start_column": 0, "end_column": 20}
        groups = {"Fixture": rows, "Empty": []}
    groups = deepcopy(groups)
    files = {module + ".lean": inventory.digest(module) for module in groups}
    selected = {module: module + ".lean" for module in groups}
    scope = migration.seal({"version": 1, "mode": "build", "selected_modules": selected,
        "excluded_files": {}, "default_modules": {path: module for module, path in selected.items()},
        "build_dir": ".lake/build"})
    native = {}
    for module, rows in groups.items():
        for row in rows:
            row.pop("meaning", None)
            row.pop("axioms", None)
        native[module] = {**report(*rows, mode="index"), "module": module}
    environment = {"config": {}, "dependencies": {}}
    index = inventory.assemble_index(native,
        {module: {"path": path, "imports": [], "line_lengths": [30] * 8}
         for module, path in selected.items()}, files,
        scope_sha256=scope["sha256"], environment=environment)
    info = {"version": 1, "policy": "migration-v1", "run_id": "bump-123456abcdef",
        "source_root": "/fixture/source", "original_root": "/fixture/original",
        "target_root": "/fixture/target", "original_commit": "1" * 40,
        "original_files": files, "original_index": index, "scope": scope,
        "selected_modules": selected, "excluded_files": {},
        "original_environment": environment, "target_environment": environment}
    return migration.seal({"version": 3, "policy": "migration-v1", "project_root": "/fixture/target",
        "project_scope": "build", "branch": "main", "head": "2" * 40,
        "files": files, "tracked_files": sorted(files), "environment": environment,
        "layout": {"build_dir": ".lake/build", "modules": {path: module for module, path in selected.items()}},
        "target_scope": "Preserve the selected original declaration universe.",
        "scope": {"mode": "migration", "bound": True, "existing_targets": sorted(index["occurrences"])},
        "migration": info})


def task(task_id, original_ids):
    return {"id": task_id, "task_id": task_id,
        "requirement_ids": ["requirement-" + key for key in original_ids],
        "revision": 4, "status": "pending", "accepted_candidate": None, "outputs": [],
        "verification": {"status": "pending"}, "faithfulness": {"status": "unreviewed"},
        "attempts": {"Ada": 2}}


def requirements(baseline, tasks):
    index = baseline["migration"]["original_index"]
    obligations = set(index["occurrences"]) | set(planner.empty_module_commands(index))
    return [{"id": "requirement-" + key, "anchor_ids": ["anchor-" + key],
             "tasks": sorted(name for name, row in tasks.items()
                             if "requirement-" + key in row["requirement_ids"])}
            for key in sorted(obligations)]


def live_state(baseline, tasks):
    ledger = requirements(baseline, tasks)
    contract = {"version": 3, "migration_policy": 1, "project_baseline": baseline,
                "requirements": deepcopy(ledger)}
    return {"project_baseline": baseline, "formal_tasks": tasks,
            "formalization": {"contract": contract, "requirements": ledger}}


class MigrationTaskBindingTests(unittest.TestCase):
    def setUp(self):
        self.baseline = binding_baseline()
        self.assertEqual(migration.baseline_errors(self.baseline), [])
        self.index = self.baseline["migration"]["original_index"]
        self.ids = {row["display_name"]: key for key, row in self.index["occurrences"].items()}

    def bind(self, tasks, *, previous=None, compiler=None, baseline=None):
        baseline = baseline or self.baseline
        return migration.bind_task_identities(baseline, requirements(baseline, tasks), tasks,
            previous_tasks=previous or {}, compiler_metadata=compiler)

    def bound_task(self, name="repair", original="first"):
        row = task(name, [self.ids[original]])
        row["migration"] = self.bind({name: row})[name]
        return row

    def command(self, name="command", module="Empty"):
        if module == "Empty":
            governed = [key for key, row in planner.empty_module_commands(self.index).items()
                        if row["module"] == module]
        else:
            governed = self.index["modules"][module]["occurrence_ids"]
        row = task(name, governed)
        metadata = {"kind": "command", "original_ids": [], "path": module + ".lean",
            "module": module, "command_line": 2,
            "original_ranges": [{"start_line": 2, "end_line": 2,
                                 "start_column": 0, "end_column": 30}], "diagnostic_ids": ["diag-command"]}
        if module == "Empty":
            metadata["command_obligation"] = governed[0]
        return row, metadata

    def test_new_declaration_uses_ledger_not_advisory_task_name_or_metadata(self):
        row = task("not-an-original-id", [self.ids["first"]])
        row.update(predicted_kind="command", migration={"kind": "command", "original_ids": []})
        inputs = deepcopy((self.baseline, row))
        metadata = self.bind({row["id"]: row})[row["id"]]
        self.assertEqual(metadata["original_ids"], [self.ids["first"]])
        self.assertEqual((metadata["module"], metadata["path"], metadata["kind"]),
                         ("Fixture", "Fixture.lean", "declaration"))
        self.assertEqual(metadata["original_ranges"], [self.index["occurrences"][self.ids["first"]]["range"]])
        self.assertEqual(metadata["diagnostic_ids"], [])
        self.assertNotIn("verification", metadata)
        self.assertNotIn("outputs", metadata)
        self.assertEqual((self.baseline, row), inputs)

    def test_existing_identity_and_provisional_history_are_copied_without_aliases(self):
        row = self.bound_task()
        row.update(status="complete", accepted_candidate="candidate-one",
                   verification={"status": "provisional", "native_pending": True})
        row["migration"].update(critic_reopen=True, diagnostic_ids=["old-diagnostic"])
        previous, tasks = {"repair": deepcopy(row)}, {"repair": deepcopy(row)}
        inputs = deepcopy((self.baseline, previous, tasks))
        metadata = self.bind(tasks, previous=previous)
        self.assertEqual(metadata["repair"], row["migration"])
        metadata["repair"]["diagnostic_ids"].append("local-edit")
        metadata["repair"]["original_ranges"][0]["end_column"] = 1
        self.assertEqual((self.baseline, previous, tasks), inputs)

    def test_existing_missing_identity_is_not_reconstructed(self):
        row = task("repair", [self.ids["first"]])
        with self.assertRaisesRegex(ValueError, "lacks its controller-owned identity"):
            self.bind({"repair": row}, previous={"repair": deepcopy(row)})

    def test_valid_but_different_assignment_cannot_reuse_an_existing_id(self):
        previous = {"repair": self.bound_task()}
        changed = {"repair": task("repair", [self.ids["second"]])}
        with self.assertRaisesRegex(ValueError, "assigned original declarations"):
            self.bind(changed, previous=previous)

    def test_requirement_ids_must_match_the_canonical_owner_ledger(self):
        tasks = {"repair": task("repair", [self.ids["first"]])}
        ledger = requirements(self.baseline, tasks)
        next(row for row in ledger if row["id"] == "requirement-" + self.ids["first"])["tasks"] = []
        with self.assertRaisesRegex(ValueError, "canonical requirement task assignment"):
            migration.bind_task_identities(self.baseline, ledger, tasks, previous_tasks={})

    def test_original_obligations_cannot_be_omitted_for_a_small_repair_queue(self):
        tasks = {"repair": task("repair", [self.ids["first"]])}
        ledger = requirements(self.baseline, tasks)[:-1]
        with self.assertRaisesRegex(ValueError, "every original occurrence exactly once"):
            migration.bind_task_identities(self.baseline, ledger, tasks, previous_tasks={})

    def test_unrelated_same_file_declarations_remain_separate_assignments(self):
        tasks = {"whole-file": task("whole-file", [self.ids["first"], self.ids["second"]])}
        with self.assertRaisesRegex(ValueError, "separate assignments"):
            self.bind(tasks)

    def test_cross_module_originals_cannot_form_one_task(self):
        baseline = binding_baseline({"Fixture": [declaration("first")], "Other": [declaration("second")]})
        ids = sorted(baseline["migration"]["original_index"]["occurrences"])
        with self.assertRaisesRegex(ValueError, "cross-file work"):
            self.bind({"cross-file": task("cross-file", ids)}, baseline=baseline)

    def test_same_range_generated_family_uses_existing_group_policy(self):
        owner, generated = declaration("owner"), declaration("generated")
        owner["range"] = {"start_line": 1, "end_line": 1, "start_column": 0, "end_column": 20}
        generated["range"] = deepcopy(owner["range"])
        baseline = binding_baseline({"Fixture": [owner, generated]})
        ids = sorted(baseline["migration"]["original_index"]["occurrences"])
        metadata = self.bind({"family": task("family", ids)}, baseline=baseline)["family"]
        self.assertEqual(metadata["kind"], "declaration")
        self.assertEqual(metadata["original_ids"], ids)

    def test_compiler_metadata_cannot_rewrite_declaration_ranges(self):
        row = self.bound_task()
        metadata = deepcopy(row["migration"])
        metadata["original_ranges"][0]["end_column"] += 1
        with self.assertRaisesRegex(ValueError, "declaration ranges differ"):
            self.bind({"repair": row}, previous={"repair": deepcopy(row)}, compiler={"repair": metadata})

    def test_compiler_none_slots_and_refinement_omissions_describe_the_same_family(self):
        owner, generated = declaration("owner"), declaration("generated")
        owner["range"] = {"start_line": 1, "end_line": 1, "start_column": 0, "end_column": 20}
        generated["dependencies"] = [{"module": "Fixture", "name_ast": owner["name_ast"]}]
        baseline = binding_baseline({"Fixture": [owner, generated]})
        index = baseline["migration"]["original_index"]
        ids = sorted(index["occurrences"])
        row = task("family", ids)
        metadata = self.bind({"family": row}, baseline=baseline)["family"]
        row["migration"] = deepcopy(metadata)
        metadata["original_ranges"] = [deepcopy(index["occurrences"][key]["range"]) for key in ids]
        bound = self.bind({"family": row}, previous={"family": deepcopy(row)},
                          compiler={"family": metadata}, baseline=baseline)
        self.assertEqual(bound["family"]["original_ranges"], metadata["original_ranges"])

    def test_new_empty_module_command_requires_controller_metadata(self):
        row, metadata = self.command()
        with self.assertRaisesRegex(ValueError, "controller compiler metadata"):
            self.bind({"command": row})
        bound = self.bind({"command": row}, compiler={"command": metadata})
        self.assertEqual(bound["command"], metadata)
        self.assertEqual(row["verification"], {"status": "pending"})

    def test_existing_bounded_command_preserves_its_static_obligation(self):
        row, metadata = self.command()
        row["migration"] = deepcopy(metadata)
        self.assertEqual(self.bind({"command": row}, previous={"command": deepcopy(row)})["command"], metadata)
        migration.require_task_identities(live_state(self.baseline, {"command": row}))

    def test_nonempty_module_command_governs_originals_without_claiming_them_as_outputs(self):
        row, metadata = self.command(module="Fixture")
        bound = self.bind({"command": row}, compiler={"command": metadata})
        self.assertEqual(bound["command"]["original_ids"], [])
        self.assertEqual(set(row["requirement_ids"]), {"requirement-" + key
                                                       for key in self.index["modules"]["Fixture"]["occurrence_ids"]})
        self.assertEqual(row["outputs"], [])

    def test_command_location_and_obligation_forgery_are_rejected(self):
        row, metadata = self.command()
        for changed in (dict(metadata, command_line=True), dict(metadata, command_line=0),
                        dict(metadata, command_line=9), dict(metadata, module="Fixture"),
                        dict(metadata, command_obligation="not-the-original-command"),
                        dict(metadata, original_ranges=[{"start_line": 1, "end_line": 8}])):
            with self.subTest(metadata=changed), self.assertRaises(ValueError):
                self.bind({"command": row}, compiler={"command": changed})

    def test_existing_command_cannot_move_to_another_valid_line(self):
        row, metadata = self.command()
        row["migration"] = deepcopy(metadata)
        changed = deepcopy(metadata)
        changed["command_line"] = 3
        changed["original_ranges"] = [{"start_line": 3, "end_line": 3, "start_column": 0, "end_column": 30}]
        with self.assertRaises(ValueError):
            self.bind({"command": row}, previous={"command": deepcopy(row)}, compiler={"command": changed})

    def test_strict_validator_rejects_corrupt_retained_tasks_without_mutation(self):
        row = self.bound_task()
        state = live_state(self.baseline, {"repair": row})
        migration.require_task_identities(state)
        for field, value in (("migration", None), ("requirement_ids", [])):
            changed = deepcopy(state)
            changed["formal_tasks"]["repair"][field] = value
            before = deepcopy(changed)
            with self.subTest(field=field), self.assertRaises(ValueError):
                migration.require_task_identities(changed)
            self.assertEqual(changed, before)

    def test_existing_malformed_identity_fields_do_not_become_new_task_bindings(self):
        row = self.bound_task()
        for field, value in (("kind", []), ("original_ids", []), ("original_ids", [self.ids["first"]] * 2),
                             ("module", "Outside"), ("path", "Outside.lean")):
            previous = deepcopy(row)
            previous["migration"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.bind({"repair": deepcopy(row)}, previous={"repair": previous})

    def test_guard_avoids_rehashing_the_baseline_and_preserves_legacy_behavior(self):
        state = live_state(self.baseline, {"repair": self.bound_task()})
        before = deepcopy(state)
        with patch.object(migration, "baseline_errors", side_effect=AssertionError("redundant baseline seal")):
            migration.require_task_identities(state)
        self.assertEqual(state, before)
        legacy = {"formalization": {"contract": {"version": 3}}, "formal_tasks": {"old": {"status": "complete"}}}
        previous = deepcopy(legacy)
        migration.require_task_identities(legacy)
        self.assertEqual(legacy, previous)

    def test_clean_queue_still_keeps_the_entire_original_requirement_ledger(self):
        state = live_state(self.baseline, {})
        migration.require_task_identities(state)
        self.assertEqual(self.bind({}), {})
        self.assertEqual(len(state["formalization"]["requirements"]), 4)

    def test_lost_migration_policy_cannot_bypass_retained_task_validation(self):
        state = live_state(self.baseline, {"repair": self.bound_task()})
        state["formalization"]["contract"].pop("migration_policy")
        with self.assertRaisesRegex(ValueError, "require a migration contract"):
            migration.require_task_identities(state)

    def test_refine_keeps_task_table_aliases_and_provisional_evidence(self):
        previous = {"repair": self.bound_task()}
        tasks = {"repair": deepcopy(previous["repair"]), "added": task("added", [self.ids["second"]])}
        tasks["repair"].update(status="complete", verification={"status": "provisional", "native_pending": True})
        state = live_state(self.baseline, tasks)
        table, existing, added = state["formal_tasks"], tasks["repair"], tasks["added"]
        original = deepcopy(state)
        migration.refine(state, {"upserts": [deepcopy(added)]}, previous_tasks=previous)
        self.assertIs(state["formal_tasks"], table)
        self.assertIs(table["repair"], existing)
        self.assertIs(table["added"], added)
        for name, row in table.items():
            self.assertEqual({key: value for key, value in row.items() if key != "migration"},
                             {key: value for key, value in original["formal_tasks"][name].items() if key != "migration"})
        self.assertEqual(added["migration"]["original_ids"], [self.ids["second"]])


if __name__ == "__main__":
    unittest.main()
