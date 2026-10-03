import json
import tempfile
import unittest
import sys
import subprocess
from pathlib import Path
from unittest.mock import patch

from unity import artifacts, bump_diagnostics as diagnostics
from unity import bump_jobs
from unity.bump_inventory import digest
from tests.test_bump_inventory import fixture_index


def fixture_imports(index, *, edges=None, source_sha="1" * 64, source_files_sha="2" * 64, environment_sha="3" * 64):
    result = {"version": 1, "original_index_sha256": index["index_sha256"], "source_sha256": source_sha,
        "source_files_sha256": source_files_sha, "environment_sha256": environment_sha,
        "modules": {module: {"path": row["path"], "imports": (edges or {}).get(module, row["imports"]),
            "compiler_derived": True, "status": "complete", "unavailable_reason": None}
            for module, row in index["modules"].items()}}
    result["sha256"] = digest(result)
    return result


def fixture_diagnostics(index, *, compiled=(), errors=("B", "D")):
    rows = [{"id": "diag-" + module.lower() * 64, "path": module + ".lean", "line": 2, "column": 1,
             "severity": "error", "kind": "declaration", "message": "type mismatch", "message_truncated": False,
             "log_offset": 0, "log_bytes": 20} for module in errors]
    for row in rows:
        row["content_sha256"] = diagnostics.diagnostic_content_key(row, row["message"])
    result = {"version": 1, "kind": "bump-build-diagnostics-v1", "original_index_sha256": index["index_sha256"],
        "source_sha256": "1" * 64, "source_files_sha256": "2" * 64, "environment_sha256": "3" * 64,
        "artifact_ref": {"artifact_id": "artifact-" + "0" * 12, "sha256": "4" * 64},
        "source_unchanged": True, "complete_output": True, "modules": sorted(index["modules"]),
        "compiled_modules": sorted(compiled), "module_checks": [{"module": module, "passed": True} for module in compiled],
        "returncode": 1 if errors else 0, "passed": not errors, "diagnostics": rows,
        "target_imports": fixture_imports(index),
        "module_source_hashes": {module: row["source_sha256"] for module, row in index["modules"].items()}}
    result["snapshot_sha256"] = digest(result)
    return result


