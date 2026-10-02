"""Public dependency briefs retain exact module-scoped verification identity."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from unity import bump_contract as contract
from unity.forum import bump_server as forum

if __package__:
    from .test_bump_occurrence_contract import occurrence_contract
else:
    from test_bump_occurrence_contract import occurrence_contract


class BumpForumOccurrenceTests(unittest.TestCase):
    def state(self):
        bound = occurrence_contract()
        value = {"formalization": {"contract": bound}, "formal_tasks": {}, "formal_candidates": {}}
        for module in bound["bindings"]:
            candidate_id = "candidate:" + module
            outputs = bound["bindings"][module]
            value["formal_tasks"][module] = {
                "status": "complete", "outputs": outputs, "accepted_candidate": candidate_id,
                "verification": {"status": "verified", "candidate_id": candidate_id}}
            value["formal_candidates"][candidate_id] = {
                "status": "merged", "task_id": module, "outputs": outputs, "stage": "complete",
                "verification": {"status": "passed", "verified_targets": contract.output_fingerprints(bound, module)}}
        return value

    def outputs(self, value, modules):
        with patch.object(forum.bump_state, "candidate_is_current", return_value=True):
            return forum._verified_dependency_outputs(value, set(modules))

    def test_same_named_occurrences_advertised_only_from_own_receipts(self):
        value = self.state()
        actual = self.outputs(value, ["Fixture", "Empty"])
        self.assertEqual({row["task_id"] for row in actual}, {"Fixture", "Empty"})
        self.assertEqual([row["outputs"][0]["declaration"] for row in actual], ["result", "result"])

    def test_sibling_receipt_and_extra_occurrence_are_not_accepted(self):
        value = self.state()
        first = value["formal_candidates"]["candidate:Fixture"]["verification"]
        sibling = value["formal_candidates"]["candidate:Empty"]["verification"]
        for modified in (deepcopy(sibling["verified_targets"]),
                         {**first["verified_targets"], **sibling["verified_targets"]}):
            changed = deepcopy(value)
            changed["formal_candidates"]["candidate:Fixture"]["verification"]["verified_targets"] = modified
            self.assertEqual(self.outputs(changed, ["Fixture"]), [])

    def test_flattened_name_receipt_cannot_advertise_migration_dependency(self):
        value = self.state()
        receipt = value["formal_candidates"]["candidate:Fixture"]["verification"]
        receipt["verified_targets"] = {"result": next(iter(receipt["verified_targets"].values()))}
        self.assertEqual(self.outputs(value, ["Fixture"]), [])
