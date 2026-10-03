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

    def test_complete_current_header_replaces_removed_original_import(self):
        index = fixture_index()
        plan = planner.plan_repairs(index, self.imports_receipt(index, {"C": []}))
        self.assertEqual(plan["dependencies"]["C"], [])
        self.assertEqual(plan["tasks"]["C"]["status"], "build_required")
        self.assertEqual(plan["dependency_provenance"]["C"], "current_native")
        self.assertEqual(plan["task_bindings"]["C"]["obligation_ids"], index["modules"]["C"]["occurrence_ids"])

    def test_native_edge_outside_scope_or_actual_current_cycle_fails_closed(self):
        index = fixture_index()
        for edges in ({"D": ["Excluded"]}, {"A": ["B"]}):
            with self.subTest(edges=edges), self.assertRaisesRegex(ValueError, "scope|cycle"):
                planner.plan_repairs(index, self.imports_receipt(index, edges))

    def test_acyclic_current_graph_can_reverse_old_edge_without_false_union_cycle(self):
        index = fixture_index()
        receipt = fixture_diagnostics(index, errors=("A", "B"))
        receipt["target_imports"] = fixture_imports(index, edges={"A": ["B"], "B": []})
        receipt["snapshot_sha256"] = digest({k: v for k, v in receipt.items() if k != "snapshot_sha256"})
        plan = planner.plan_repairs(index, receipt)
        self.assertEqual(plan["dependencies"]["A"], ["B"])
        self.assertEqual(plan["dependencies"]["B"], [])
        self.assertEqual(plan["unresolved_import_cycles"], [])
        self.assertEqual(plan["tasks"]["B"]["status"], "repair")

    def test_incomplete_header_keeps_fallback_and_only_header_repair_can_bypass_uncertainty(self):
        index = fixture_index()
        receipt = fixture_diagnostics(index, errors=("A", "B"))
        imports = fixture_imports(index, edges={"A": ["B"], "B": []})
        imports["modules"]["B"].update(status="unavailable", unavailable_reason="header_syntax")
        imports["sha256"] = digest({k: v for k, v in imports.items() if k != "sha256"})
        receipt["target_imports"] = imports
        receipt["diagnostics"][1]["kind"] = "syntax"
        receipt["diagnostics"][1]["content_sha256"] = planner.diagnostic_content_key(
            receipt["diagnostics"][1], receipt["diagnostics"][1]["message"])
        receipt["snapshot_sha256"] = digest({k: v for k, v in receipt.items() if k != "snapshot_sha256"})
        plan = planner.plan_repairs(index, receipt)
        self.assertEqual(plan["dependencies"]["B"], ["A"])
        self.assertEqual(plan["dependency_provenance"]["B"], "original_fallback")
        self.assertEqual(plan["unresolved_import_cycles"], [["A", "B"]])
        self.assertTrue(plan["tasks"]["B"]["header_repair_only"])
        self.assertEqual(plan["tasks"]["B"]["blocked_by"], ["A"])
        self.assertEqual(plan["tasks"]["A"]["status"], "blocked")
        self.assertFalse(plan["tasks"]["A"]["header_repair_only"])
        plan["tasks"]["A"]["header_repair_only"] = True
        plan["plan_sha256"] = digest({k: v for k, v in plan.items() if k != "plan_sha256"})
        with self.assertRaisesRegex(ValueError, "header repair"):
            planner.validate_repair_plan(index, plan)

    def test_stable_group_input_ignores_unrelated_sources_and_artifact_identity(self):
        index = fixture_index()
        receipt = fixture_diagnostics(index, compiled=["A"])
        before = planner.plan_repairs(index, receipt)
        updated = copy.deepcopy(receipt)
        updated["source_sha256"] = "9" * 64
        updated["target_imports"]["source_sha256"] = updated["source_sha256"]
        updated["target_imports"]["sha256"] = digest({k: v for k, v in updated["target_imports"].items() if k != "sha256"})
        updated["artifact_ref"] = {"artifact_id": "artifact-" + "1" * 12, "sha256": "2" * 64}
        updated["module_source_hashes"]["D"] = "e" * 64
        for row in updated["diagnostics"]:
            row["id"] += "-new-log"
            row["log_offset"] += 30
        updated["snapshot_sha256"] = digest({k: v for k, v in updated.items() if k != "snapshot_sha256"})
        after = planner.plan_repairs(index, updated, before)
        self.assertNotEqual(before["plan_sha256"], after["plan_sha256"])
        self.assertEqual(before["tasks"]["B"]["input_sha256"], after["tasks"]["B"]["input_sha256"])
        self.assertNotEqual(before["tasks"]["D"]["input_sha256"], after["tasks"]["D"]["input_sha256"])
        updated["module_source_hashes"]["A"] = "f" * 64
        updated["snapshot_sha256"] = digest({k: v for k, v in updated.items() if k != "snapshot_sha256"})
        dependency_changed = planner.plan_repairs(index, updated, after)
        self.assertNotEqual(after["tasks"]["B"]["input_sha256"], dependency_changed["tasks"]["B"]["input_sha256"])

    def test_returned_plan_has_no_mutable_aliases_to_input_receipt_or_index(self):
        index = fixture_index()
        receipt = copy.deepcopy(fixture_diagnostics(index, compiled=["A"]))
        plan = planner.plan_repairs(index, receipt)
        original_plan = copy.deepcopy(plan)
        receipt["target_imports"]["modules"]["D"]["imports"].append("B")
        receipt["module_source_hashes"]["B"] = "e" * 64
        receipt["artifact_ref"]["sha256"] = "0" * 64
        self.assertEqual(plan, original_plan)
        planner.validate_repair_plan(index, plan)

        fresh_receipt = fixture_diagnostics(index, compiled=["A"])
        frozen_receipt, frozen_index = copy.deepcopy(fresh_receipt), copy.deepcopy(index)
        next_plan = planner.plan_repairs(index, fresh_receipt, plan)
        next_plan["target_imports"]["modules"]["B"]["imports"].clear()
        next_plan["module_source_hashes"]["B"] = "f" * 64
        next_plan["diagnostic_artifact"]["sha256"] = "0" * 64
        next_plan["task_bindings"]["B"]["obligation_ids"].clear()
        self.assertEqual(fresh_receipt, frozen_receipt)
        self.assertEqual(index, frozen_index)
        self.assertEqual(plan, original_plan)

    def test_resealed_group_context_or_content_identity_tampering_is_rejected(self):
        index = fixture_index()
        for field in ("group_contexts", "diagnostic_content_keys", "dependency_provenance"):
            with self.subTest(field=field):
                plan = planner.plan_repairs(index, fixture_diagnostics(index, compiled=["A"]))
                if field == "group_contexts":
                    plan[field]["B"]["own_source_sha256"] = "0" * 64
                elif field == "diagnostic_content_keys":
                    plan[field][next(iter(plan[field]))] = "0" * 64
                else:
                    plan[field]["B"] = "original_fallback"
                plan["plan_sha256"] = digest({k: v for k, v in plan.items() if k != "plan_sha256"})
                with self.assertRaises(ValueError):
                    planner.validate_repair_plan(index, plan)

    def test_legacy_plan_validation_keeps_its_union_policy(self):
        index = fixture_index()
        plan = planner.plan_repairs(index, fixture_diagnostics(index, compiled=["A"]))
        for key in ("scheduling_policy", "dependency_provenance", "unresolved_import_cycles", "module_source_hashes",
                    "diagnostic_content_keys", "group_contexts"):
            plan.pop(key)
        for row in plan["tasks"].values():
            row.pop("header_repair_only")
            row.pop("input_sha256")
        plan.update(version=1, kind="bump-diagnostic-plan-v1")
        plan["plan_sha256"] = digest({k: v for k, v in plan.items() if k != "plan_sha256"})
        planner.validate_repair_plan(index, plan)

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