class BumpDiagnosticsTests(unittest.TestCase):
    def test_full_content_keys_ignore_log_offset_but_include_truncated_tails_and_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "build.log"
            def key(prefix, tail, detail):
                log.write_text(prefix + "error: B.lean:2:1: " + "x" * 2500 + tail + "\n" + detail + "\n")
                return diagnostics.parse_diagnostics(log, root, source_sha256="1" * 64, returncode=1)["diagnostics"][0]
            one = key("", "first", "expected Nat")
            shifted = key("ordinary progress\n", "first", "expected Nat")
            changed_tail = key("", "second", "expected Nat")
            changed_detail = key("", "first", "expected Int")
            self.assertNotEqual(one["id"], shifted["id"])
            self.assertEqual(one["content_sha256"], shifted["content_sha256"])
            self.assertEqual(one["message"], changed_tail["message"])
            self.assertNotEqual(one["content_sha256"], changed_tail["content_sha256"])
            self.assertNotEqual(one["content_sha256"], changed_detail["content_sha256"])

    def test_lake_command_and_info_records_do_not_change_previous_semantic_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "build.log"
            def parsed(metadata, expected="Int"):
                log.write_text("error: B.lean:2:1: type mismatch\n  value\nhas type\n  Nat\n"
                               "but is expected to have type\n  " + expected + "\n" + metadata)
                return diagnostics.parse_diagnostics(log, root, source_sha256="1" * 64,
                                                     returncode=1)["diagnostics"][0]["content_sha256"]
            baseline = parsed("")
            for metadata in (
                    "trace: .> /public/toolchain/bin/lean A.lean -o /public/build/A.olean\n",
                    "info: Fixture: replaying a cached independent job\n",
                    "trace: .> /other/public/toolchain/bin/lean C.lean\ninfo: completed another job\n"):
                with self.subTest(metadata=metadata):
                    self.assertEqual(parsed(metadata), baseline)
                    self.assertNotEqual(parsed(metadata, expected="String"), baseline)

    def test_lake_failed_target_summary_does_not_change_last_diagnostic_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "build.log"
            def parsed(summary):
                log.write_text("error: Fixture/B.lean:1:27: Unknown identifier `missingFixtureProof`\n" + summary)
                rows = diagnostics.parse_diagnostics(log, root, source_sha256="1" * 64,
                                                     returncode=1)["diagnostics"]
                self.assertEqual(len(rows), 1)
                return rows[0]["content_sha256"]
            bare = parsed("")
            aggregate = parsed("Some required targets logged failures:\n- Fixture.A\n- Fixture.B\nerror: build failed\n")
            single = parsed("Some required targets logged failures:\n- Fixture.B\nerror: build failed\n")
            self.assertEqual(aggregate, bare)
            self.assertEqual(single, bare)

    def test_registered_job_streams_complete_stdout_and_stderr_to_owned_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "compiler.log"
            with log.open("xb") as output:
                result = bump_jobs.run(root, [sys.executable, "-c",
                    "import os; os.write(1,b'first-error\\n'); os.write(1,b'x'*400000); os.write(2,b'last-error\\n')"],
                    cwd=root, output_stream=output, timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            data = log.read_bytes()
            self.assertTrue(data.startswith(b"first-error\n"))
            self.assertTrue(data.endswith(b"last-error\n"))
            self.assertGreater(len(data), 400000)

    def test_complete_log_keeps_early_error_beyond_old_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "build.log"
            log.write_bytes(b"error: B.lean:2:1: early real error\n" + b"ordinary progress\n" * 30000 + b"error: build failed\n")
            parsed = diagnostics.parse_diagnostics(log, root, source_sha256="1" * 64, returncode=1)
            self.assertEqual(len(parsed["diagnostics"]), 1)
            self.assertIn("early real error", parsed["diagnostics"][0]["message"])
            ref = diagnostics.store_log_artifact(root / "artifacts", log)
            self.assertEqual(artifacts.artifact_bytes(root / "artifacts", ref["artifact_id"]), log.read_bytes())

    def test_unclassified_failed_output_is_explicit_global_blocker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "build.log"
            log.write_text("unexpected nonstandard tool output\n")
            parsed = diagnostics.parse_diagnostics(log, root, source_sha256="1" * 64, returncode=1)
            self.assertEqual(parsed["diagnostics"][0]["kind"], "environment")

    def test_outside_path_not_attributed_to_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "build.log"
            log.write_text("error: /elsewhere/A.lean:1:0: failure\n")
            self.assertIsNone(diagnostics.parse_diagnostics(log, root, source_sha256="1" * 64, returncode=1)["diagnostics"][0]["path"])

    def test_failed_aggregate_rechecks_real_prerequisites_and_skips_cascades(self):
        index = fixture_index()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for module in index["modules"]:
                (root / (module + ".lean")).write_text("source")
            calls = []
            def build(root, modules, scope, diagnostics_path, diagnostic_imports):
                calls.append(modules)
                diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
                passed = modules == ["A"]
                errors = ("B", "D") if len(modules) > 1 else tuple(modules) if not passed else ()
                diagnostics_path.write_text("".join(f"error: {module}.lean:2:1: type mismatch\n" for module in errors)
                                            + ("error: build failed\n" if errors else "Build completed successfully.\n"))
                return {"passed": passed, "returncode": 0 if passed else 1,
                        "source_unchanged": True, "diagnostics_complete": True}
            identity = {"source_sha256": "1" * 64, "environment": {"lean": "fixed"}, "main_sha": "fixed"}
            with patch("unity.bump_contract.source_identity", return_value=identity), \
                 patch.object(diagnostics.project, "snapshot", return_value="2" * 64), \
                 patch.object(diagnostics, "capture_target_imports", return_value=fixture_imports(index, environment_sha=digest(identity["environment"]))), \
                 patch.object(diagnostics.project, "build", side_effect=build):
                receipt = diagnostics.collect_build_diagnostics(root, index, artifact_dir=root / "artifacts", scope={})
            self.assertEqual(receipt["compiled_modules"], ["A"])
            self.assertNotIn(["C"], calls)
            self.assertEqual(len(calls), 4)
            self.assertEqual({row["path"] for row in receipt["diagnostics"]}, {"B.lean", "D.lean"})
            self.assertEqual(len(receipt["additional_artifact_refs"]), 3)

    def test_fake_compiled_prerequisite_without_receipt_rejected(self):
        receipt = fixture_diagnostics(fixture_index(), compiled=["A"])
        receipt["module_checks"] = []
        receipt["snapshot_sha256"] = digest({k: v for k, v in receipt.items() if k != "snapshot_sha256"})
        with self.assertRaisesRegex(ValueError, "actual successful"):
            diagnostics.validate_diagnostics(receipt)

    def test_uncertain_cycle_preserves_diagnostics_without_inventing_success(self):
        index = fixture_index()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for module in index["modules"]:
                (root / (module + ".lean")).write_text("source")
            identity = {"source_sha256": "1" * 64, "environment": {"lean": "fixed"}, "main_sha": "fixed"}
            imports = fixture_imports(index, edges={"A": ["B"], "B": []},
                                      environment_sha=digest(identity["environment"]))
            imports["modules"]["B"].update(status="unavailable", unavailable_reason="header_syntax")
            imports["sha256"] = digest({k: v for k, v in imports.items() if k != "sha256"})
            calls = []
            def build(root, modules, scope, diagnostics_path, diagnostic_imports):
                calls.append(modules)
                diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
                passed = modules == ["D"]
                diagnostics_path.write_text("Build completed successfully.\n" if passed else
                                            "error: B.lean:1:0: unexpected token in import header\n")
                return {"passed": passed, "returncode": 0 if passed else 1,
                        "source_unchanged": True, "diagnostics_complete": True}
            with patch("unity.bump_contract.source_identity", return_value=identity), \
                 patch.object(diagnostics.project, "snapshot", return_value="2" * 64), \
                 patch.object(diagnostics, "capture_target_imports", return_value=imports), \
                 patch.object(diagnostics.project, "build", side_effect=build):
                receipt = diagnostics.collect_build_diagnostics(root, index, artifact_dir=root / "artifacts", scope={})
            self.assertEqual(calls, [["A", "B", "C", "D"], ["D"]])
            self.assertEqual(receipt["compiled_modules"], ["D"])
            self.assertEqual(receipt["diagnostics"][0]["kind"], "syntax")
            self.assertEqual(diagnostics.scheduling_graph(index, imports)["unresolved_import_cycles"], [["A", "B"]])

    def target_imports(self, index, headers, *, result=None):
        root = Path("/fixture")
        scope = {"mode": "all", "selected_modules": {key: row["path"] for key, row in index["modules"].items()},
                 "excluded_modules": {"Excluded": "Excluded.lean"}}
        def deps(_root, command):
            if result is not None:
                return result
            module = Path(command[-1]).stem
            return subprocess.CompletedProcess(command, 0, "".join(
                str(root / ".lake/build/lib/lean" / (dep + ".olean")) + "\n"
                for dep in headers[module]["imports"] if dep in index["modules"]), "")
        with patch.object(diagnostics.project, "source_files", return_value={}), \
             patch.object(diagnostics.project, "scope_errors", return_value=[]), \
             patch("unity.bump_migration_contract._native_executable", return_value=Path("/fixture-native")), \
             patch.object(diagnostics, "read_native_imports", side_effect=lambda _root, filename, **_kw: headers[Path(filename).stem]), \
             patch.object(diagnostics.project, "_run", side_effect=deps):
            return diagnostics.capture_target_imports(root, index, scope,
                {"source_sha256": "1" * 64, "environment": {}}, "2" * 64)

    def test_native_target_edges_change_on_refresh_and_keep_partial_headers(self):
        index = fixture_index()
        headers = {module: {"imports": [], "header_errors": False} for module in index["modules"]}
        first = self.target_imports(index, headers)
        headers["D"] = {"imports": ["B"], "header_errors": True}
        second = self.target_imports(index, headers)
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(second["modules"]["D"]["imports"], ["B"])
        self.assertEqual(second["modules"]["D"]["unavailable_reason"], "header_syntax")

    def test_proven_current_cycle_cannot_hide_inside_larger_unavailable_component(self):
        index = fixture_index()
        imports = fixture_imports(index, edges={"A": ["B", "C"], "B": ["A"], "C": ["A"]})
        imports["modules"]["C"].update(status="unavailable", unavailable_reason="header_syntax")
        imports["sha256"] = digest({k: v for k, v in imports.items() if k != "sha256"})
        graph = {module: row["imports"] for module, row in imports["modules"].items()}
        self.assertEqual(diagnostics._cycle_components(graph), [["A", "B", "C"]])
        for check in (lambda: diagnostics.validate_target_imports(imports, index),
                      lambda: diagnostics.scheduling_graph(index, imports)):
            with self.assertRaisesRegex(ValueError, "current_import_cycle: A, B$"):
                check()

    def test_excluded_native_header_is_rejected_even_when_malformed(self):
        headers = {module: {"imports": [], "header_errors": False} for module in fixture_index()["modules"]}
        headers["D"] = {"imports": ["Excluded"], "header_errors": True}
        with self.assertRaisesRegex(ValueError, "excluded"):
            self.target_imports(fixture_index(), headers)

    def test_unknown_import_can_be_unavailable_but_tool_failure_cannot(self):
        headers = {module: {"imports": [], "header_errors": False} for module in fixture_index()["modules"]}
        unavailable = self.target_imports(fixture_index(), headers,
            result=subprocess.CompletedProcess([], 1, "", "unknown module prefix 'Missing'"))
        self.assertEqual(unavailable["modules"]["D"]["unavailable_reason"], "missing_import")
        for code, message in ((127, "lean missing"), (-11, "unknown module prefix"), (1, "PANIC internal crash"),
                              (1, "PANIC\nunknown module prefix 'Missing'"), (1, "backend failed while parsing unknown module prefix")):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, "operationally"):
                self.target_imports(fixture_index(), headers, result=subprocess.CompletedProcess([], code, "", message))

    def test_unavailable_import_without_own_diagnostic_cannot_dispatch(self):
        receipt = fixture_diagnostics(fixture_index(), compiled=["A"])
        imports = receipt["target_imports"]
        imports["modules"]["A"].update(status="unavailable", unavailable_reason="header_syntax")
        imports["sha256"] = digest({k: v for k, v in imports.items() if k != "sha256"})
        receipt["snapshot_sha256"] = digest({k: v for k, v in receipt.items() if k != "snapshot_sha256"})
        with self.assertRaisesRegex(ValueError, "located failed-module"):
            diagnostics.validate_diagnostics(receipt)


if __name__ == "__main__":
    unittest.main()
