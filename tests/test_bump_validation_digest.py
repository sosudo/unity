"""Lossless seal validation must not copy giant native trees or cache authority."""
import copy
import unittest
from unittest.mock import patch

from unity import bump_contract, bump_project


class ValidationDigestTests(unittest.TestCase):
    def payload(self):
        expression = ["const", ["str", ["anonymous"], "quoted.⋙₂"], []]
        for index in range(80):
            expression = ["app", expression, ["bvar", index]]
        return {"native": expression, "unicode": "κ 𝕜", "null": None,
                "numbers": [0, 1, 1.0, -0.0, 1e30], "flags": [True, False]}

    def test_baseline_digest_is_exact_old_seal(self):
        value = self.payload()
        value.update(sha256="stale", artifact_id="included-in-baseline")
        before = copy.deepcopy(value)
        expected = bump_project._seal(value)["sha256"]
        with patch.object(bump_project.copy, "deepcopy", side_effect=AssertionError("copy")):
            self.assertEqual(bump_project._baseline_digest(value), expected)
        self.assertEqual(value, before)

    def test_contract_digest_is_exact_old_seal(self):
        value = self.payload()
        value.update(sha256="stale", artifact_id="excluded-from-contract")
        before = copy.deepcopy(value)
        expected = bump_contract._seal_contract(value)["sha256"]
        with patch.object(bump_contract.copy, "deepcopy", side_effect=AssertionError("copy")):
            self.assertEqual(bump_contract._contract_digest(value), expected)
        self.assertEqual(value, before)

    def test_nested_mutation_recomputed_each_time(self):
        for digest in (bump_project._baseline_digest, bump_contract._contract_digest):
            with self.subTest(digest=digest.__name__):
                value = self.payload()
                original = digest(value)
                value["native"][2][1] += 1
                self.assertNotEqual(digest(value), original)
                value["native"][2][1] -= 1
                self.assertEqual(digest(value), original)

    def test_excluded_fields_remain_exact(self):
        value = self.payload()
        baseline = bump_project._baseline_digest(value)
        contract = bump_contract._contract_digest(value)
        value["sha256"] = "ignored"
        self.assertEqual(bump_project._baseline_digest(value), baseline)
        self.assertEqual(bump_contract._contract_digest(value), contract)
        value["artifact_id"] = "record"
        self.assertNotEqual(bump_project._baseline_digest(value), baseline)
        self.assertEqual(bump_contract._contract_digest(value), contract)

    def test_publication_still_deep_copies(self):
        for seal in (bump_project._seal, bump_contract._seal_contract):
            with self.subTest(seal=seal.__name__):
                value = self.payload()
                sealed = seal(value)
                value["native"][2][1] += 1
                self.assertNotEqual(sealed["native"], value["native"])

    def reports(self):
        result = {}
        for module in ("A", "B"):
            result[module] = {
                "declarations": {"same": {"kind": "theorem", "axioms": [module + ".trust"]}},
                "meanings": {"same": {"meaning": {
                    "name": ["str", ["anonymous"], "same"], "type": self.payload()["native"]}}}}
        return result

    def test_occurrence_comparison_matches_full_copy_without_allocating(self):
        reports = self.reports()
        expected = bump_project.migration_declarations(reports)
        self.assertEqual(len(expected), 2)
        with patch.object(bump_project.copy, "deepcopy", side_effect=AssertionError("copy")):
            self.assertTrue(bump_project._migration_declarations_match(reports, expected))

    def test_occurrence_comparison_rejects_every_altered_field_and_extras(self):
        reports = self.reports()
        expected = bump_project.migration_declarations(reports)
        occurrence = next(iter(expected))
        for field in expected[occurrence]:
            altered = copy.deepcopy(expected)
            altered[occurrence][field] = "changed"
            self.assertNotEqual(bump_project.migration_declarations(reports), altered)
            self.assertFalse(bump_project._migration_declarations_match(reports, altered), field)
        for altered in ({}, {**expected, "extra": expected[occurrence]}):
            self.assertFalse(bump_project._migration_declarations_match(reports, altered))
        reports["A"]["declarations"]["same"]["axioms"].append("new.trust")
        self.assertFalse(bump_project._migration_declarations_match(reports, expected))

    def test_duplicate_structural_occurrences_still_rejected(self):
        reports = self.reports()
        expected = bump_project.migration_declarations(reports)
        reports["A"]["declarations"]["alias"] = reports["A"]["declarations"]["same"]
        reports["A"]["meanings"]["alias"] = reports["A"]["meanings"]["same"]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            bump_project.migration_declarations(reports)
        self.assertFalse(bump_project._migration_declarations_match(reports, expected))


if __name__ == "__main__":
    unittest.main()
