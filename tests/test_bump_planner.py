import copy
import unittest

from unity import bump_planner as planner
from unity.bump_inventory import digest
from tests.test_bump_inventory import fixture_index
from tests.test_bump_diagnostics import fixture_diagnostics, fixture_imports


class BumpPlannerTests(unittest.TestCase):
    def imports_receipt(self, index, edges):
        receipt = fixture_diagnostics(index, compiled=["A"])
        receipt["target_imports"] = fixture_imports(index, edges=edges)
        receipt["snapshot_sha256"] = digest({k: v for k, v in receipt.items() if k != "snapshot_sha256"})
        return receipt

    def test_new_local_import_waits_for_new_prerequisite(self):
        index = fixture_index()
        plan = planner.plan_repairs(index, self.imports_receipt(index, {"D": ["B"]}))
        self.assertEqual(plan["dependencies"]["D"], ["B"])
        self.assertEqual(plan["tasks"]["D"]["status"], "blocked")
        self.assertEqual(plan["tasks"]["D"]["blocked_by"], ["B"])

    def test_removed_original_import_remains_conservative_prerequisite(self):
        index = fixture_index()
        plan = planner.plan_repairs(index, self.imports_receipt(index, {"C": []}))
        self.assertEqual(plan["dependencies"]["C"], ["B"])
        self.assertEqual(plan["tasks"]["C"]["status"], "blocked")

    def test_native_edge_outside_scope_or_union_cycle_fails_closed(self):
        index = fixture_index()
        for edges in ({"D": ["Excluded"]}, {"A": ["B"]}):
            with self.subTest(edges=edges), self.assertRaisesRegex(ValueError, "scope|cycle"):
                planner.plan_repairs(index, self.imports_receipt(index, edges))

    def test_target_edge_cannot_be_removed_from_plan_after_reseal(self):
        index = fixture_index()
        plan = planner.plan_repairs(index, self.imports_receipt(index, {"D": ["B"]}))
        plan["dependencies"]["D"] = []
        plan["plan_sha256"] = digest({k: v for k, v in plan.items() if k != "plan_sha256"})
        with self.assertRaisesRegex(ValueError, "prerequisites"):
            planner.validate_repair_plan(index, plan)

    def test_clean_prerequisite_unlocks_root_errors_not_cascades(self):
        index = fixture_index()
        plan = planner.plan_repairs(index, fixture_diagnostics(index, compiled=["A"]))
        self.assertEqual(set(plan["tasks"]), {"B", "C", "D"})
        self.assertEqual(plan["tasks"]["B"]["status"], "repair")
        self.assertEqual(plan["tasks"]["D"]["status"], "repair")
        self.assertEqual(plan["tasks"]["C"]["status"], "blocked")
        self.assertEqual(set(plan["task_bindings"]), {"A", "B", "C", "D"})

    def test_old_ranges_are_not_reused_after_source_edit(self):
        index = fixture_index()
        receipt = fixture_diagnostics(index, compiled=["A"])
        receipt["module_source_hashes"]["B"] = "f" * 64
        receipt["snapshot_sha256"] = digest({k: v for k, v in receipt.items() if k != "snapshot_sha256"})
        plan = planner.plan_repairs(index, receipt)
        with_errors = [row for row in plan["tasks"]["B"]["declaration_subtasks"] if row["diagnostic_ids"]]
        self.assertEqual(with_errors[0]["kind"], "module")
        self.assertEqual(with_errors[0]["original_ids"], [])

    def test_subtasks_never_authorize_partial_module_promotion(self):
        index = fixture_index()
        plan = planner.plan_repairs(index, fixture_diagnostics(index, compiled=["A"]))
        for task in plan["tasks"].values():
            self.assertTrue(task["single_writer"])
            self.assertEqual(task["execution_unit"], "module")
            self.assertEqual(len(plan["task_bindings"][task["task_id"]]["files"]), 1)

    def test_dropping_original_obligation_rejected_even_after_reseal(self):
        index = fixture_index()
        plan = planner.plan_repairs(index, fixture_diagnostics(index))
        plan["task_bindings"]["A"]["obligation_ids"] = []
        plan["plan_sha256"] = digest({k: v for k, v in plan.items() if k != "plan_sha256"})
        with self.assertRaisesRegex(ValueError, "drops"):
            planner.validate_repair_plan(index, plan)

    def test_dropping_diagnostic_rejected_even_after_reseal(self):
        index = fixture_index()
        plan = planner.plan_repairs(index, fixture_diagnostics(index))
        plan["tasks"]["B"]["declaration_subtasks"][0]["diagnostic_ids"] = []
        plan["plan_sha256"] = digest({k: v for k, v in plan.items() if k != "plan_sha256"})
        with self.assertRaisesRegex(ValueError, "drop"):
            planner.validate_repair_plan(index, plan)

    def test_sccs_group_mutual_declarations_without_recursion(self):
        nodes = [str(i) for i in range(2000)]
        edges = {node: [str((int(node) + 1) % len(nodes))] for node in nodes}
        self.assertEqual(planner._components(nodes, edges), [sorted(nodes)])

    def test_replan_preserves_lineage_and_coverage_after_errors_disappear(self):
        index = fixture_index()
        first = planner.plan_repairs(index, fixture_diagnostics(index, compiled=["A"]))
        final = planner.plan_repairs(index, fixture_diagnostics(index, compiled=index["modules"], errors=()), first)
        self.assertEqual(final["tasks"], {})
        self.assertEqual(final["task_bindings"], first["task_bindings"])
        self.assertEqual(final["generation"], 2)


if __name__ == "__main__":
    unittest.main()
