"""Original-location hints survive normalization without adopting implementations."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from unity import formalize_delta as delta, formalize_project as project
from unity import formalize_runtime as runtime
from test_formalize_manifest_repair import informal_dag, project_baseline
from test_formalize_command import changes_baseline
from test_formalize_project import inventory, record


class DeltaPlanTests(unittest.TestCase):
    def setUp(self):
        self.source = {"candidate_id": "source-fixture", "sha256": "a" * 64,
                       "source_refs": [{"ref_id": "paper"}]}
        self.plan = {**self.source, "solution_candidate": self.source["candidate_id"],
                     "solution_sha256": self.source["sha256"]}
        self.dag = informal_dag(self.source, [{"id": "node", "lean_decl": "target",
                                              "source_components": ["paper"]}])
        self.dag["existing_targets"] = ["target"]
        self.dag["chunks"][0]["outputs"] = [{"declaration": "target", "lean_file": "Main.lean"}]

    def normalize(self, draft=None):
        return runtime.normalize_chunking_dag(draft or self.dag, plan=self.plan,
                                               expected_solution_sha=self.source["sha256"])

    def test_initial_normalized_plan_can_bind_original_target(self):
        baseline = changes_baseline(project_baseline())
        baseline["target_scope"] = "target"
        baseline["scope"] = {"mode": "explicit", "bound": False, "existing_targets": ["target"]}
        baseline["layout"].update(modules={"Main.lean": "Main"}, default_modules={"Main.lean": "Main"})
        baseline["import_headers"] = {"Main.lean": []}
        baseline = project._seal(baseline)
        native = inventory({"target": record("target", hole=True)}, sorries=["target"])
        receipt = project._seal({**delta._receipt_identity(baseline, "Main"), "inspection": native})
        normalized = self.normalize()
        with patch.object(delta, "original_context", return_value=receipt) as inspect:
            bound = project.bind_scope(baseline, normalized, root=Path("/fixture"))
        inspect.assert_called_once()
        self.assertTrue(bound["scope"]["bound"])
        self.assertEqual(bound["scope"]["existing_targets"], ["target"])
        self.assertNotIn("bindings", normalized)

    def test_new_implementation_outputs_are_not_adopted_by_chunking(self):
        self.dag["chunks"][0]["outputs"].append({"declaration": "newTarget", "file": "New.lean"})
        normalized = self.normalize()
        self.assertEqual(normalized["chunks"][0]["outputs"],
                         [{"declaration": "target", "lean_file": "Main.lean"}])

    def test_conflicting_original_locations_fail_during_preflight(self):
        self.dag["chunks"][0]["outputs"].append({"declaration": "target", "lean_file": "Other.lean"})
        with self.assertRaisesRegex(ValueError, "conflicting original locations"):
            self.normalize()

    def test_file_alias_is_canonicalized_without_mutating_agent_draft(self):
        self.dag["chunks"][0]["outputs"] = [{"declaration": "target", "file": "Main.lean"}]
        original = deepcopy(self.dag)
        self.assertEqual(self.normalize()["chunks"][0]["outputs"],
                         [{"declaration": "target", "lean_file": "Main.lean"}])
        self.assertEqual(self.dag, original)


if __name__ == "__main__":
    unittest.main()
