"""Offline Optimize evidence tests: fake compiler, no model/toolchain evaluation."""
from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from unity import optimize_metrics as m, optimize_verification as v


class MetricsAndVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="optimize-evidence-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "--quiet")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@localhost")
        self.write(".gitignore", ".unity/\n.lake/\n.env\n")
        self.write("lean-toolchain", "leanprover/lean4:v4.34.1\n")
        self.write("lakefile.toml", 'name = "Example"\n')
        self.write("Example.lean", "theorem first : True := by\n  have h : True := by trivial\n  exact h\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "baseline")
        self.scope = {"files": ["Example.lean"], "targets": "Universe", "modules": ["Example"]}
        self.install_metric("length")
        self.metric = m.load_metric(self.root, "length")
        self.method = {"description": "Fixture explicitly counted tactic invocations", "direction": "lower"}
        self.calls = []
        self.mock = patch("unity.optimize_jobs.run", side_effect=self.fake_build)
        self.mock.start()
        self.addCleanup(self.mock.stop)
        discover = patch("unity.optimize_workspace.discover", return_value={"modules": {"Example.lean": "Example"}, "unmatched": [], "issues": []})
        discover.start()
        self.addCleanup(discover.stop)

    def git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def install_metric(self, name):
        self.write(".unity/metrics/" + name + ".md", (Path(m.__file__).parent / "defaults" / "metrics" / (name + ".md")).read_text())

    def fake_build(self, root, command, **kwargs):
        self.calls.append(command)
        self.assertNotIn("--version", command)
        return subprocess.CompletedProcess(command, 0, "✔ Built Example\n", "")

    def measurement(self, value, *, identity=None, metric=None, subject="first", kind="agent_judged", method=None):
        return m.record_measurement(metric or self.metric, subject=subject, raw_value=value,
                                    source_identity=identity or v.file_identity(self.root, scope=self.scope),
                                    method=method or self.method, evidence={"reason": "Explicit offline fixture count"}, evidence_kind=kind)

    def baseline(self, value=3, *, metric=None):
        metric = metric or self.metric
        result = v.capture_baseline(self.root, metric=metric, scope=self.scope)
        result["measurements"] = {"first": self.measurement(value, metric=metric, identity=result["identity"])}
        result["sha256"] = m.digest({key: value for key, value in result.items() if key != "sha256"})
        return result

    def candidate(self, baseline, value=1, **kwargs):
        self.write("Example.lean", "theorem first : True := by trivial\n")
        metric = kwargs.pop("metric", self.metric)
        measurements = {"first": self.measurement(value, metric=metric)}
        return v.verify_final(self.root, baseline=baseline, metric=metric, scope=self.scope, measurements=measurements, **kwargs)

    def completed(self):
        baseline = self.baseline()
        snapshot = self.candidate(baseline)
        self.assertTrue(snapshot["passed"], snapshot["issues"])
        state = {"phase": "complete", "run_id": "opt-test", "baseline": baseline,
                 "baseline_sealed": True,
                 "tasks": {"selected": {"status": "complete", "chunk_ids": ["first"], "files": ["Example.lean"]}},
                 "metric": self.metric, "scope": self.scope, "main_sha": snapshot["main_sha"],
                 "config": {"primary": "Primary"},
                 "review": {"reviewer": "Primary", "snapshot": snapshot,
                            "verdict": {"author": "Primary", "verdict": "approved", "snapshot_id": snapshot["snapshot_id"], "snapshot_sha256": snapshot["sha256"]}}}
        return state

    def test_builtin_is_frozen_prose_not_executable_scorer(self):
        self.assertEqual(self.metric["builtin"], "length")
        self.assertEqual(self.metric["units"], "tactic invocations")
        self.assertEqual(self.metric["helpers"], {})
        self.assertIn("Markdown specification", self.metric["qualification"])

    def test_overridden_builtin_name_does_not_inherit_wrong_metric(self):
        self.write(".unity/metrics/length.md", "# length\n## Prompt\nMaximize total characters.\n")
        metric = m.load_metric(self.root, "length")
        self.assertIsNone(metric["builtin"])
        self.assertIsNone(metric["direction"])

    def test_helper_file_bytes_are_bound_and_not_run(self):
        self.write(".unity/metrics/custom.md", "# custom\n## Score function\n`score.py`\n## Metric function\n[measure](measure.py)\n")
        self.write(".unity/metrics/score.py", "raise Exception('must not run')\n")
        self.write(".unity/metrics/measure.py", "raise Exception('must not run')\n")
        old = m.load_metric(self.root, "custom")
        self.assertEqual(set(old["helpers"]), {"score.py", "measure.py"})
        self.write(".unity/metrics/measure.py", "changed\n")
        self.assertNotEqual(old["sha256"], m.load_metric(self.root, "custom")["sha256"])

    def test_missing_helper_fails_not_silently_prose(self):
        self.write(".unity/metrics/custom.md", "# custom\n## Metric function\nmissing.py\n")
        with self.assertRaisesRegex(ValueError, "helper missing"):
            m.load_metric(self.root, "custom")

    def test_escaping_helper_is_rejected(self):
        self.write(".unity/metrics/custom.md", "# custom\n## Metric function\n../outside.py\n")
        with self.assertRaisesRegex(ValueError, "inside"):
            m.load_metric(self.root, "custom")

    def test_numeric_comparison_keeps_raw_and_improvement_separate(self):
        result = m.compare_measurements(self.measurement(4), self.measurement(2))
        self.assertTrue(result["improved"])
        self.assertEqual((result["before"], result["after"], result["delta"], result["improvement_percent"]), (4, 2, 2, 50))
        self.assertEqual(result["evidence_kind"], "agent_judged")

    def test_zero_denominator_not_invented_percentage(self):
        result = m.compare_measurements(self.measurement(0), self.measurement(0))
        self.assertFalse(result["improved"])
        self.assertIsNone(result["improvement_percent"])

    def test_modularity_zero_denominator_is_not_fabricated_zero(self):
        self.install_metric("modularity")
        metric = m.load_metric(self.root, "modularity")
        row = m.record_measurement(metric, subject="first", raw_value="undefined",
                                   source_identity=v.file_identity(self.root, self.scope),
                                   method={"description": "Count typed haves / tactics", "direction": "higher"},
                                   evidence={"zero_denominator": True, "reason": "Term proof contains no tactic block"})
        self.assertEqual(row["raw_value"], "undefined")
        with self.assertRaisesRegex(ValueError, "judgment"):
            m.compare_measurements(row, row)

    def test_builtin_units_cannot_be_silently_replaced_with_lines(self):
        with self.assertRaisesRegex(ValueError, "units"):
            m.record_measurement(self.metric, subject="first", raw_value=3,
                                 source_identity=v.file_identity(self.root, self.scope),
                                 method=self.method, evidence="Counted lines", units="lines")

    def test_different_method_cannot_claim_improvement(self):
        with self.assertRaisesRegex(ValueError, "not comparable"):
            m.compare_measurements(self.measurement(3), self.measurement(1, method={"description": "Different counting convention", "direction": "lower"}))

    def test_changed_source_invalidates_measurement(self):
        row = self.measurement(3)
        self.write("Example.lean", "theorem first : True := by trivial\n")
        self.assertFalse(m.validate_measurement_binding(row, self.metric, v.file_identity(self.root, scope=self.scope)))

    def test_measurement_rebind_requires_identical_bytes(self):
        old = v.file_identity(self.root, scope=self.scope)
        row = self.measurement(3, identity=old)
        self.git("commit", "--quiet", "--allow-empty", "-m", "metadata only")
        new = v.file_identity(self.root, scope=self.scope)
        rebound = m.rebind_measurement(row, self.metric, old, new)
        self.assertTrue(m.validate_measurement_binding(rebound, self.metric, new))
        self.assertEqual(rebound["origin"]["measurement_sha256"], row["sha256"])
        self.write("Example.lean", "theorem first : True := by trivial\n")
        with self.assertRaisesRegex(ValueError, "identical"):
            m.rebind_measurement(row, self.metric, old, v.file_identity(self.root, scope=self.scope))

    def test_qualitative_custom_metric_keeps_evidenced_judgment(self):
        self.write(".unity/metrics/style.md", "# style\n## Prompt\nPrefer clearer proof explanations.\n")
        metric = m.load_metric(self.root, "style")
        old = self.measurement("unclear", metric=metric)
        new = self.measurement("clearer", metric=metric)
        with self.assertRaisesRegex(ValueError, "judgment"):
            m.compare_measurements(old, new)
        comparison = m.compare_measurements(old, new, judgment={"improved": True, "reason": "Named local facts are explained"})
        self.assertTrue(comparison["improved"])
        self.assertIsNone(comparison["delta"])
        self.assertEqual(comparison["evidence_kind"], "agent_judged")

    def test_source_identity_does_not_run_compiler_or_read_env(self):
        self.write(".env", "PRIVATE_SECRET=do-not-read\n")
        before = len(self.calls)
        identity = v.file_identity(self.root, scope=self.scope)
        self.assertEqual(len(self.calls), before)
        self.assertNotIn(".env", identity["files"])

    def test_baseline_retains_original_measurement_after_final(self):
        baseline = self.baseline()
        saved = deepcopy(baseline)
        snapshot = self.candidate(baseline)
        self.assertEqual(baseline, saved)
        self.assertEqual(snapshot["comparisons"]["first"]["before"], 3)

    def test_build_runs_default_and_explicit_modules(self):
        self.baseline()
        self.assertEqual(self.calls, [["lake", "--rehash", "build"], ["lake", "--rehash", "build", "+Example"]])

    def test_build_does_not_invent_module_names_from_srcdir_paths(self):
        self.assertEqual(v.scope_build_commands({"files": ["src/Example.lean", "scratch/Draft.lean"]}),
                         [["lake", "--rehash", "build"]])

    def test_supplemental_failure_cannot_hide_behind_default_build_success(self):
        def fake(root, command, **kwargs):
            if "+Example" in command:
                return subprocess.CompletedProcess(command, 1, "Example.lean:1:0: error: failed selected module\n", "")
            return subprocess.CompletedProcess(command, 0, "default passed\n", "")
        with patch("unity.optimize_jobs.run", side_effect=fake):
            build = v.build_scope(self.root, scope=self.scope)
        self.assertFalse(build["passed"])
        self.assertEqual([row["passed"] for row in build["builds"]], [True, False])
        self.assertEqual(build["errors"][0]["message"], "failed selected module")

    def test_selected_srcdir_file_uses_actual_lake_module_mapping(self):
        self.write("src/Nested.lean", "theorem nested : True := by trivial\n")
        with patch("unity.optimize_workspace.discover", return_value={"modules": {"src/Nested.lean": "Nested"}, "unmatched": [], "issues": []}):
            build = v.build_scope(self.root, scope={"files": ["src/Nested.lean"]}, selected_files=["src/Nested.lean"])
        self.assertTrue(build["passed"])
        self.assertEqual(build["commands"][-1], ["lake", "--rehash", "build", "+Nested"])
        self.assertEqual(build["selected_modules"], {"src/Nested.lean": "Nested"})

    def test_unowned_selected_source_is_unchecked_not_default_build_success(self):
        self.write("Draft.lean", "theorem draft : True := by trivial\n")
        with patch("unity.optimize_workspace.discover", return_value={"modules": {}, "unmatched": ["Draft.lean"], "issues": []}):
            build = v.build_scope(self.root, scope={}, selected_files=["Draft.lean"])
        self.assertFalse(build["passed"])
        self.assertEqual(build["unchecked_files"], ["Draft.lean"])
        self.assertEqual(build["commands"], [["lake", "--rehash", "build"]])

    def test_missing_selected_source_cannot_be_reported_built(self):
        build = v.build_scope(self.root, scope={}, selected_files=["Missing.lean"])
        self.assertFalse(build["passed"])
        self.assertEqual(build["unchecked_files"], ["Missing.lean"])

    def test_changed_actual_build_directory_rejected_before_build(self):
        layout = {"modules": {"Example.lean": "Example"}, "build_dir": "generated", "unmatched": [], "issues": []}
        with patch("unity.optimize_workspace.discover", return_value=layout):
            with self.assertRaisesRegex(ValueError, "build directory differs"):
                v.build_scope(self.root, scope=self.scope, selected_files=["Example.lean"])
        self.assertEqual(self.calls, [])

    def test_actual_custom_build_directory_excluded_from_pure_identity(self):
        scope = {**self.scope, "build_dir": "generated"}
        layout = {"modules": {"Example.lean": "Example"}, "build_dir": "generated", "unmatched": [], "issues": []}
        before = v.file_identity(self.root, scope)
        def fake(root, command, **kwargs):
            self.write("generated/lib/Example.olean", "offline generated output")
            return subprocess.CompletedProcess(command, 0, "build passed\n", "")
        with patch("unity.optimize_workspace.discover", return_value=layout), patch("unity.optimize_jobs.run", side_effect=fake):
            build = v.build_scope(self.root, scope=scope, selected_files=["Example.lean"])
        self.assertTrue(build["passed"])
        self.assertEqual(v.file_identity(self.root, scope), before)
        self.assertNotIn("generated/lib/Example.olean", before["files"])

    def test_completion_partial_candidate_cannot_skip_unowned_source(self):
        self.install_metric("completion")
        metric = m.load_metric(self.root, "completion")
        baseline = self.baseline(2, metric=metric)
        with patch("unity.optimize_workspace.discover", return_value={"modules": {}, "unmatched": ["Example.lean"], "issues": []}):
            snapshot = v.verify_candidate(self.root, baseline=baseline, metric=metric, scope=self.scope,
                measurements={"first": self.measurement(1, metric=metric)})
        self.assertFalse(snapshot["passed"])
        self.assertIn("selected_files_unchecked", {row["kind"] for row in snapshot["issues"]})

    def test_completion_allows_broken_baseline(self):
        self.install_metric("completion")
        metric = m.load_metric(self.root, "completion")
        with patch("unity.optimize_jobs.run", return_value=subprocess.CompletedProcess([], 1, "Example.lean:1:0: error: unknown name\n", "")):
            baseline = self.baseline(1, metric=metric)
        self.assertFalse(baseline["build"]["passed"])
        self.assertEqual(len(baseline["build"]["errors"]), 1)

    def test_length_does_not_call_broken_baseline_success(self):
        with patch("unity.optimize_jobs.run", return_value=subprocess.CompletedProcess([], 1, "error: bad build\n", "")):
            with self.assertRaisesRegex(ValueError, "baseline build failed"):
                self.baseline()

    def test_no_new_hole_in_candidate(self):
        baseline = self.baseline()
        self.write("Example.lean", "theorem first : True := by sorry\n")
        result = v.verify_candidate(self.root, baseline=baseline, metric=self.metric, scope=self.scope,
                                    measurements={"first": self.measurement(1)})
        self.assertFalse(result["passed"])
        self.assertIn("introduced_sorry", {row["kind"] for row in result["issues"]})

    def test_deleting_or_weakening_claim_fails_audit(self):
        baseline = self.baseline()
        self.write("Example.lean", "theorem first : True ∨ False := by trivial\n")
        self.assertIn("changed_declaration_header", {row["kind"] for row in v.audit_sources(self.root, baseline)["issues"]})
        self.write("Example.lean", "-- deleted\n")
        self.assertIn("removed_declaration", {row["kind"] for row in v.audit_sources(self.root, baseline)["issues"]})

    def test_comments_and_string_hole_words_not_counted(self):
        self.write("Example.lean", 'theorem first : True := by trivial\n/- sorry /- axiom -/ -/\ndef s := "sorry"\n')
        baseline = self.baseline()
        self.assertEqual(v.audit_sources(self.root, baseline)["holes_remaining"], 0)

    def test_literal_whitespace_in_statement_is_not_normalized_away(self):
        self.write("Example.lean", 'theorem first : "a b" = "a b" := rfl\n')
        baseline = self.baseline()
        self.write("Example.lean", 'theorem first : "a  b" = "a  b" := rfl\n')
        self.assertFalse(v.audit_sources(self.root, baseline)["passed"])

    def test_where_substring_in_name_does_not_truncate_header(self):
        self.write("Example.lean", "theorem somewhere : True := by trivial\n")
        baseline = self.baseline()
        self.write("Example.lean", "theorem somewhere : True ∨ False := by trivial\n")
        self.assertFalse(v.audit_sources(self.root, baseline)["passed"])

    def test_completion_can_replace_axiom_by_proof_in_namespace(self):
        self.install_metric("completion")
        metric = m.load_metric(self.root, "completion")
        self.write("Example.lean", "namespace X\naxiom first : True\nend X\n")
        baseline = self.baseline(1, metric=metric)
        self.write("Example.lean", "namespace X\ntheorem first : True := by trivial\nend X\n")
        self.assertTrue(v.audit_sources(self.root, baseline)["passed"])

    def test_same_type_changed_definition_not_claimed_semantic_proof(self):
        self.write("Example.lean", "def value : Nat := 1\n")
        baseline = self.baseline()
        self.write("Example.lean", "def value : Nat := 2\n")
        audit = v.audit_sources(self.root, baseline)
        self.assertTrue(audit["passed"])
        self.assertIn("not elaborated type equality or semantic equivalence", audit["qualification"])

    def test_candidate_compares_to_current_not_only_baseline(self):
        baseline = self.baseline(5)
        current = {"identity": baseline["identity"], "measurements": {"first": self.measurement(2, identity=baseline["identity"])} }
        self.write("Example.lean", "theorem first : True := by trivial\n")
        result = v.verify_candidate(self.root, baseline=baseline, metric=self.metric, scope=self.scope,
                                    comparison=current, measurements={"first": self.measurement(3)})
        self.assertFalse(result["passed"])
        self.assertIn("metric_regression", {row["kind"] for row in result["issues"]})

    def test_noop_is_not_success(self):
        baseline = self.baseline()
        result = self.candidate(baseline, value=3)
        self.assertFalse(result["passed"])
        self.assertIn("no_improvement_found", {row["kind"] for row in result["issues"]})

    def test_changed_metric_helpers_or_definition_block_verification(self):
        baseline = self.baseline()
        self.write(".unity/metrics/length.md", self.metric["definition"] + "\nchanged\n")
        with self.assertRaisesRegex(ValueError, "helper bytes changed"):
            self.candidate(baseline)

    def test_successful_report_is_pure_and_primary_bound(self):
        state = self.completed()
        with patch.object(v, "_git", side_effect=AssertionError("must not run Git")), patch("unity.optimize_jobs.run", side_effect=AssertionError("must not run tools")):
            result = v.completion_report(state)
        self.assertEqual(result["status"], "accepted")
        state["review"]["verdict"]["author"] = "Other"
        with self.assertRaisesRegex(ValueError, "configured-primary"):
            v.completion_report(state)

    def test_stale_main_or_snapshot_cannot_be_accepted(self):
        state = self.completed()
        state["main_sha"] = "new-main"
        with self.assertRaises(ValueError):
            v.completion_report(state)
        state = self.completed()
        state["review"]["snapshot"]["measurements"]["first"]["raw_value"] = 0
        with self.assertRaises(ValueError):
            v.completion_report(state)

    def test_final_selected_coverage_must_match_active_tasks(self):
        state = self.completed()
        state["tasks"]["old-group"] = {"status": "grouped", "chunk_ids": ["old"], "files": ["OldGroup.lean"]}
        self.assertEqual(v.completion_report(state)["status"], "accepted")
        state["tasks"]["selected"]["files"].append("New.lean")
        with self.assertRaisesRegex(ValueError, "scope"):
            v.completion_report(state)

    def test_complete_phase_cannot_bypass_unsealed_baseline(self):
        state = self.completed()
        for value in (False, None, "true", 1):
            with self.subTest(baseline_sealed=value):
                state["baseline_sealed"] = value
                with self.assertRaisesRegex(ValueError, "sealed original baseline"):
                    v.completion_report(state)
        del state["baseline_sealed"]
        with self.assertRaisesRegex(ValueError, "sealed original baseline"):
            v.completion_report(state)

    def test_complete_phase_cannot_bypass_unresolved_active_task(self):
        state = self.completed()
        for status in ("pending", "running", "blocked", "exhausted", None):
            with self.subTest(task_status=status):
                state["tasks"]["selected"]["status"] = status
                with self.assertRaisesRegex(ValueError, "resolved active tasks"):
                    v.completion_report(state)
        state["tasks"]["selected"]["status"] = "no_improvement"
        self.assertEqual(v.completion_report(state)["status"], "accepted")

    def test_final_scores_must_cover_selected_task_subjects_exactly(self):
        state = self.completed()
        for selected in (["first", "omitted"], ["different"], []):
            with self.subTest(selected_subjects=selected):
                state["tasks"]["selected"]["chunk_ids"] = selected
                with self.assertRaisesRegex(ValueError, "every selected task subject"):
                    v.completion_report(state)
        state["tasks"] = {}
        with self.assertRaisesRegex(ValueError, "every selected task subject"):
            v.completion_report(state)

    def test_measurement_key_cannot_hide_a_different_subject(self):
        for field in ("baseline", "current"):
            with self.subTest(field=field):
                state = self.completed()
                snapshot = state["review"]["snapshot"]
                collection = state["baseline"] if field == "baseline" else snapshot
                row = collection["measurements"]["first"]
                row["subject"] = "not-the-selected-declaration"
                row["sha256"] = m.digest({key: value for key, value in row.items() if key != "sha256"})
                if field == "baseline":
                    state["baseline"]["sha256"] = m.digest({key: value for key, value in state["baseline"].items() if key != "sha256"})
                    snapshot["baseline_sha256"] = state["baseline"]["sha256"]
                snapshot["sha256"] = m.digest({key: value for key, value in snapshot.items() if key != "sha256"})
                state["review"]["verdict"]["snapshot_sha256"] = snapshot["sha256"]
                with self.assertRaisesRegex(ValueError, "every selected task subject"):
                    v.completion_report(state)

    def test_exhausted_stopped_noop_are_distinct_and_not_accepted(self):
        for phase, status in (("exhausted", "exhausted"), ("stopped", "stopped"), ("no_improvement", "no_improvement_found")):
            self.assertEqual(v.completion_report({"phase": phase})["status"], status)

    def test_completion_needs_zero_and_build_not_only_reduced_count(self):
        self.install_metric("completion")
        metric = m.load_metric(self.root, "completion")
        self.write("Example.lean", "theorem first : True := by sorry\n")
        baseline = self.baseline(2, metric=metric)
        result = self.candidate(baseline, value=1, metric=metric)
        self.assertFalse(result["passed"])
        self.assertIn("completion_not_zero", {row["kind"] for row in result["issues"]})

    def test_exact_final_report_artifact_is_written(self):
        result = v.persist_report(self.root, self.completed(), accepted=True)
        record = result["artifacts"]["final_report"]
        payload = (self.root / ".unity" / "artifacts" / "blobs" / record["sha256"]).read_bytes()
        import hashlib
        self.assertEqual(hashlib.sha256(payload).hexdigest(), record["sha256"])


if __name__ == "__main__":
    unittest.main()
