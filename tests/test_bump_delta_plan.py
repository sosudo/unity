"""Original-location hints survive normalization without adopting implementations."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from unity import bump_delta as delta, bump_project as project
from unity import bump_runtime as runtime
from test_bump_manifest_repair import informal_dag, project_baseline
from test_bump_project import inventory, record


def changes_baseline(base):
    """Legacy copied delta fixture, independent of Bump's migration CLI."""
    result = deepcopy(base)
    result.update(version=2, policy="changes-v1", project_scope="changes",
                  declarations={}, original_contexts={}, project_axioms=[],
                  project_sorries=[], project_used_axioms=[])
    result["layout"].update(project_scope="changes", verification_modules={},
                            default_modules=dict(result["layout"]["modules"]),
                            unknown_default_targets=[])
    result["import_headers"] = {p: [] for p in result["layout"]["modules"]}
    result["scope"] = {"mode": "natural", "bound": False, "existing_targets": []}
    return project._seal(result)


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
