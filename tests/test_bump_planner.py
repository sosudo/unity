"""Declaration scheduling contracts, not assertions about old module behavior."""
from copy import deepcopy
import unittest

from unity import bump_inventory, bump_planner, bump_spec
from test_bump_inventory import fixture_index, fixture_source, name_ast


def diagnostics(*lines, passed=False):
    return {"source_sha256": "d" * 64, "passed": passed, "unmapped_error_count": 0,
            "diagnostics": [{"id": "diag-" + str(line), "path": "Fixture.lean", "line": line,
                 "column": 1, "severity": "error", "kind": "declaration", "message": "test error"} for line in lines]}


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.index, self.source = fixture_index(), fixture_source()

    def test_long_declaration_chain_does_not_require_recursive_python_traversal(self):
        rows = {str(i): {"dependencies": [str(i - 1)] if i else []} for i in range(2500)}
        components = bump_planner._components(rows)
        self.assertEqual(len(components), 2500)
        self.assertTrue(all(len(component) == 1 for component in components))

    def test_two_errors_in_one_file_are_two_independent_tasks(self):
        plan = bump_planner.plan_repairs(self.index, diagnostics(1, 3), self.source)
        self.assertEqual(len(plan["chunks"]), 2)
        self.assertEqual({row["lean_file"] for row in plan["chunks"]}, {"Fixture.lean"})
        self.assertTrue(all(row["migration"]["kind"] == "declaration" for row in plan["chunks"]))
        self.assertTrue(all(not row["dependencies"] for row in plan["chunks"]))
        self.assertEqual(len(plan["requirements"]), 3)
        self.assertEqual(sum(not row["tasks"] for row in plan["requirements"]), 1)
        spec = bump_spec.normalize_spec(plan["spec"], source=self.source, requirements=plan["requirements"], tasks=plan["chunks"])
        self.assertEqual(len(bump_spec.normalize_informal_nodes(plan["chunks"], plan["requirements"], spec, self.source)), 2)

    def test_original_declaration_dependency_orders_actual_assignments(self):
        plan = bump_planner.plan_repairs(self.index, diagnostics(1, 5), self.source)
        nodes = {row["lean_decl"]: row for row in plan["chunks"]}
        self.assertEqual(nodes["third"]["dependencies"], [nodes["first"]["id"]])

    def test_clean_upgrade_keeps_full_obligations_without_fake_task(self):
        plan = bump_planner.plan_repairs(self.index, diagnostics(passed=True), self.source)
        self.assertEqual(plan["chunks"], [])
        self.assertEqual(len(plan["requirements"]), 3)
        self.assertTrue(all(row["tasks"] == [] for row in plan["requirements"]))
        bump_spec.normalize_spec(plan["spec"], source=self.source, requirements=plan["requirements"], tasks=[])

    def empty_module(self):
        index = deepcopy(self.index)
        index["occurrences"] = {}
        index["modules"]["Fixture"].update(occurrence_ids=[], line_lengths=[20, 0])
        index["index_sha256"] = bump_inventory.digest({k: v for k, v in index.items() if k != "index_sha256"})
        return index

    def test_empty_native_module_has_static_command_obligation_without_fake_task(self):
        index = self.empty_module()
        commands = bump_planner.empty_module_commands(index)
        self.assertEqual(len(commands), 1)
        key = next(iter(commands))
        plan = bump_planner.plan_repairs(index, diagnostics(passed=True), self.source)
        self.assertEqual(plan["chunks"], [])
        self.assertEqual(plan["requirements"][0]["id"], "requirement-" + key)
        self.assertEqual(plan["requirements"][0]["tasks"], [])
        self.assertEqual(plan["spec"]["scope"]["targets"], ["anchor-" + key])

    def test_empty_module_error_is_a_bounded_command_not_module_assignment(self):
        index = self.empty_module()
        clean = bump_planner.plan_repairs(index, diagnostics(passed=True), self.source)
        planned = bump_planner.plan_repairs(index, diagnostics(1), self.source)
        node = planned["chunks"][0]
        self.assertEqual(node["migration"]["kind"], "command")
        self.assertEqual(node["migration"]["original_ids"], [])
        self.assertEqual(node["migration"]["command_line"], 1)
        self.assertEqual(node["migration"]["original_ranges"], [{"start_line": 1, "end_line": 1, "start_column": 0, "end_column": 20}])
        self.assertIn(node["migration"]["command_obligation"], bump_planner.empty_module_commands(index))
        self.assertEqual({key: value for key, value in clean["requirements"][0].items() if key != "tasks"},
                         {key: value for key, value in planned["requirements"][0].items() if key != "tasks"})
        bump_spec.normalize_spec(planned["spec"], source=self.source, requirements=planned["requirements"], tasks=planned["chunks"])

    def test_empty_module_command_cannot_claim_lines_outside_original_source(self):
        with self.assertRaisesRegex(ValueError, "sealed original source lines"):
            bump_planner.plan_repairs(self.empty_module(), diagnostics(3), self.source)

    def test_refresh_retains_completed_task_and_discovers_new_declaration(self):
        first = bump_planner.plan_repairs(self.index, diagnostics(1), self.source)
        old = first["chunks"][0]
        old.update(status="complete", accepted_candidate="accepted-one")
        refreshed = bump_planner.plan_repairs(self.index, diagnostics(3), self.source, {"formal_tasks": {old["id"]: old}})
        nodes = {row["id"]: row for row in refreshed["chunks"]}
        self.assertEqual(nodes[old["id"]]["informal_statement"], old["informal_statement"])
        self.assertEqual(nodes[old["id"]]["accepted_candidate"], "accepted-one")
        self.assertNotIn(old["id"], refreshed["compiler_tasks"])
        self.assertEqual(len(nodes), 2)

    def test_refined_declaration_owner_is_not_replaced_by_retired_parent(self):
        plan = bump_planner.plan_repairs(self.index, diagnostics(1), self.source)
        parent = plan["chunks"][0]
        child = deepcopy(parent)
        child["id"] = child["task_id"] = "refined-declaration"
        refreshed = bump_planner.plan_repairs(self.index, diagnostics(1), self.source,
            {"formal_tasks": {child["id"]: child}, "retired_tasks": {parent["id"]: {**parent, "replaced_by": [child["id"]]}}})
        self.assertEqual(refreshed["compiler_tasks"], [child["id"]])
        self.assertEqual([row["id"] for row in refreshed["chunks"]], [child["id"]])

    def test_mutually_dependent_declarations_are_one_explicit_scc(self):
        first = bump_inventory.occurrence_id("Fixture", name_ast("first"))
        second = bump_inventory.occurrence_id("Fixture", name_ast("second"))
        self.index["occurrences"][first]["dependencies"] = [second]
        self.index["occurrences"][second]["dependencies"] = [first]
        self.index["index_sha256"] = bump_inventory.digest({k: v for k, v in self.index.items() if k != "index_sha256"})
        plan = bump_planner.plan_repairs(self.index, diagnostics(1), self.source)
        self.assertEqual(len(plan["chunks"]), 1)
        self.assertEqual(plan["chunks"][0]["migration"]["kind"], "mutual")
        self.assertEqual(set(plan["chunks"][0]["migration"]["original_ids"]), {first, second})

    def test_group_kind_accepts_exact_native_source_family_without_claiming_mutual(self):
        rows = deepcopy(self.index["occurrences"])
        first, second = [next(key for key, row in rows.items() if row["display_name"] == name)
                         for name in ("first", "second")]
        rows[second]["range"] = deepcopy(rows[first]["range"])
        self.assertEqual(bump_planner.declaration_group_kind(rows, [first, second]), "declaration")

    def test_group_kind_rejects_unrelated_same_file_declarations_and_overlapping_ranges(self):
        rows = deepcopy(self.index["occurrences"])
        first, second = [next(key for key, row in rows.items() if row["display_name"] == name)
                         for name in ("first", "second")]
        with self.assertRaisesRegex(ValueError, "separate assignments"):
            bump_planner.declaration_group_kind(rows, [first, second])
        rows[second]["range"] = {**rows[first]["range"], "start_column": 1}
        with self.assertRaisesRegex(ValueError, "separate assignments"):
            bump_planner.declaration_group_kind(rows, [first, second])

    def test_group_kind_requires_structural_connection_for_unranged_generated_member(self):
        rows = deepcopy(self.index["occurrences"])
        first, second = [next(key for key, row in rows.items() if row["display_name"] == name)
                         for name in ("first", "second")]
        rows[second]["range"] = None
        rows[second]["is_internal"] = True
        with self.assertRaisesRegex(ValueError, "connect structurally"):
            bump_planner.declaration_group_kind(rows, [first, second])
        rows[second]["dependencies"] = [first]
        self.assertEqual(bump_planner.declaration_group_kind(rows, [first, second]), "declaration")

    def test_group_kind_recognizes_whole_mutual_component_and_rejects_unrelated_union(self):
        rows = deepcopy(self.index["occurrences"])
        first, second, third = [next(key for key, row in rows.items() if row["display_name"] == name)
                                for name in ("first", "second", "third")]
        rows[first]["dependencies"] = [second]
        rows[second]["dependencies"] = [first]
        self.assertEqual(bump_planner.declaration_group_kind(rows, [first, second]), "mutual")
        with self.assertRaisesRegex(ValueError, "separate assignments"):
            bump_planner.declaration_group_kind(rows, [first, second, third])

    def test_critic_reopen_remains_active_without_diagnostics_and_keeps_metadata(self):
        plan = bump_planner.plan_repairs(self.index, diagnostics(1), self.source)
        node = plan["chunks"][0]
        node.update(status="pending", faithfulness={"status": "changes_requested"})
        node["migration"]["critic_reopen"] = True
        prior = {"formal_tasks": {node["id"]: node}}
        for observed in (diagnostics(passed=True), diagnostics(1)):
            with self.subTest(observed=observed):
                refreshed = bump_planner.plan_repairs(self.index, observed, self.source, prior)
                self.assertIn(node["id"], refreshed["compiler_tasks"])
                self.assertTrue(refreshed["chunks"][0]["migration"]["critic_reopen"])

    def test_blocked_module_cannot_make_known_declaration_debt_disappear(self):
        plan = bump_planner.plan_repairs(self.index, diagnostics(1, 3), self.source)
        prior = {row["id"]: row for row in plan["chunks"]}
        for row in prior.values():
            row["status"] = "pending"
        observed = diagnostics()
        observed["blocked_modules"] = ["Fixture"]
        refreshed = bump_planner.plan_repairs(self.index, observed, self.source, {"formal_tasks": prior})
        self.assertEqual(set(refreshed["compiler_tasks"]), set(prior))
        next(iter(prior.values()))["status"] = "complete"
        refreshed = bump_planner.plan_repairs(self.index, observed, self.source, {"formal_tasks": prior})
        self.assertEqual(len(refreshed["compiler_tasks"]), 1)

    def test_unlocated_build_failure_is_not_silently_accepted(self):
        data = diagnostics()
        data["unmapped_error_count"] = 1
        with self.assertRaisesRegex(ValueError, "unlocated"):
            bump_planner.plan_repairs(self.index, data, self.source)
