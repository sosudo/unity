import copy
import json
import tempfile
import unittest
from pathlib import Path

from unity import artifacts, bump_inventory as inventory


def name(label):
    return ["str", ["anonymous"], label]


def fixture_index():
    graph = {"A": {"path": "A.lean", "imports": []}, "B": {"path": "B.lean", "imports": ["A"]},
             "C": {"path": "C.lean", "imports": ["B"]}, "D": {"path": "D.lean", "imports": []}}
    reports = {}
    for module in graph:
        row = {"name_ast": name("shared"), "display_name": "shared", "kind": "theorem",
            "range": {"start_line": 2, "start_column": 0, "end_line": 4, "end_column": 0},
            "direct_sorry": False, "is_internal": False, "dependencies": []}
        reports[module] = {"mode": "index", "module": module, "declaration_inventory": "raw-module-constants-v1",
            "raw_declaration_count": 1, "declarations": [row], "imported_modules": [module, *graph[module]["imports"]]}
    return inventory.assemble_index(reports, graph, {row["path"]: module.lower() * 64 for module, row in graph.items()},
                                    scope_sha256="f" * 64, environment={"lean_version": "fixture"})


class BumpSlimInventoryTests(unittest.TestCase):
    def test_module_occurrence_identity_and_all_originals_retained(self):
        index = fixture_index()
        self.assertEqual(len(index["occurrences"]), 4)
        self.assertNotEqual(inventory.occurrence_id("A", name("shared")), inventory.occurrence_id("B", name("shared")))
        self.assertNotIn("meanings", json.dumps(index))
        inventory.validate_index(index)

    def test_typed_names_do_not_collapse_display_aliases(self):
        self.assertNotEqual(inventory.occurrence_id("A", ["str", name("a"), "b"]),
                            inventory.occurrence_id("A", name("a.b")))
        self.assertNotEqual(inventory.occurrence_id("A", ["num", name("a"), 7]),
                            inventory.occurrence_id("A", ["str", name("a"), "7"]))

    def test_missing_dependency_rejected_not_clipped(self):
        index = fixture_index()
        next(iter(index["occurrences"].values()))["dependencies"] = ["occ-" + "0" * 64]
        index["index_sha256"] = inventory.digest({k: v for k, v in index.items() if k != "index_sha256"})
        with self.assertRaisesRegex(ValueError, "occurrence fields"):
            inventory.validate_index(index)

    def test_obligation_removal_cannot_hide_behind_resealed_index(self):
        index = fixture_index()
        index["modules"]["A"]["occurrence_ids"] = []
        index["index_sha256"] = inventory.digest({k: v for k, v in index.items() if k != "index_sha256"})
        with self.assertRaisesRegex(ValueError, "drops"):
            inventory.validate_index(index)

    def test_detailed_expression_not_allowed_in_scheduling_schema(self):
        index = fixture_index()
        next(iter(index["occurrences"].values()))["meaning"] = {"value": ["app"]}
        index["index_sha256"] = inventory.digest({k: v for k, v in index.items() if k != "index_sha256"})
        with self.assertRaisesRegex(ValueError, "non-slim"):
            inventory.validate_index(index)

    def test_index_ref_roundtrip_and_modified_blob_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = fixture_index()
            record = artifacts.store_text(root, json.dumps(index), kind="bump_original_index")
            ref = {key: record[key] for key in ("artifact_id", "sha256")}
            self.assertEqual(inventory.load_original_index(root, ref), index)
            (root / "blobs" / record["sha256"]).write_text("{}")
            with self.assertRaisesRegex(ValueError, "bytes changed"):
                inventory.load_original_index(root, ref)

    def test_raw_inventory_count_mismatch_rejected(self):
        graph = {"A": {"path": "A.lean", "imports": []}}
        report = {"mode": "index", "module": "A", "declaration_inventory": "raw-module-constants-v1",
                  "raw_declaration_count": 1, "declarations": []}
        with self.assertRaisesRegex(ValueError, "incomplete"):
            inventory.assemble_index({"A": report}, graph, {"A.lean": "a" * 64}, scope_sha256="f" * 64, environment={})

    def test_deep_name_numeric_bool_is_not_an_integer_identity(self):
        with self.assertRaises(ValueError):
            inventory.name_key(["num", name("a"), True])


if __name__ == "__main__":
    unittest.main()
