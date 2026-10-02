"""Lossless native Name identities, including Lean's escaped display labels."""

import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unity import bump_migration_contract as contract

if __package__:
    from .test_bump_contract import report, seal
else:
    from test_bump_contract import report, seal


ANON = ["anonymous"]


def string_name(part, prefix=None):
    return ["str", ANON if prefix is None else prefix, part]


def with_external(label, identity):
    value = report()
    row = value["meanings"].pop("External.value")
    row["meaning"]["name"] = copy.deepcopy(identity)
    value["meanings"][label] = row
    value["meanings"]["result"]["meaning"]["type"] = ["const", copy.deepcopy(identity), []]
    value["meanings"]["result"]["dependencies"] = [label]
    return seal(value)


class BumpNameIdentityTests(unittest.TestCase):
    def assert_compares(self, original, current, passed, **kwargs):
        checked = contract.compare_module(original, current, **kwargs)
        self.assertEqual(checked["passed"], passed, checked)
        return checked

    def test_exact_generated_operator_name_that_failed_poly(self):
        component = "_aux_Poly_Bifunctor_Basic___macroRules_CategoryTheory_term_⋙₂__1"
        label = "CategoryTheory.«" + component + "»"
        identity = string_name(component, string_name("CategoryTheory"))
        value = with_external(label, identity)
        self.assertEqual(contract._report_issues(value), [])
        self.assert_compares(value, copy.deepcopy(value), True)

    def test_printing_exceptions_remain_opaque_labels_with_exact_identity(self):
        cases = [
            ("«a.b»", string_name("a.b")),
            ("a.b", string_name("b", string_name("a"))),
            ("«1»", string_name("1")),
            ("1", ["num", ANON, 1]),
            ("«»", string_name("")),
            ("[anonymous]", ANON),
            ("«with space»", string_name("with space")),
            ("λ₂", string_name("λ₂")),
            ("?u", string_name("?u")),
            ("a».b", string_name("a».b")),
            ("x._@.Fixture._hyg.7", ["num", string_name("_hyg", string_name("Fixture",
                string_name("_@", string_name("x")))), 7]),
        ]
        identities = set()
        for label, identity in cases:
            with self.subTest(label=label):
                key = contract._name(identity)
                self.assertNotIn(key, identities)
                identities.add(key)
                value = with_external(label, identity)
                self.assert_compares(value, copy.deepcopy(value), True)

    def test_malformed_structural_names_still_fail(self):
        for identity in [[], ["anonymous", 0], ["str", ANON, 1], ["num", ANON, True],
                         ["num", ANON, -1], ["future", ANON, "x"], ["str", "prefix", "x"]]:
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                contract._name(identity)

    def test_dotted_and_numeric_flattening_collisions_cannot_resolve_references(self):
        for first, second in [(string_name("a.b"), string_name("b", string_name("a"))),
                              (string_name("1"), ["num", ANON, 1])]:
            value = with_external("first", first)
            second_row = copy.deepcopy(value["meanings"]["first"])
            second_row["meaning"]["name"] = second
            value["meanings"]["second"] = second_row
            value["meanings"]["result"]["dependencies"] = ["second"]
            checked = self.assert_compares(seal(value), seal(copy.deepcopy(value)), False)
            self.assertTrue(any("incomplete structural meaning" in issue for issue in checked["issues"]))

    def test_duplicate_structural_identity_under_two_labels_fails(self):
        value = report()
        value["meanings"]["forged_alias"] = copy.deepcopy(value["meanings"]["External.value"])
        self.assert_compares(seal(value), seal(copy.deepcopy(value)), False)

    def test_missing_identity_or_reference_binding_fails(self):
        for mutation in (lambda value: value["meanings"].pop("External.value"),
                         lambda value: value["meanings"]["External.value"]["meaning"].pop("name"),
                         lambda value: value["meanings"]["External.value"]["meaning"].update(
                             name=string_name("different")),
                         lambda value: value["meanings"]["result"].update(dependencies=[])):
            value = report()
            mutation(value)
            self.assert_compares(seal(value), seal(copy.deepcopy(value)), False)

    def test_forged_label_without_rebinding_dependency_fails(self):
        value = report()
        value["meanings"]["forged"] = value["meanings"].pop("External.value")
        self.assert_compares(seal(value), seal(copy.deepcopy(value)), False)

    def test_same_display_label_cannot_hide_changed_structural_name(self):
        old = with_external("opaque display label", string_name("a.b"))
        new = with_external("opaque display label", string_name("b", string_name("a")))
        checked = self.assert_compares(old, new, False)
        self.assertTrue(any("semantic meaning changed" in issue for issue in checked["issues"]))

    def test_explicit_quoted_rename_rewrites_constants_but_not_binder_names(self):
        before_name, after_name = string_name("old.dot"), string_name("new ⋙₂")
        before, after = "«old.dot»", "«new ⋙₂»"
        old, new = with_external(before, before_name), with_external(after, after_name)
        for value, identity in [(old, before_name), (new, after_name)]:
            value["meanings"]["result"]["meaning"]["type"] = [
                "forallE", before_name, ["const", identity, []], ["bvar", 0], "default"]
            seal(value)
        self.assert_compares(old, new, True, correspondences={before: after})
        changed = copy.deepcopy(new)
        changed["meanings"][after]["meaning"]["value"] = ["natVal", 9]
        self.assert_compares(old, seal(changed), False, correspondences={before: after})

    def test_forged_correspondence_to_missing_or_wrong_binding_fails(self):
        old = with_external("«old.dot»", string_name("old.dot"))
        new = with_external("«new.dot»", string_name("new.dot"))
        self.assert_compares(old, new, False, correspondences={"«old.dot»": "missing"})
        self.assert_compares(old, new, False, correspondences={"«old.dot»": "result"})

    def test_native_json_rejects_duplicate_keys_at_any_depth(self):
        for payload in ['{"module":"Fixture","module":"Fixture"}',
                        '{"meanings":{"same":{},"same":{}}}',
                        '{"meaning":{"name":[],"name":[]}}']:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "duplicate native JSON"):
                    json.loads(payload, object_pairs_hook=contract._unique_json_object)
                with patch.object(contract, "_run", return_value=SimpleNamespace(stdout=payload)):
                    with self.assertRaisesRegex(ValueError, "did not return a native JSON report"):
                        contract._native_json(Path("unused"), Path("unused"), "Fixture", ["Fixture"])

    def test_unknown_expressions_and_trust_expansion_remain_blocked(self):
        old = with_external("«⋙₂»", string_name("⋙₂"))
        new = copy.deepcopy(old)
        new["meanings"]["result"]["meaning"]["type"] = ["futureExpr"]
        self.assert_compares(old, seal(new), False)
        plain = report()
        trusted = report(axiom=True)
        self.assert_compares(plain, trusted, False)

    def test_opaque_labels_cannot_hide_inherited_hole_or_native_trust_from_helpers(self):
        for identity in [string_name("sorryAx"), string_name("ofReduceBool", string_name("Lean")),
                         string_name("ofReduceNat", string_name("Lean")),
                         string_name("trustCompiler", string_name("Lean")),
                         string_name("ax_1", ["num", string_name("_native", string_name("Fixture")), 7])]:
            old = report(axiom=True)
            old["meanings"]["Trust"]["meaning"]["name"] = identity
            new = copy.deepcopy(old)
            new["meanings"]["helper"] = copy.deepcopy(new["meanings"]["result"])
            new["meanings"]["helper"]["meaning"]["name"] = string_name("helper")
            new["declarations"]["helper"] = {**new["declarations"]["result"], "name": "helper"}
            checked = self.assert_compares(seal(old), seal(new), False)
            self.assertTrue(any("new helper introduces" in issue for issue in checked["issues"]))


if __name__ == "__main__":
    unittest.main()
