"""Pure migration meaning/trust and preservation checks; no Lean or providers."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity import bump_inventory as inventory, bump_migration as migration


def lean_name(*parts):
    result = ["anonymous"]
    for part in parts:
        result = ["num" if type(part) is int else "str", result, part]
    return result


def const(*parts):
    return ["const", lean_name(*parts), []]


def axiom(*parts):
    return {"reference": {"module": "Fixture", "name_ast": lean_name(*parts)},
            "display_name": ".".join(map(str, parts)), "kind": "axiom", "unsafe": False,
            "type": ["sort", ["zero"]], "level_params": []}


def declaration(*parts, kind="def", display=None):
    name = lean_name(*parts)
    meaning = {"name": name, "kind": kind, "level_params": [], "type": const("Nat")}
    if kind == "def":
        meaning.update(value=["natVal", 1], hints=["regular", 1], safety="safe", all=[name])
    return {"name_ast": name, "display_name": display or ".".join(map(str, parts)),
            "kind": kind, "range": None, "is_internal": False, "direct_sorry": False,
            "dependencies": [], "meaning": meaning, "axioms": []}


def report(*rows, mode="local-meanings"):
    return {"schema_version": 1, "mode": mode, "module": "Fixture",
            "declaration_inventory": "raw-module-constants-v1",
            "raw_declaration_count": len(rows), "declarations": list(rows),
            "imported_modules": ["Init", "Fixture"], "compiled_modules": []}


def baseline(root=Path("/fixture"), files=None, rows=None):
    files = files or {"Fixture.lean": "a" * 64, "Notes.lean": "b" * 64}
    rows = deepcopy(rows or [declaration("Example", "original")])
    for row in rows:
        row.pop("meaning")
        row.pop("axioms")
    scope = migration.seal({"version": 1, "mode": "build",
        "selected_modules": {"Fixture": "Fixture.lean"},
        "excluded_files": {name: sha for name, sha in files.items() if name != "Fixture.lean"},
        "default_modules": {"Fixture.lean": "Fixture"}, "build_dir": ".lake/build"})
    environment = {"config": {}, "dependencies": {}}
    index = inventory.assemble_index({"Fixture": report(*rows, mode="index")},
        {"Fixture": {"path": "Fixture.lean", "imports": []}}, files,
        scope_sha256=scope["sha256"], environment=environment)
    info = {"version": 1, "policy": "migration-v1", "run_id": "bump-123456abcdef",
        "source_root": str(root / "source"), "original_root": str(root / "original"),
        "target_root": str(root / "target"), "original_commit": "a" * 40,
        "original_files": files, "original_index": index, "scope": scope,
        "selected_modules": scope["selected_modules"], "excluded_files": scope["excluded_files"],
        "target_environment": environment, "original_environment": environment}
    return migration.seal({"version": 3, "policy": "migration-v1",
        "project_root": str(root / "target"), "project_scope": "build", "branch": "main", "head": "b" * 40,
        "files": files, "tracked_files": sorted(files), "environment": environment,
        "layout": {"build_dir": ".lake/build", "verification_modules": {"Fixture.lean": "Fixture"}},
        "target_scope": "Preserve all original declarations",
        "scope": {"mode": "migration", "bound": True, "existing_targets": sorted(index["occurrences"])},
        "migration": info})


class MigrationMeaningTests(unittest.TestCase):
    def assert_rejected(self, original, current, correspondences=None):
        try:
            result = migration.compare_reports(original, current, correspondences)
        except ValueError:
            return
        self.assertFalse(result["passed"], result)
        self.assertTrue(result["issues"], result)

    def test_identical_definition_and_theorem_meanings_pass(self):
        old = report(declaration("Example", "value"), declaration("Example", "proof", kind="theorem"))
        self.assertTrue(migration.compare_reports(old, deepcopy(old))["passed"])

    def test_definition_body_type_universe_and_kind_changes_are_rejected(self):
        old = report(declaration("Example", "value"))
        for field, changed in (("value", ["natVal", 2]), ("type", const("Int")),
                               ("level_params", [lean_name("u")]), ("kind", "axiom")):
            with self.subTest(field=field):
                new = deepcopy(old)
                new["declarations"][0]["meaning"][field] = changed
                self.assert_rejected(old, new)

    def test_theorem_type_change_is_rejected(self):
        old = report(declaration("Example", "proof", kind="theorem"))
        new = deepcopy(old)
        new["declarations"][0]["meaning"]["type"] = const("False")
        self.assert_rejected(old, new)

    def test_new_axiom_or_inherited_sorry_spread_is_rejected(self):
        old = report(declaration("Example", "proof", kind="theorem"))
        for trust in (axiom("NewAssumption"), axiom("sorryAx")):
            with self.subTest(trust=trust["display_name"]):
                new = deepcopy(old)
                new["declarations"][0]["axioms"] = [trust]
                self.assert_rejected(old, new)

    def test_existing_trust_can_be_preserved_but_its_type_cannot_change(self):
        old = report(declaration("Example", "proof", kind="theorem"))
        old["declarations"][0]["axioms"] = [axiom("OldAssumption")]
        self.assertTrue(migration.compare_reports(old, deepcopy(old))["passed"])
        new = deepcopy(old)
        new["declarations"][0]["axioms"][0]["type"] = const("False")
        self.assert_rejected(old, new)

    def test_direct_sorry_cannot_be_added_without_changing_reported_axioms(self):
        old = report(declaration("Example", "proof", kind="theorem"))
        new = deepcopy(old)
        new["declarations"][0]["direct_sorry"] = True
        self.assert_rejected(old, new)

    def test_explicit_typed_rename_updates_definition_and_constant_references(self):
        old_value = declaration("Example", "old")
        old_use = declaration("Example", "use")
        old_use["meaning"]["value"] = const("Example", "old")
        new_use = deepcopy(old_use)
        new_use["meaning"]["value"] = const("Example", "new")
        old, new = report(old_value, old_use), report(declaration("Example", "new"), new_use)
        self.assert_rejected(old, new)
        checked = migration.compare_reports(old, new, {"Example.old": "Example.new"})
        self.assertTrue(checked["passed"], checked)

    def test_quoted_component_and_numeric_name_identity_are_not_flattened(self):
        quoted = declaration("Example", "a.b", display="Example.«a.b»")
        numbered = declaration("Example", 7, display="Example.7")
        old = report(quoted, numbered)
        self.assertTrue(migration.compare_reports(old, deepcopy(old))["passed"])
        new = report(declaration("Example", "a", "b", display="Example.«a.b»"), numbered)
        self.assert_rejected(old, new)

    def test_duplicate_display_or_structural_identity_is_rejected(self):
        first = declaration("Example", "one")
        old = report(first)
        self.assert_rejected(old, report(first, declaration("Example", "two", display=first["display_name"])))
        second = deepcopy(first)
        second["display_name"] = "misleading.other"
        self.assert_rejected(old, report(first, second))

    def test_dropped_raw_or_generated_declaration_is_rejected(self):
        generated = declaration("Example", "_private", 0)
        generated["is_internal"] = True
        old = report(declaration("Example", "visible"), generated)
        self.assert_rejected(old, report(old["declarations"][0]))

    def test_incomplete_raw_inventory_and_index_only_report_cannot_pass(self):
        old = report(declaration("Example", "value"))
        new = deepcopy(old)
        new["raw_declaration_count"] += 1
        self.assert_rejected(old, new)
        new = deepcopy(old)
        new["mode"] = "index"
        self.assert_rejected(old, new)


class MigrationBaselineTests(unittest.TestCase):
    def test_complete_baseline_binds_scope_and_occurrences(self):
        value = baseline()
        self.assertEqual(migration.baseline_errors(value), [])
        evidence = migration.coverage(value)
        self.assertEqual(evidence["original_occurrences"], sorted(value["migration"]["original_index"]["occurrences"]))
        self.assertEqual(evidence["excluded_files"], {"Notes.lean": "b" * 64})

    def test_baseline_tampering_is_rejected(self):
        value = baseline()
        value["project_scope"] = "all"
        self.assertTrue(migration.baseline_errors(value))

    def test_resealed_module_scope_or_occurrence_ownership_disagreement_is_rejected(self):
        for target in ("module", "occurrence"):
            with self.subTest(target=target):
                value = baseline()
                index = value["migration"]["original_index"]
                if target == "module":
                    index["modules"]["Fixture"]["path"] = "Notes.lean"
                else:
                    next(iter(index["occurrences"].values()))["path"] = "Notes.lean"
                value = migration.seal(value)
                self.assertTrue(migration.baseline_errors(value))

    def test_resealing_outer_baseline_cannot_change_scope_or_index_bindings(self):
        for field in ("scope_mode", "excluded_files", "index_scope", "index_environment", "index_source"):
            with self.subTest(field=field):
                value = baseline()
                info = value["migration"]
                if field == "scope_mode":
                    info["scope"]["mode"] = "all"
                    info["scope"] = migration.seal(info["scope"])
                elif field == "excluded_files":
                    info["excluded_files"] = {}
                    info["scope"]["excluded_files"] = {}
                    info["scope"] = migration.seal(info["scope"])
                    info["original_index"]["scope_sha256"] = info["scope"]["sha256"]
                elif field == "index_scope":
                    info["original_index"]["scope_sha256"] = "wrong"
                elif field == "index_environment":
                    info["original_index"]["environment"] = {"config": {"lean-toolchain": "changed"}}
                else:
                    info["original_index"]["source_sha256"] = "wrong"
                index = info["original_index"]
                index["index_sha256"] = migration.digest({k: v for k, v in index.items() if k != "index_sha256"})
                self.assertTrue(migration.baseline_errors(migration.seal(value)))

    def test_resealed_baseline_cannot_redirect_target_root(self):
        value = baseline()
        value["project_root"] = "/different/target"
        self.assertTrue(migration.baseline_errors(migration.seal(value)))

    def test_foreign_root_and_unbound_lookalike_worktree_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            value = baseline(root)
            lookalike = root / "target/.worktrees/bump-worker"
            lookalike.mkdir(parents=True)
            (lookalike / ".git").write_text("gitdir: /foreign/repository/worktrees/worker\n")
            with patch("unity.bump_contract._git", side_effect=["/foreign/common", "/bound/common"]):
                with self.assertRaisesRegex(ValueError, "sealed target or an owned Bump worktree"):
                    migration.require_inputs(lookalike, value)
            with self.assertRaisesRegex(ValueError, "sealed target or an owned Bump worktree"):
                migration.require_inputs(root / "unrelated", value)

    def test_original_source_and_excluded_bytes_are_checked_before_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contents = {"Fixture.lean": "def original := 1\n", "Notes.lean": "-- excluded\n"}
            hashes = {name: hashlib.sha256(data.encode()).hexdigest() for name, data in contents.items()}
            value = baseline(root, hashes)
            for tree in ("source", "original", "target"):
                (root / tree).mkdir()
                for name, data in contents.items():
                    (root / tree / name).write_text(data)
            with patch("unity.bump_contract._dependencies", return_value={}):
                migration.require_inputs(root / "target", value)
                (root / "source/Fixture.lean").write_text("def original := 2\n")
                with self.assertRaisesRegex(ValueError, "original migration source changed"):
                    migration.require_inputs(root / "target", value)
                (root / "source/Fixture.lean").write_text(contents["Fixture.lean"])
                (root / "target/Notes.lean").write_text("-- changed excluded file\n")
                with self.assertRaisesRegex(ValueError, "excluded migration input changed"):
                    migration.require_inputs(root / "target", value)

    def test_added_files_in_preserved_source_or_original_are_not_silently_ignored(self):
        for preserved in ("source", "original"):
            with self.subTest(preserved=preserved), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                contents = {"Fixture.lean": "def original := 1\n", "Notes.lean": "-- excluded\n"}
                hashes = {name: hashlib.sha256(data.encode()).hexdigest() for name, data in contents.items()}
                value = baseline(root, hashes)
                for tree in ("source", "original", "target"):
                    (root / tree).mkdir()
                    for name, data in contents.items():
                        (root / tree / name).write_text(data)
                (root / preserved / "Added.lean").write_text("def newInput := 1\n")
                with patch("unity.bump_contract._dependencies", return_value={}):
                    with self.assertRaises(ValueError):
                        migration.require_inputs(root / "target", value)


def migration_contract(value):
    return migration.seal({"version": 3, "migration_policy": 1, "project_baseline": value,
        "bindings": {}, "targets": {}, "adopted_outputs": {},
        "requirements": [{"id": "requirement-" + key, "anchor_ids": ["anchor-" + key]}
                         for key in value["migration"]["original_index"]["occurrences"]],
        "migration_correspondences": {key: {"module": row["module"],
            "declaration": row["display_name"], "name_ast": row["name_ast"]}
            for key, row in value["migration"]["original_index"]["occurrences"].items()}})


def final_snapshot(contract):
    value = contract["project_baseline"]
    expected = sorted(value["migration"]["original_index"]["occurrences"])
    return {"passed": True, "project_verification": migration.coverage(value),
        "compiled_receipt": {"artifact_id": "artifact-compiled", "sha256": "c" * 64},
        "migration_review": {"policy": migration.POLICY, "native_complete": True,
            "original_occurrences": expected, "verified_occurrences": expected,
            "occurrence_declarations": {key: row["declaration"] for key, row in
                                        contract["migration_correspondences"].items()},
            "scope_sha256": value["migration"]["scope"]["sha256"],
            "correspondences_sha256": migration.digest(contract["migration_correspondences"]),
            "native_reports": [{"module": "Fixture", "side": side,
                                "artifact_id": "artifact-" + side, "sha256": "a" * 64}
                               for side in ("original", "target")]}}


class MigrationSnapshotTests(unittest.TestCase):
    def test_complete_saved_native_evidence_bindings_are_required(self):
        contract = migration_contract(baseline())
        state = {"formalization": {"contract": contract}}
        migration.validate_native_snapshot(state, final_snapshot(contract))
        for field, value in (("native_complete", False), ("verified_occurrences", []),
                             ("original_occurrences", []), ("scope_sha256", "wrong"),
                             ("correspondences_sha256", "wrong"), ("native_reports", []),
                             ("occurrence_declarations", {})):
            with self.subTest(field=field):
                snapshot = final_snapshot(contract)
                snapshot["migration_review"][field] = value
                with self.assertRaises(ValueError):
                    migration.validate_native_snapshot(state, snapshot)

    def test_compiled_receipt_and_exact_scope_coverage_cannot_be_omitted(self):
        contract = migration_contract(baseline())
        state = {"formalization": {"contract": contract}}
        for field in ("compiled_receipt", "project_verification"):
            snapshot = final_snapshot(contract)
            snapshot.pop(field)
            with self.subTest(field=field), self.assertRaises(ValueError):
                migration.validate_native_snapshot(state, snapshot)

    def test_changed_correspondence_invalidates_saved_native_review(self):
        contract = migration_contract(baseline())
        snapshot = final_snapshot(contract)
        changed = deepcopy(contract)
        next(iter(changed["migration_correspondences"].values()))["declaration"] = "Example.replacement"
        with self.assertRaises(ValueError):
            migration.validate_native_snapshot({"formalization": {"contract": changed}}, snapshot)

    def test_omitting_an_original_requirement_cannot_accept_native_review(self):
        contract = migration_contract(baseline())
        snapshot = final_snapshot(contract)
        changed = deepcopy(contract)
        changed["requirements"] = []
        with self.assertRaises(ValueError):
            migration.validate_native_snapshot({"formalization": {"contract": changed}}, snapshot)

    def test_compiler_progress_receipt_never_satisfies_native_acceptance(self):
        state = {"formalization": {"contract": migration_contract(baseline())}}
        with self.assertRaises(ValueError):
            migration.validate_native_snapshot(state, {"passed": True, "status": "passed",
                "mode": "diagnostic_repair", "native_pending": True, "verified_targets": {}, "verified_tasks": []})


class DeclarationRepairProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        value = baseline(self.root, rows=[declaration("Example", "target"), declaration("Example", "other")])
        self.contract = migration_contract(value)
        ids = {row["display_name"]: key for key, row in value["migration"]["original_index"]["occurrences"].items()}
        self.target_id, self.other_id = ids["Example.target"], ids["Example.other"]
        self.task = {"task_id": "repair-target", "migration": {"kind": "declaration",
            "original_occurrences": [self.target_id]}, "requirement_ids": ["preserve-" + self.target_id]}
        self.candidate = {"changed_paths": ["Fixture.lean"],
            "outputs": [{"declaration": "Example.target", "file": "Fixture.lean"}]}
        self.other_error = {"path": "Fixture.lean", "occurrence_ids": [self.other_id],
                            "severity": "error", "message": "other declaration still fails"}
        self.current = {"errors": [self.other_error], "unmapped_error_count": 0}
        folder = self.root / ".unity/bump"
        folder.mkdir(parents=True)
        (folder / "diagnostics.json").write_text(json.dumps({"main_sha": "a" * 40,
            "errors": [self.other_error]}))
        for target, values in (
            ("unity.bump_migration.require_inputs", {}),
            ("unity.bump_migration._patch_issues", {"return_value": []}),
            ("unity.bump_contract._git", {"return_value": "a" * 40}),
            ("unity.bump_contract.policy_hash", {"return_value": "b" * 64}),
            ("unity.bump_contract.adopted_output_records", {"return_value": {}}),
            ("unity.bump_contract._seal_contract", {"side_effect": migration.seal}),
        ):
            mocked = patch(target, **values)
            mocked.start()
            self.addCleanup(mocked.stop)

    def verify(self, current=None, returncode=1):
        with patch("unity.bump_diagnostics.diagnostics_from_output", return_value=current or self.current):
            return migration.verify_candidate(self.root, self.contract, self.task, self.candidate,
                formal_tasks=[self.task], build={"returncode": returncode, "output": "fixture diagnostics"},
                layout={"modules": {"Fixture.lean": "Fixture"}})

    def test_fixed_declaration_can_integrate_while_same_file_neighbor_still_fails(self):
        checked = self.verify()
        self.assertEqual(checked["status"], "passed", checked)
        self.assertEqual(checked["mode"], "diagnostic_repair")
        self.assertTrue(checked["native_pending"])
        self.assertEqual(checked["verified_targets"], {})
        self.assertEqual(checked["verified_tasks"], [])
        self.assertEqual(checked["original_occurrences"], [self.target_id])
        with self.assertRaises(ValueError):
            migration.validate_native_snapshot({"formalization": {"contract": self.contract}}, checked)

    def test_assigned_declaration_still_failing_cannot_integrate(self):
        current = {"errors": [{**self.other_error, "occurrence_ids": [self.target_id]}]}
        checked = self.verify(current)
        self.assertEqual(checked["status"], "failed")
        self.assertIn("assigned declaration still has compiler errors", checked["issues"])

    def test_new_unrelated_error_is_rejected_even_when_assigned_error_is_gone(self):
        current = {"errors": [{**self.other_error, "message": "new unrelated failure"}]}
        checked = self.verify(current)
        self.assertEqual(checked["status"], "failed")
        self.assertTrue(any("unrelated compiler error" in issue for issue in checked["issues"]))

    def test_failed_compiler_with_no_mapped_diagnostics_is_not_progress(self):
        checked = self.verify({"errors": [], "unmapped_error_count": 0})
        self.assertEqual(checked["status"], "failed")
        self.assertIn("failed compiler execution did not yield mapped source diagnostics", checked["issues"])

    def test_other_preexisting_source_command_error_does_not_block_assigned_command_progress(self):
        self.task["migration"] = {"kind": "command", "original_ids": [], "path": "Fixture.lean",
            "command_line": 2, "original_ranges": [{"start_line": 2, "end_line": 2}]}
        self.task["requirement_ids"] = []
        self.candidate["outputs"] = []
        sibling = {"path": "Fixture.lean", "line": 7, "original_line": 7, "occurrence_ids": [],
                   "severity": "error", "message": "preexisting unrelated source command failure"}
        (self.root / ".unity/bump/diagnostics.json").write_text(json.dumps({"main_sha": "a" * 40,
            "errors": [sibling]}))
        checked = self.verify({"errors": [sibling], "unmapped_error_count": 0})
        self.assertEqual(checked["status"], "passed", checked)
        self.assertTrue(checked["native_pending"])
        checked = self.verify({"errors": [{**sibling, "line": 2, "original_line": 2}],
            "unmapped_error_count": 0})
        self.assertEqual(checked["status"], "failed")
        self.assertIn("assigned source command still has compiler errors", checked["issues"])


class DeclarationAssignmentBoundaryTests(unittest.TestCase):
    def refinement(self, rows):
        value = baseline(rows=rows)
        contract = migration_contract(value)
        keys = sorted(value["migration"]["original_index"]["occurrences"])
        previous = {key: {"task_id": key, "migration": {"kind": "declaration",
            "original_ids": [key], "path": "Fixture.lean", "module": "Fixture"}} for key in keys}
        state = {"formalization": {"contract": contract, "requirements": contract["requirements"]},
                 "formal_tasks": {"group": {"task_id": "group",
                     "requirement_ids": ["requirement-" + key for key in keys]}}}
        migration.refine(state, {"replacements": [{"old_ids": keys, "new_ids": ["group"],
            "reason": "one inseparable original source construction"}]}, previous_tasks=previous)
        return state["formal_tasks"]["group"]["migration"]

    def test_refinement_cannot_turn_unrelated_same_file_declarations_into_mutual_group(self):
        value = baseline(rows=[declaration("Example", "first"), declaration("Example", "second")])
        contract = migration_contract(value)
        keys = sorted(value["migration"]["original_index"]["occurrences"])
        previous = {key: {"task_id": key, "migration": {"kind": "declaration",
            "original_ids": [key], "path": "Fixture.lean", "module": "Fixture"}} for key in keys}
        state = {"formalization": {"contract": contract, "requirements": contract["requirements"]},
                 "formal_tasks": {"whole-module": {"task_id": "whole-module",
                     "requirement_ids": ["requirement-" + key for key in keys]}}}
        with self.assertRaises(ValueError):
            migration.refine(state, {"replacements": [{"old_ids": keys, "new_ids": ["whole-module"],
                "reason": "same source file"}]}, previous_tasks=previous)

    def test_new_support_file_does_not_bypass_provisional_no_new_hole_guard(self):
        row = declaration("Example", "target")
        row["range"] = {"start_line": 1, "end_line": 1, "start_column": 0, "end_column": 20}
        value = baseline(rows=[row])
        contract = migration_contract(value)
        key = next(iter(value["migration"]["original_index"]["occurrences"]))
        task = {"task_id": key, "migration": {"kind": "declaration", "original_ids": [key]}}
        candidate = {"base_main_sha": "a" * 40, "commit_sha": "b" * 40, "changed_paths": ["Helper.lean"]}
        for body in ("theorem helper : True := by sorry\n", "axiom helper : False\n"):
            with self.subTest(body=body), patch("unity.bump_migration._git_text", return_value=body):
                issues = migration._patch_issues(Path("/fixture/target"), contract, task, candidate)
                self.assertTrue(issues, "New files must not skip the same candidate trust guard as existing files")

    def test_genuine_original_mutual_component_can_remain_one_task(self):
        first, second = declaration("Example", "first"), declaration("Example", "second")
        first["range"] = {"start_line": 2, "end_line": 2, "start_column": 0, "end_column": 20}
        second["range"] = {"start_line": 3, "end_line": 3, "start_column": 0, "end_column": 20}
        first["dependencies"] = [{"module": "Fixture", "name_ast": second["name_ast"]}]
        second["dependencies"] = [{"module": "Fixture", "name_ast": first["name_ast"]}]
        grouped = self.refinement([first, second])
        self.assertEqual(len(grouped["original_ids"]), 2)
        self.assertEqual(grouped["path"], "Fixture.lean")
        self.assertEqual(grouped["kind"], "mutual")

    def test_exact_native_source_range_family_remains_one_declaration_task(self):
        owner, generated = declaration("Example", "owner"), declaration("Example", "owner", "constructor")
        owner["range"] = {"start_line": 2, "end_line": 4, "start_column": 0, "end_column": 20}
        generated["range"] = deepcopy(owner["range"])
        generated["is_internal"] = True
        grouped = self.refinement([owner, generated])
        self.assertEqual(grouped["kind"], "declaration")
        self.assertEqual(len(grouped["original_ids"]), 2)
        self.assertEqual(grouped["original_ranges"], [owner["range"], owner["range"]])

    def test_connected_unranged_generated_occurrence_can_join_its_source_owner(self):
        owner, generated = declaration("Example", "owner"), declaration("Example", "_generated", 0)
        owner["range"] = {"start_line": 2, "end_line": 4, "start_column": 0, "end_column": 20}
        generated["is_internal"] = True
        generated["dependencies"] = [{"module": "Fixture", "name_ast": owner["name_ast"]}]
        grouped = self.refinement([owner, generated])
        self.assertEqual(grouped["kind"], "declaration")
        self.assertEqual(len(grouped["original_ids"]), 2)
        self.assertEqual(grouped["original_ranges"], [owner["range"]])

    def test_unrelated_unranged_generated_occurrence_cannot_join_source_owner(self):
        owner, generated = declaration("Example", "owner"), declaration("Example", "_generated", 0)
        owner["range"] = {"start_line": 2, "end_line": 4, "start_column": 0, "end_column": 20}
        generated["is_internal"] = True
        with self.assertRaises(ValueError):
            self.refinement([owner, generated])

    def test_new_support_file_comment_and_string_words_are_not_proof_holes(self):
        row = declaration("Example", "target")
        row["range"] = {"start_line": 1, "end_line": 1, "start_column": 0, "end_column": 20}
        value = baseline(rows=[row])
        contract = migration_contract(value)
        key = next(iter(value["migration"]["original_index"]["occurrences"]))
        task = {"task_id": key, "migration": {"kind": "declaration", "original_ids": [key]}}
        candidate = {"base_main_sha": "a" * 40, "commit_sha": "b" * 40, "changed_paths": ["Helper.lean"]}
        body = '-- sorry admit axiom\n/- axiom /- sorry -/ admit -/\ndef helper : String := "sorry admit axiom"\n'
        with patch("unity.bump_migration._git_text", return_value=body):
            self.assertEqual(migration._patch_issues(Path("/fixture/target"), contract, task, candidate), [])


class DeclarationColumnBoundaryTests(unittest.TestCase):
    def checked(self, original, before, after, span):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_root = root / "original"
            original_root.mkdir()
            (original_root / "Fixture.lean").write_text(original)
            row = declaration("Example", "target")
            row["range"] = span
            value = baseline(root, rows=[row])
            contract = migration_contract(value)
            key = next(iter(value["migration"]["original_index"]["occurrences"]))
            task = {"task_id": key, "migration": {"kind": "declaration", "original_ids": [key]}}
            candidate = {"base_main_sha": "a" * 40, "commit_sha": "b" * 40,
                         "changed_paths": ["Fixture.lean"]}
            with patch("unity.bump_migration._git_text", side_effect=[before, after]):
                return migration._patch_issues(root / "target", contract, task, candidate)

    def test_only_assigned_declaration_can_change_on_shared_line(self):
        first = "def first : Nat := 1"
        original = first + "   def second : Nat := 2\n"
        span = {"start_line": 1, "start_column": 0, "end_line": 1, "end_column": len(first)}
        self.assertEqual(self.checked(original, original, original.replace(":= 1", ":= 3"), span), [])
        self.assertTrue(self.checked(original, original, original.replace(":= 2", ":= 3"), span))
        self.assertTrue(self.checked(original, original, original.replace("Nat", "Int"), span))

    def test_unicode_columns_are_characters_not_utf8_bytes(self):
        first, second = "def αβγ : Nat := 1", "def δ : Nat := 2"
        original = first + "   " + second + "\n"
        span = {"start_line": 1, "start_column": len(first) + 3,
                "end_line": 1, "end_column": len(original) - 1}
        self.assertEqual(self.checked(original, original, original.replace(":= 2", ":= 3"), span), [])
        self.assertTrue(self.checked(original, original, original.replace(":= 1", ":= 3"), span))

    def test_prior_sibling_merge_can_shift_assigned_lines(self):
        original = "def sibling : Nat := 0\n\ndef target : Nat := 1\n"
        before = "def sibling : Nat := by\n  exact 0\n\ndef target : Nat := 1\n"
        span = {"start_line": 3, "start_column": 0, "end_line": 3,
                "end_column": len("def target : Nat := 1")}
        self.assertEqual(self.checked(original, before, before.replace(":= 1", ":= 2"), span), [])
        self.assertTrue(self.checked(original, before, before.replace("exact 0", "exact 2"), span))

    def test_prior_sibling_merge_can_shift_assigned_columns(self):
        first, second = "def first : Nat := 1", "def second : Nat := 2"
        original = first + "   " + second + "\n"
        before = original.replace(":= 1", ":= (0 + 1)")
        span = {"start_line": 1, "start_column": len(first) + 3,
                "end_line": 1, "end_column": len(original) - 1}
        self.assertEqual(self.checked(original, before, before.replace(":= 2", ":= 3"), span), [])
        self.assertTrue(self.checked(original, before, before.replace("0 + 1", "0 + 4"), span))

    def test_ambiguous_replacement_crossing_original_range_boundary_is_rejected(self):
        original = "abcdef\n"
        span = {"start_line": 1, "start_column": 0, "end_line": 1, "end_column": 3}
        self.assertTrue(self.checked(original, "aZZZf\n", "aZQZf\n", span))

    def test_insertion_at_neighboring_declaration_boundary_is_rejected(self):
        original = "def first : Nat := 1   def second : Nat := 2\n"
        span = {"start_line": 1, "start_column": 0, "end_line": 1,
                "end_column": len("def first : Nat := 1")}
        self.assertTrue(self.checked(original, original,
            original.replace("1   def", "1 + 3   def"), span))


if __name__ == "__main__":
    unittest.main()
