"""Offline build-plus-score regressions. No Lean, provider or service calls."""
from copy import deepcopy
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from unity import artifacts, optimize_jobs as jobs, optimize_metrics as m
from unity import optimize_scoring as scoring, optimize_verification as v

_REAL_JOB_RUN = jobs.run


class ScoringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="optimize-scoring-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.git(self.root, "init", "--quiet")
        self.git(self.root, "config", "user.name", "Test")
        self.git(self.root, "config", "user.email", "test@localhost")
        (self.root / ".gitignore").write_text(".unity/\n.lake/\n")
        (self.root / "Example.lean").write_text("theorem first : True := by\n  have h : True := by trivial\n  exact h\n")
        (self.root / "lean-toolchain").write_text("leanprover/lean4:v4.19.0\n")
        self.install_metric(self.root)
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "--quiet", "-m", "original")
        self.scope = {"files": ["Example.lean"], "targets": "first", "modules": ["Example"]}
        self.metric = m.load_metric(self.root, "length")
        self.method = {"description": "Explicit fixture tactic inventory", "direction": "lower"}
        self.build_calls, self.evaluations = [], []
        self.build_failure = False
        build = patch("unity.optimize_jobs.run", side_effect=self.fake_build)
        build.start()
        self.addCleanup(build.stop)
        discover = patch("unity.optimize_workspace.discover", return_value={
            "modules": {"Example.lean": "Example"}, "unmatched": [], "issues": []})
        discover.start()
        self.addCleanup(discover.stop)
        self.baseline = v.capture_baseline(self.root, metric=self.metric, scope=self.scope)
        self.baseline["measurements"] = {"first": m.record_measurement(self.metric,
            subject="first", raw_value=3, source_identity=self.baseline["identity"],
            method=self.method, evidence={"reason": "original fixture count"})}
        self.baseline = v._seal({key: value for key, value in self.baseline.items() if key != "sha256"})

    def git(self, root, *args):
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def install_metric(self, root):
        folder = root / ".unity" / "metrics"
        folder.mkdir(parents=True)
        shutil.copyfile(Path(m.__file__).parent / "defaults" / "metrics" / "length.md", folder / "length.md")

    def fake_build(self, root, args, **kwargs):
        self.build_calls.append((str(kwargs.get("cwd", root)), args))
        return subprocess.CompletedProcess(args, int(self.build_failure),
            "Example.lean:1:1: error: fixture failure\n" if self.build_failure else "Built Example\n", "")

    async def checked(self, function, *args, **kwargs):
        return function(*args, **kwargs)

    async def evaluate(self, request):
        self.assertEqual(jobs._held_build_locks.get(), (), "agent scoring must not hold the build lock")
        self.evaluations.append(deepcopy(request))
        value = 3 if "have h" in (Path(request["root"]) / "Example.lean").read_text() else 1
        return {"measurements": {subject: {"raw_value": value,
            "method": request["original_methods"].get(subject, self.method),
            "evidence_kind": "measured",  # A model claim cannot upgrade provenance.
            "source_sha256": "forged-worker-binding", "evidence": {"reason": "explicit fixture count"}}
            for subject in request["subjects"]}, "judgments": {}}

    def candidate(self):
        path = Path(self.temp.name) / "candidate"
        self.git(self.root, "worktree", "add", "--quiet", "--detach", str(path), "HEAD")
        self.install_metric(path)
        (path / "Example.lean").write_text("theorem first : True := by trivial\n")
        self.git(path, "add", "Example.lean")
        self.git(path, "commit", "--quiet", "-m", "candidate")
        return path

    async def check_candidate(self, root, **kwargs):
        return await v.verify_candidate_scored(root, before_root=self.root,
            baseline=self.baseline, metric=self.metric, scope=self.scope,
            task={"chunk_ids": ["first"], "files": ["Example.lean"]},
            evaluator=kwargs.pop("evaluator", self.evaluate), run_checked=self.checked,
            project_root=self.root, **kwargs)

    async def test_candidate_builds_and_scores_without_worker_measurements(self):
        candidate = self.candidate()
        baseline_bytes = json.dumps(self.baseline, sort_keys=True)
        result = await self.check_candidate(candidate)
        self.assertTrue(result["passed"], result["issues"])
        self.assertEqual(len(self.evaluations), 2)
        self.assertEqual(result["comparison_measurements"]["first"]["raw_value"], 3)
        self.assertEqual(result["measurements"]["first"]["raw_value"], 1)
        self.assertEqual(result["measurements"]["first"]["evidence_kind"], "agent_judged")
        self.assertEqual(result["measurements"]["first"]["source_sha256"], result["source_identity"]["sha256"])
        self.assertEqual(result["build"]["source_identity"], result["scoring"]["source_identity"])
        self.assertEqual(self.evaluations[1]["comparison_records"], result["comparison_measurements"])
        self.assertEqual(json.dumps(self.baseline, sort_keys=True), baseline_bytes)

    async def test_verifier_parses_count_text_but_keeps_original_response_evidence(self):
        baseline_bytes = json.dumps(self.baseline, sort_keys=True)
        responses = []

        async def textual(request):
            response = await self.evaluate(request)
            for row in response["measurements"].values():
                row["raw_value"] = str(row["raw_value"]) + ".0"
            responses.append(deepcopy(response))
            return response

        result = await self.check_candidate(self.candidate(), evaluator=textual)
        self.assertTrue(result["passed"], result["issues"])
        for row, expected, response in (
                (result["comparison_measurements"]["first"], 3, responses[0]),
                (result["measurements"]["first"], 1, responses[1])):
            self.assertIs(type(row["raw_value"]), int)
            self.assertEqual(row["raw_value"], expected)
            self.assertEqual(row["method"], self.method)
            self.assertEqual(row["evidence_kind"], "agent_judged")
            evidence = artifacts.artifact_bytes(self.root / ".unity/artifacts",
                row["evidence"]["artifact"]["artifact_id"])
            self.assertEqual(evidence.decode(), scoring._json(response))
            self.assertEqual(json.loads(evidence)["measurements"]["first"]["raw_value"], str(expected) + ".0")
        self.assertEqual(json.dumps(self.baseline, sort_keys=True), baseline_bytes)

    async def test_verifier_does_not_truncate_fractional_count_text_after_build(self):
        baseline_bytes = json.dumps(self.baseline, sort_keys=True)

        async def fractional(request):
            response = await self.evaluate(request)
            response["measurements"]["first"]["raw_value"] = "3.000000000000000001"
            return response

        with self.assertRaisesRegex(scoring.ScoringError, "invalid measurement record"):
            await self.check_candidate(self.candidate(), evaluator=fractional)
        self.assertTrue(self.build_calls)
        self.assertEqual(json.dumps(self.baseline, sort_keys=True), baseline_bytes)

    async def test_verifier_parses_ratio_text_and_keeps_undefined_evidence_gate(self):
        folder = self.root / ".unity/metrics"
        shutil.copyfile(Path(m.__file__).parent / "defaults/metrics/modularity.md", folder / "modularity.md")
        metric = m.load_metric(self.root, "modularity")
        method = {"description": "Explicit fixture ratio", "direction": "higher"}
        for raw, expected, evidence in (("0.75", 0.75, {"reason": "fixture ratio"}),
                ("undefined", "undefined", {"zero_denominator": True})):
            with self.subTest(raw=raw):
                async def ratio(_request):
                    return {"measurements": {"first": {"raw_value": raw,
                        "method": method, "evidence": evidence}}}

                result = await v.score_source(self.root, metric=metric, scope=self.scope,
                    subjects=["first"], original_methods={"first": method},
                    evaluator=ratio, run_checked=self.checked)
                row = result["measurements"]["first"]
                self.assertEqual(row["raw_value"], expected)
                self.assertIs(type(row["raw_value"]), type(expected))
                self.assertEqual(row["evidence_kind"], "agent_judged")
                if raw == "undefined":
                    self.assertTrue(row["evidence"]["zero_denominator"])

    async def test_exact_cache_reused_but_changed_commit_forces_current_scoring(self):
        candidate = self.candidate()
        result = await self.check_candidate(candidate)
        cache = result["comparison"]
        self.evaluations.clear()
        self.build_calls.clear()
        await self.check_candidate(candidate, before_cache=cache, after_cache=result)
        self.assertEqual(len(self.evaluations), 0)
        self.assertEqual(self.build_calls, [])
        (self.root / "Unrelated.lean").write_text("theorem extra : True := by trivial\n")
        self.git(self.root, "add", "Unrelated.lean")
        self.git(self.root, "commit", "--quiet", "-m", "accepted other declaration")
        self.evaluations.clear()
        await self.check_candidate(candidate, before_cache=cache)
        self.assertEqual(len(self.evaluations), 2)
        self.assertNotEqual(self.evaluations[0]["source_identity"], cache["identity"])

    async def test_legacy_candidate_cache_cannot_bypass_verifier_scoring(self):
        candidate = self.candidate()
        result = await self.check_candidate(candidate)
        legacy = deepcopy(result)
        legacy.pop("scoring")
        self.evaluations.clear()
        self.build_calls.clear()
        current = await self.check_candidate(candidate, before_cache=result["comparison"], after_cache=legacy)
        self.assertEqual(len(self.evaluations), 1)
        self.assertEqual(self.evaluations[0]["source_identity"], result["source_identity"])
        self.assertTrue(self.build_calls)
        self.assertTrue(current["passed"], current["issues"])
        self.assertTrue(v._valid(current["scoring"]))

    async def test_candidate_cache_scoring_must_bind_the_exact_records(self):
        candidate = self.candidate()
        result = await self.check_candidate(candidate)
        invalid = deepcopy(result)
        invalid["scoring"]["metric_sha256"] = "different-metric"
        invalid["scoring"] = v._seal({key: value for key, value in invalid["scoring"].items() if key != "sha256"})
        self.evaluations.clear()
        await self.check_candidate(candidate, before_cache=result["comparison"], after_cache=invalid)
        self.assertEqual(len(self.evaluations), 1)

    async def test_qualitative_cache_requires_identical_comparison_records(self):
        folder = self.root / ".unity" / "metrics"
        (folder / "quality.md").write_text("# quality\n## Metric function\nReadability judgment.\n")
        metric = m.load_metric(self.root, "quality")
        method = {"description": "Explicit fixture readability judgment"}
        identity = v.file_identity(self.root, scope=self.scope)
        old = m.record_measurement(metric, subject="first", raw_value="old", source_identity=identity,
            method=method, evidence={"reason": "old fixture"})
        requests = []

        async def judge(request):
            requests.append(request)
            return {"measurements": {"first": {"raw_value": "new", "method": method, "evidence": {"reason": "new fixture"}}},
                "judgments": {"first": {"improved": True, "reason": "specific readability comparison"}}}

        response = await v.score_source(self.root, metric=metric, scope=self.scope, subjects=["first"],
            original_methods={"first": method}, evaluator=judge, run_checked=self.checked,
            comparison_records={"first": old})
        response["comparison_measurements"] = {"first": old}
        await v.score_source(self.root, metric=metric, scope=self.scope, subjects=["first"],
            original_methods={"first": method}, evaluator=judge, run_checked=self.checked,
            comparison_records={"first": old}, cache=response)
        self.assertEqual(len(requests), 1)
        different = m.record_measurement(metric, subject="first", raw_value="different", source_identity=identity,
            method=method, evidence={"reason": "different fixture"})
        await v.score_source(self.root, metric=metric, scope=self.scope, subjects=["first"],
            original_methods={"first": method}, evaluator=judge, run_checked=self.checked,
            comparison_records={"first": different}, cache=response)
        self.assertEqual(len(requests), 2)

    async def test_source_change_during_scoring_rejected(self):
        candidate = self.candidate()

        async def change_source(request):
            result = await self.evaluate(request)
            (Path(request["root"]) / "Example.lean").write_text("changed during scoring\n")
            return result

        with self.assertRaisesRegex(scoring.ScoringError, "source or metric changed"):
            await self.check_candidate(candidate, evaluator=change_source)

    async def test_original_method_cannot_be_replaced(self):
        candidate = self.candidate()

        async def change_method(request):
            result = await self.evaluate(request)
            result["measurements"]["first"]["method"] = {"description": "Different method", "direction": "lower"}
            return result

        with self.assertRaisesRegex(scoring.ScoringError, "original measurement method"):
            await self.check_candidate(candidate, evaluator=change_method)

    async def test_build_success_cannot_hide_scorer_failure(self):
        async def failed(_request):
            raise scoring.ScoringError("fixture scorer failure")

        with self.assertRaisesRegex(scoring.ScoringError, "fixture scorer failure"):
            await self.check_candidate(self.candidate(), evaluator=failed)

    async def test_length_improvement_cannot_hide_build_failure(self):
        self.build_failure = True
        result = await self.check_candidate(self.candidate())
        self.assertFalse(result["passed"])
        self.assertIn("required_build_failed", {row["kind"] for row in result["issues"]})

    async def test_completion_keeps_partial_build_progress_but_final_requires_success(self):
        folder = self.root / ".unity" / "metrics"
        shutil.copyfile(Path(m.__file__).parent / "defaults" / "metrics" / "completion.md",
                        folder / "completion.md")
        self.metric = m.load_metric(self.root, "completion")
        self.method = {"description": "Explicit fixture unresolved obligations", "direction": "lower"}
        self.build_failure = True
        self.baseline = v.capture_baseline(self.root, metric=self.metric, scope=self.scope)
        self.baseline["measurements"] = {"first": m.record_measurement(self.metric,
            subject="first", raw_value=3, source_identity=self.baseline["identity"],
            method=self.method, evidence={"reason": "original unresolved obligations"})}
        self.baseline = v._seal({key: value for key, value in self.baseline.items() if key != "sha256"})
        candidate = self.candidate()
        shutil.copyfile(folder / "completion.md", candidate / ".unity" / "metrics" / "completion.md")
        result = await self.check_candidate(candidate)
        self.assertTrue(result["passed"], result["issues"])
        self.assertFalse(result["build"]["passed"])
        final = await v.verify_final_scored(candidate, baseline=self.baseline,
            metric=self.metric, scope=self.scope, evaluator=self.evaluate,
            run_checked=self.checked, project_root=self.root)
        self.assertFalse(final["passed"])
        self.assertIn("required_build_failed", {row["kind"] for row in final["issues"]})
        self.assertIn("completion_not_zero", {row["kind"] for row in final["issues"]})

    async def test_current_source_change_during_final_candidate_checks_is_rejected(self):
        candidate = self.candidate()

        async def checked(function, *args, **kwargs):
            result = function(*args, **kwargs)
            if function is v._verify:
                (self.root / "Example.lean").write_text("changed accepted source\n")
            return result

        with self.assertRaisesRegex(ValueError, "accepted source changed during candidate verification"):
            await v.verify_candidate_scored(candidate, before_root=self.root,
                baseline=self.baseline, metric=self.metric, scope=self.scope,
                task={"chunk_ids": ["first"], "files": ["Example.lean"]},
                evaluator=self.evaluate, run_checked=checked, project_root=self.root)

    async def test_final_build_and_scoring_share_original_baseline(self):
        result = await v.verify_final_scored(self.candidate(), baseline=self.baseline,
            metric=self.metric, scope=self.scope, evaluator=self.evaluate,
            run_checked=self.checked, project_root=self.root)
        self.assertTrue(result["passed"], result["issues"])
        self.assertEqual(len(self.evaluations), 1)
        self.assertEqual(result["comparison"], self.baseline)
        self.assertEqual(result["baseline_sha256"], self.baseline["sha256"])

    async def test_baseline_completion_is_idempotent_and_retains_original_bytes(self):
        saved = json.dumps(self.baseline, sort_keys=True)
        result = await v.complete_baseline_scored(self.baseline, self.root, subjects=["first"],
            evaluator=self.evaluate, run_checked=self.checked)
        self.assertEqual(json.dumps(result, sort_keys=True), saved)
        self.assertEqual(self.evaluations, [])
        unsealed = v.capture_baseline(self.root, metric=self.metric, scope=self.scope)
        result = await v.complete_baseline_scored(unsealed, self.root, subjects=["first"],
            evaluator=self.evaluate, run_checked=self.checked)
        self.assertEqual(result["identity"], unsealed["identity"])
        self.assertEqual(result["measurements"]["first"]["raw_value"], 3)
        self.assertEqual(unsealed["measurements"], {})

    def executable_metric(self, timeout=None):
        folder = self.root / ".unity" / "metrics"
        (folder / "measure.py").write_text("# Explicit offline fixture helper; never executed by these tests.\n")
        config = {"protocol": scoring.PROTOCOL, "helper": "measure.py", "interpreter": "python"}
        if timeout is not None:
            config["timeout_seconds"] = timeout
        (folder / "custom.md").write_text("# custom\n## Metric function\nmeasure.py\n## Evaluator\n" + json.dumps(config) + "\n")
        return m.load_metric(self.root, "custom")

    async def test_executable_adapter_runs_only_explicit_protocol_and_bounds_output(self):
        self.assertIsNone(scoring.executable_evaluator(self.metric, run_checked=self.checked, project_root=self.root))
        metric = self.executable_metric()
        identity = v.file_identity(self.root, scope=self.scope)
        build = v.build_scope(self.root, scope=self.scope, selected_files=["Example.lean"])
        commands = []

        async def helper_job(function, project_root, command, **kwargs):
            self.assertIs(function, jobs.run)
            self.assertNotIn("input", kwargs)
            commands.append(command)
            request = json.loads(Path(command[-1]).read_text())
            self.assertEqual(command[-2], "--request-file")
            response = await self.evaluate(request)
            response["protocol"] = scoring.PROTOCOL
            output = json.dumps(response)
            kwargs["on_output"]("stdout", output)
            return subprocess.CompletedProcess(command, 0, output, "")

        evaluator = scoring.executable_evaluator(metric, run_checked=helper_job, project_root=self.root)
        result = await scoring.score_snapshot(self.root, metric=metric, subjects=["first"],
            source_identity=identity, build_result=build, original_methods={"first": self.method},
            evaluator=evaluator)
        self.assertEqual(result["measurements"]["first"]["evidence_kind"], "measured")
        self.assertEqual(len(commands), 1)

        async def excessive(_function, _owner, command, **kwargs):
            kwargs["on_output"]("stderr", "x" * (scoring.MAX_RESPONSE_BYTES + 1))
            self.fail("oversized response was not interrupted")

        with self.assertRaisesRegex(scoring.ScoringError, "size limit"):
            await scoring.score_snapshot(self.root, metric=metric, subjects=["first"],
                source_identity=identity, build_result=build, original_methods={"first": self.method},
                evaluator=scoring.executable_evaluator(metric, run_checked=excessive, project_root=self.root))

    async def test_executable_custom_scores_preserve_float_and_text_types(self):
        metric = self.executable_metric()
        identity = v.file_identity(self.root, scope=self.scope)
        build = v.build_scope(self.root, scope=self.scope, selected_files=["Example.lean"])
        for raw in (0.75, -1.25, "0.75", "clearer"):
            with self.subTest(raw=raw):
                async def helper_job(_function, _owner, command, **kwargs):
                    response = {"protocol": scoring.PROTOCOL, "measurements": {
                        "first": {"raw_value": raw, "method": self.method,
                                  "evidence": {"reason": "fixture output"}}}}
                    output = json.dumps(response)
                    kwargs["on_output"]("stdout", output)
                    return subprocess.CompletedProcess(command, 0, output, "")

                result = await scoring.score_snapshot(self.root, metric=metric, subjects=["first"],
                    source_identity=identity, build_result=build, original_methods={"first": self.method},
                    evaluator=scoring.executable_evaluator(metric, run_checked=helper_job, project_root=self.root))
                row = result["measurements"]["first"]
                self.assertIs(type(row["raw_value"]), type(raw))
                self.assertEqual(row["raw_value"], raw)
                self.assertEqual(row["evidence_kind"], "measured")
                self.assertEqual(row["method"], self.method)

    def test_old_metric_hash_is_unchanged_without_explicit_evaluator(self):
        self.assertNotIn("evaluator", self.metric)
        self.assertEqual(self.metric, m.load_metric(self.root, "length"))
        folder = self.root / ".unity" / "metrics"
        (folder / "unfrozen.md").write_text('# custom\n## Evaluator\n{"protocol":"unity-optimize-score-v1","helper":"unknown.py"}\n')
        with self.assertRaisesRegex(ValueError, "unfrozen helper"):
            m.load_metric(self.root, "unfrozen")

    async def test_owned_executable_protocol_and_output_limit_with_real_fixture_process(self):
        # The only child is this generated Python fixture. No compiler, model,
        # remote service or repository evaluation is started.
        self.executable_metric()
        helper = self.root / ".unity" / "metrics" / "measure.py"
        helper.write_text("import json, pathlib, sys\n"
            "request = json.loads(pathlib.Path(sys.argv[2]).read_text())\n"
            "print(json.dumps({'protocol': 'unity-optimize-score-v1', 'measurements': "
            "{s: {'raw_value': 3, 'method': request['original_methods'][s], 'evidence': {'fixture': True}} "
            "for s in request['subjects']}, 'judgments': {}}))\n")
        metric = m.load_metric(self.root, "custom")
        identity = v.file_identity(self.root, scope=self.scope)
        build = v.build_scope(self.root, scope=self.scope, selected_files=["Example.lean"])

        async def real_checked(_function, *args, **kwargs):
            return await asyncio.to_thread(_REAL_JOB_RUN, *args, **kwargs)

        result = await scoring.score_snapshot(self.root, metric=metric, subjects=["first"],
            source_identity=identity, build_result=build, original_methods={"first": self.method},
            evaluator=scoring.executable_evaluator(metric, run_checked=real_checked, project_root=self.root))
        self.assertEqual(result["measurements"]["first"]["raw_value"], 3)
        self.assertEqual(result["evaluator_kind"], "measured")
        self.assertEqual(list((self.root / ".unity" / "jobs" / "optimize").glob("*.json")), [])

        helper.write_text("import sys\nwhile True:\n sys.stdout.write('x' * 65536)\n sys.stdout.flush()\n")
        metric = m.load_metric(self.root, "custom")
        with self.assertRaisesRegex(scoring.ScoringError, "size limit"):
            await scoring.score_snapshot(self.root, metric=metric, subjects=["first"],
                source_identity=identity, build_result=build, original_methods={"first": self.method},
                evaluator=scoring.executable_evaluator(metric, run_checked=real_checked, project_root=self.root))
        self.assertEqual(list((self.root / ".unity" / "jobs" / "optimize").glob("*.json")), [])

    async def test_explicit_deadline_cancels_and_drains_owned_fixture(self):
        from unity.optimize_workflow import Controller
        self.executable_metric(timeout=0.15)
        helper = self.root / ".unity" / "metrics" / "measure.py"
        helper.write_text("import time\nwhile True:\n time.sleep(1)\n")
        metric = m.load_metric(self.root, "custom")
        identity = v.file_identity(self.root, scope=self.scope)
        build = v.build_scope(self.root, scope=self.scope, selected_files=["Example.lean"])
        paths = SimpleNamespace(project_root=self.root, unity=self.root / ".unity")
        controller = Controller(paths, SimpleNamespace(agents=[]))

        async def checked(_function, *args, **kwargs):
            return await controller.checked_job(_REAL_JOB_RUN, *args, **kwargs)

        with self.assertRaisesRegex(scoring.ScoringError, "deadline exceeded"):
            await scoring.score_snapshot(self.root, metric=metric, subjects=["first"],
                source_identity=identity, build_result=build, original_methods={"first": self.method},
                evaluator=scoring.executable_evaluator(metric, run_checked=checked, project_root=self.root))
        self.assertTrue(controller.build_cancel.is_set())
        self.assertEqual(list((self.root / ".unity" / "jobs" / "optimize").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
