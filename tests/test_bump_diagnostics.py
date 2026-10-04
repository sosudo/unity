import tempfile
import json
import subprocess
from pathlib import Path
import unittest
from unittest.mock import patch

from unity import bump_diagnostics as diagnostics
from test_bump_inventory import fixture_index


class DiagnosticsTests(unittest.TestCase):
    def test_diagnostic_source_identity_ignores_only_sealed_custom_build_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "Fixture.lean").write_text("def first : Nat := 1\n")
            output = root / "generated/build"
            output.mkdir(parents=True)
            cache = output / "Fixture.olean"
            cache.write_bytes(b"before")
            before = diagnostics.diagnostics_from_output(root, fixture_index(), "", 0, build_dir="generated/build")
            cache.write_bytes(b"after build")
            after = diagnostics.diagnostics_from_output(root, fixture_index(), "", 0, build_dir="generated/build")
            self.assertEqual(before["source_sha256"], after["source_sha256"])
            self.assertEqual(set(after["source_files"]), {"Fixture.lean"})

    def test_multiple_declaration_errors_are_located(self):
        parsed = diagnostics.parse_diagnostics("Fixture.lean:1:3: error: first\nFixture.lean:3:2: error: second\nerror: build failed\n",
            Path("/fixture"), source_sha256="a" * 64, returncode=1)
        self.assertEqual(len(parsed["diagnostics"]), 2)
        self.assertEqual(parsed["unmapped_error_count"], 0)

    def test_build_failure_without_diagnostics_remains_explicit(self):
        parsed = diagnostics.parse_diagnostics("compiler exited", Path("/fixture"), source_sha256="a" * 64, returncode=1)
        self.assertEqual(parsed["unmapped_error_count"], 1)

    def test_inserted_lines_map_errors_to_original_declaration(self):
        original = "theorem first := 1\n\ntheorem second := 2\n"
        current = "-- extra\n-- extra\n" + original
        self.assertEqual(diagnostics.original_line(original, current, 5), 3)
        self.assertEqual(len(diagnostics.occurrence_ids_at(fixture_index(), "Fixture.lean", 3)), 1)

    def test_external_absolute_paths_stay_unmapped(self):
        parsed = diagnostics.parse_diagnostics("/elsewhere/A.lean:2:1: error: failure", Path("/fixture"), source_sha256="a" * 64, returncode=1)
        self.assertEqual(parsed["unmapped_error_count"], 1)

    def test_column_distinguishes_separate_native_ranges_on_same_line(self):
        index = fixture_index()
        first = next(key for key, row in index["occurrences"].items() if row["display_name"] == "first")
        second = next(key for key, row in index["occurrences"].items() if row["display_name"] == "second")
        index["occurrences"][first]["range"] = {"start_line": 1, "start_column": 0, "end_line": 1, "end_column": 20}
        index["occurrences"][second]["range"] = {"start_line": 1, "start_column": 22, "end_line": 1, "end_column": 43}
        self.assertEqual(diagnostics.occurrence_ids_at(index, "Fixture.lean", 1, 28), [second])
        index["occurrences"][first]["range"]["end_column"] = 22
        self.assertEqual(diagnostics.occurrence_ids_at(index, "Fixture.lean", 1, 22), [second])

    def import_record(self, report, returncode):
        with patch.object(diagnostics.bump_workspace, "_checked_files", return_value=["Fixture.lean"]), \
             patch.object(diagnostics.bump_workspace, "_executable", return_value=Path("/native-workspace")), \
             patch.object(diagnostics.bump_jobs, "run", return_value=subprocess.CompletedProcess([], returncode, json.dumps(report), "")):
            return diagnostics.scheduling_imports(Path("/fixture"), fixture_index())

    def test_current_valid_native_headers_replace_original_scheduling_edges(self):
        result = self.import_record({"imports": {"Fixture.lean": ["Init", "NewSupport"]}, "issues": []}, 0)
        self.assertTrue(result["current_imports_complete"])
        self.assertEqual(result["current_imports"]["Fixture"], ["Init", "NewSupport"])

    def test_parser_invalid_header_has_explicit_scheduling_only_fallback(self):
        result = self.import_record({"imports": {}, "issues": ["invalid Lean import header in Fixture.lean: expected identifier"]}, 1)
        self.assertFalse(result["current_imports_complete"])
        self.assertEqual(result["scheduling_header_fallbacks"], ["Fixture.lean"])
        self.assertEqual(result["current_imports"]["Fixture"], [])

    def test_operational_import_failure_is_not_reclassified_as_parser_repair(self):
        for report in ({"imports": {}, "issues": ["could not read Lean import header Fixture.lean: permission denied"]},
                       {"imports": {}, "issues": []}):
            with self.subTest(report=report), self.assertRaises(ValueError):
                self.import_record(report, 1)
