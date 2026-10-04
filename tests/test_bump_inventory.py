"""Pure original inventory controls; executed remotely with the Bump suite."""
import unittest
from copy import deepcopy

from unity import bump_inventory as inventory


def name_ast(name):
    return ["str", ["anonymous"], name]


def fixture_index():
    report = {"mode": "index", "module": "Fixture", "raw_declaration_count": 3,
              "declaration_inventory": "raw-module-constants-v1", "declarations": []}
    for name, line, dependencies in (("first", 1, []), ("second", 3, []), ("third", 5, ["first"])):
        report["declarations"].append({"name_ast": name_ast(name), "display_name": name, "kind": "theorem",
            "range": {"start_line": line, "start_column": 0, "end_line": line + 1, "end_column": 20},
            "dependencies": [{"module": "Fixture", "name_ast": name_ast(dependency)} for dependency in dependencies],
            "direct_sorry": False, "is_internal": False})
    return inventory.assemble_index({"Fixture": report}, {"Fixture": {"path": "Fixture.lean", "imports": []}},
                                    {"Fixture.lean": "a" * 64}, scope_sha256="b" * 64, environment={})


def fixture_source():
    return {"kind": "supplied_sources", "migration": True, "candidate_id": "source-" + "c" * 64,
            "sha256": "c" * 64, "source_refs": [{"ref_id": "source:project/Fixture.lean",
                "path": ".unity/source/project/Fixture.lean", "sha256": "a" * 64}]}


class InventoryTests(unittest.TestCase):
    def test_index_is_compact_and_dependency_ids_are_native_occurrences(self):
        index = fixture_index()
        self.assertEqual(len(index["occurrences"]), 3)
        first = inventory.occurrence_id("Fixture", name_ast("first"))
        third = inventory.occurrence_id("Fixture", name_ast("third"))
        self.assertEqual(index["occurrences"][third]["dependencies"], [first])
        self.assertNotIn("meaning", index["occurrences"][first])
        self.assertNotIn("axioms", index["occurrences"][first])

    def test_structural_names_do_not_collapse_string_and_numeric_components(self):
        self.assertNotEqual(inventory.occurrence_id("Fixture", ["str", ["anonymous"], "1"]),
                            inventory.occurrence_id("Fixture", ["num", ["anonymous"], 1]))

    def test_missing_occurrence_dependency_rejected(self):
        index = fixture_index()
        index["occurrences"].pop(next(iter(index["occurrences"])))
        index["index_sha256"] = inventory.digest({k: v for k, v in index.items() if k != "index_sha256"})
        with self.assertRaises((ValueError, KeyError)):
            inventory.validate_index(index)

    def test_index_change_rejected(self):
        index = fixture_index()
        next(iter(index["occurrences"].values()))["display_name"] = "changed"
        with self.assertRaisesRegex(ValueError, "seal"):
            inventory.validate_index(index)
