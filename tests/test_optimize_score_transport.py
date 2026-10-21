"""Optimize score parsing over real local MCP stdio; no providers or compiler."""
from contextlib import asynccontextmanager
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from unity import optimize_metrics as metrics, optimize_tasks as tasks
from unity import optimize_verification as verification
from unity import optimize_spawn


class OptimizeScoreTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="optimize-score-stdio-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "project"
        self.root.mkdir()
        self.directory = self.root / ".unity/forum/optimize"
        self.scope = {"targets": "A.lean only", "files": ["A.lean"]}
        self.method = {"description": "Offline score fixture", "direction": "lower"}
        (self.root / "A.lean").write_text("theorem one : True := by trivial\n")
        (self.root / ".gitignore").write_text(".unity/\n.worktrees/\n")
        self.git("init", "-q")
        self.git("config", "user.name", "Offline Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("add", "A.lean", ".gitignore")
        self.git("commit", "-qm", "Original offline source")
        self.tree = self.root / ".worktrees/score"
        self.git("worktree", "add", "--quiet", "--detach", str(self.tree), "HEAD")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, text=True, capture_output=True,
                              check=True).stdout.strip()

    def metric_fixture(self, name="length"):
        metric_dir = self.root / ".unity/metrics"
        metric_dir.mkdir(parents=True)
        definition = (Path(metrics.__file__).parent / "defaults/metrics" / (name + ".md"))
        (metric_dir / (name + ".md")).write_text(definition.read_text() if definition.is_file()
            else "# custom\n## Prompt\nUse the supplied assessment method.\n")
        self.metric = metrics.load_metric(self.root, name)
        if name == "modularity":
            self.method = {"description": "Offline ratio fixture", "direction": "higher"}
        tasks.initialize(self.directory, self.root, run_id="optimize-stdio-parser", base_sha=self.git("rev-parse", "HEAD"),
            metric=self.metric, scope=self.scope,
            config={"primary": "Alice", "agents": ["Alice"], "targets": "A.lean only"})
        tasks.install_inventory(self.directory, tasks.scan_sources(self.root, ["A.lean"]))
        self.subject = next(iter(tasks.load(self.directory)["chunks"]))

    def scoring_fixture(self, name="length"):
        self.metric_fixture(name)
        self.identity = verification.file_identity(self.tree, scope=self.scope)
        self.request = tasks.begin_scoring_request(self.directory, author="Alice", worktree=self.tree,
            source_identity=self.identity, subjects=[self.subject], original_methods={self.subject: self.method})

    def baseline_fixture(self):
        self.metric_fixture()
        self.identity = verification.file_identity(self.root, scope=self.scope)
        tasks.set_baseline(self.directory, {"source_identity": self.identity,
            "metric": self.metric, "scope": self.scope, "measurements": {}})
        with tasks.transaction(self.directory) as state:
            state["phase"] = "chunking"

    def candidate_fixture(self):
        self.baseline_fixture()
        proposal = tasks.propose_plan(self.directory, "Alice", [{"chunk_ids": [self.subject]}],
                                      "Offline measurement parsing fixture")
        tasks.accept_plan(self.directory, proposal["proposal_id"])
        original = metrics.record_measurement(self.metric, subject=self.subject, raw_value=18,
            source_identity=self.identity, method=self.method,
            evidence={"reason": "Fixed original fixture evidence"})
        tasks.install_verified_baseline_measurements(self.directory, {self.subject: original})
        tasks.seal_baseline(self.directory)
        with tasks.transaction(self.directory) as state:
            state["measurements"] = deepcopy(state["baseline"]["measurements"])
        tasks.begin_round(self.directory, max_rounds=5)
        self.task_id = next(iter(tasks.load(self.directory)["tasks"]))
        strategy = tasks.register_strategy(self.directory, "Alice", self.task_id,
            "Offline fixture strategy", "Exercise advisory input parsing only")
        tasks.claim_strategy(self.directory, "Alice", strategy["strategy_id"])
        self.assignment = tasks.configure_assignment(self.directory, "Alice", strategy["strategy_id"],
                                                      self.tree, self.git("rev-parse", "HEAD"))

    @asynccontextmanager
    async def client(self, *, profile="optimizing", scoring=True):
        # Start the production MCP entry point, with a fixture-only binding.
        # No Unity pipeline, model client, native worker, Lean or Lake is started.
        environment = {"PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "PYTHONDONTWRITEBYTECODE": "1",
            "UNITY_OPTIMIZE_SCORE_REQUEST_ID": self.request["request_id"] if scoring else "",
            "UNITY_OPTIMIZE_WORKTREE": str(self.tree), "UNITY_AGENT_NAME": "Alice",
            "UNITY_OPTIMIZE_AUTHOR": "Alice", "UNITY_OPTIMIZE_PROFILE": profile}
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
            transport = StdioTransport(command=sys.executable,
                args=["-m", "unity.forum.optimize_server", "--forum-dir", str(self.directory),
                      "--project-root", str(self.root), "--profile", profile, "--author", "Alice"],
                env=environment, cwd=str(self.tree), keep_alive=False, log_file=stderr)
            async with Client(transport, timeout=20) as client:
                yield client

    async def submit(self, client, value, *, evidence=None):
        return await client.call_tool_mcp("submit_score_report", {
            "author": "Alice", "request_id": self.request["request_id"], "subject": self.subject,
            "raw_value": value, "method": self.method,
            "evidence": evidence or {"reason": "Explicit offline fixture"}})

    async def test_runner_stdio_reports_scores_while_startup_files_stay_outside_source(self):
        self.scoring_fixture()
        environment = {"PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "PYTHONDONTWRITEBYTECODE": "1",
            "UNITY_OPTIMIZE_SCORE_REQUEST_ID": self.request["request_id"],
            "UNITY_OPTIMIZE_WORKTREE": str(self.tree), "UNITY_AGENT_NAME": "Alice",
            "UNITY_OPTIMIZE_AUTHOR": "Alice", "UNITY_OPTIMIZE_PROFILE": "optimizing"}
        server = {"command": sys.executable, "args": ["-m", "unity.forum.optimize_server",
            "--forum-dir", str(self.directory), "--project-root", str(self.root),
            "--profile", "optimizing", "--author", "Alice"], "env": environment}
        with patch.object(optimize_spawn, "_worktree_write_roots",
                          return_value=(self.tree, self.root / ".unity")):
            prepared = optimize_spawn._claude_options(self.tree, "fixture", {"unity-forum": server},
                "bypassPermissions", environment)
        self.addCleanup(optimize_spawn._finish_claude, prepared["scratch"], True, False)
        runner = Path(prepared["sdk_cwd"])
        (runner / ".gitconfig").touch()
        local = prepared["mcp_servers"]["unity-forum"]
        before = tasks.load(self.directory)
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
            transport = StdioTransport(command=local["command"], args=local["args"],
                env=local["env"], cwd=str(runner), keep_alive=False, log_file=stderr)
            async with Client(transport, timeout=20) as client:
                result = await self.submit(client, 1)
                self.assertFalse(result.isError, result.content)
                self.assertTrue((runner / ".gitconfig").exists())
                self.assertFalse((self.tree / ".gitconfig").exists())
                self.assertEqual(verification.file_identity(self.tree, scope=self.scope), self.identity)
                # A real source change is still rejected. No identity exclusion
                # or relaxed reporting check was introduced for sandbox startup.
                source = self.tree / "A.lean"
                original = source.read_bytes()
                try:
                    source.write_bytes(original + b"-- fixture tamper\n")
                    state_bytes = (self.directory / tasks.STATE_FILE).read_bytes()
                    rejected = await self.submit(client, 0)
                    self.assertTrue(rejected.isError)
                    self.assertIn("source changed", " ".join(getattr(block, "text", "") for block in rejected.content))
                    self.assertEqual((self.directory / tasks.STATE_FILE).read_bytes(), state_bytes)
                finally:
                    source.write_bytes(original)
        self.assertEqual(self.stored()["raw_value"], 1)
        self.assert_authoritative_evidence_unchanged(before)

    def stored(self):
        return tasks.load(self.directory)["scoring_requests"][self.request["request_id"]]["reports"][self.subject]

    def measurement_args(self, value, **extra):
        return {"author": "Alice", "chunk_id": self.subject, "raw_value": value,
                "method": self.method, "evidence": {"reason": "Offline advisory evidence"}, **extra}

    def assert_authoritative_evidence_unchanged(self, before):
        after = tasks.load(self.directory)
        for key in ("baseline", "baseline_sealed", "baseline_measurements", "measurements", "candidates", "rounds", "review"):
            self.assertEqual(after[key], before[key])

    async def assert_bool_rejected_without_mutation(self, client, tool, **extra):
        for value in (True, False):
            with self.subTest(tool=tool, rejected=value):
                before = (self.directory / tasks.STATE_FILE).read_bytes()
                result = await client.call_tool_mcp(tool, self.measurement_args(value, **extra))
                self.assertTrue(result.isError, result.content)
                # Confirm strict argument validation, not an unrelated profile or assignment error.
                message = " ".join(getattr(block, "text", "") for block in result.content)
                self.assertIn("raw_value", message)
                self.assertEqual((self.directory / tasks.STATE_FILE).read_bytes(), before)

    async def test_baseline_advice_parses_text_without_publishing_original_measurements(self):
        self.baseline_fixture()
        before = tasks.load(self.directory)
        async with self.client(profile="chunking", scoring=False) as client:
            result = await client.call_tool_mcp("record_measurement", self.measurement_args("18.0", baseline=True))
            self.assertFalse(result.isError, result.content)
            report = tasks.load(self.directory)["advisory_baseline_measurements"][self.subject][-1]["measurement"]
            self.assertIs(type(report["raw_value"]), int)
            self.assertEqual(report["raw_value"], 18)
            self.assertEqual(report["method"], self.method)
            await self.assert_bool_rejected_without_mutation(client, "record_measurement", baseline=True)
        self.assert_authoritative_evidence_unchanged(before)

    async def test_current_advice_parses_text_without_changing_sealed_original_evidence(self):
        self.candidate_fixture()
        before = tasks.load(self.directory)
        async with self.client(scoring=False) as client:
            result = await client.call_tool_mcp("record_current_measurement", self.measurement_args("1.0"))
            self.assertFalse(result.isError, result.content)
            report = tasks.load(self.directory)["advisory_current_measurements"][self.subject][-1]["measurement"]
            self.assertIs(type(report["raw_value"]), int)
            self.assertEqual(report["raw_value"], 1)
            self.assertEqual(report["method"], self.method)
            await self.assert_bool_rejected_without_mutation(client, "record_current_measurement")
        self.assert_authoritative_evidence_unchanged(before)

    async def test_candidate_advice_parses_text_through_both_tools_without_publishing_scores(self):
        self.candidate_fixture()
        before = tasks.load(self.directory)
        assignment_id = self.assignment["assignment_id"]
        async with self.client(scoring=False) as client:
            for tool, value, expected in (("record_measurement", "18.0", 18), ("measure_candidate", "1", 1)):
                with self.subTest(tool=tool):
                    result = await client.call_tool_mcp(tool, self.measurement_args(value, assignment_id=assignment_id))
                    self.assertFalse(result.isError, result.content)
                    report = tasks.load(self.directory)["assignments"][assignment_id]["advisory_measurements"][self.subject]
                    self.assertIs(type(report["raw_value"]), int)
                    self.assertEqual(report["raw_value"], expected)
                    self.assertEqual(report["method"], self.method)
                    await self.assert_bool_rejected_without_mutation(client, tool, assignment_id=assignment_id)
        self.assert_authoritative_evidence_unchanged(before)

    async def test_all_four_raw_value_tool_schemas_keep_numeric_and_text_inputs(self):
        self.candidate_fixture()
        async with self.client(scoring=False) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
        for name in ("record_measurement", "record_current_measurement", "measure_candidate", "submit_score_report"):
            with self.subTest(tool=name):
                schema = tools[name].inputSchema["properties"]["raw_value"]
                self.assertEqual({branch["type"] for branch in schema["anyOf"]},
                                 {"integer", "number", "string"})

    async def test_length_numeric_text_and_whole_floats_reach_stored_integer(self):
        self.scoring_fixture()
        before = tasks.load(self.directory)
        async with self.client() as client:
            for raw, expected in (("0", 0), ("1", 1), ("1.0", 1), ("18", 18), ("18.0", 18),
                                  ("1.8e1", 18), (18.0, 18), (18, 18)):
                with self.subTest(raw=raw):
                    result = await self.submit(client, raw)
                    self.assertFalse(result.isError, result.content)
                    report = self.stored()
                    self.assertIs(type(report["raw_value"]), int)
                    self.assertEqual(report["raw_value"], expected)
                    self.assertEqual(report["evidence_kind"], "agent_judged")
                    self.assertEqual(report["method"], self.method)
                    self.assertNotIn("sha256", report)
        after = tasks.load(self.directory)
        for key in ("baseline", "baseline_sealed", "baseline_measurements", "measurements", "candidates", "rounds", "review"):
            self.assertEqual(after[key], before[key])

    async def test_completion_numeric_text_keeps_integer_count(self):
        self.scoring_fixture("completion")
        async with self.client() as client:
            result = await self.submit(client, "2.0")
            self.assertFalse(result.isError, result.content)
            self.assertIs(type(self.stored()["raw_value"]), int)
            self.assertEqual(self.stored()["raw_value"], 2)

    async def test_count_rejections_do_not_publish_a_report_or_change_state(self):
        self.scoring_fixture()
        async with self.client() as client:
            for raw in ("1.9", 1.9, "-1", -1, True, False, "NaN", "Infinity", "", "one", "1 tactic"):
                with self.subTest(raw=raw):
                    before = (self.directory / tasks.STATE_FILE).read_bytes()
                    result = await self.submit(client, raw)
                    self.assertTrue(result.isError, result.content)
                    self.assertEqual((self.directory / tasks.STATE_FILE).read_bytes(), before)

    async def test_modularity_numeric_text_remains_a_ratio_and_not_an_integer(self):
        self.scoring_fixture("modularity")
        async with self.client() as client:
            for raw, expected in (("0.75", 0.75), ("7.5e-1", 0.75), (0.75, 0.75), ("0", 0), ("1", 1)):
                with self.subTest(raw=raw):
                    result = await self.submit(client, raw)
                    self.assertFalse(result.isError, result.content)
                    self.assertEqual(self.stored()["raw_value"], expected)
                    if expected == 0.75:
                        self.assertIs(type(self.stored()["raw_value"]), float)
            for raw in ("1.01", "-0.01", "NaN", "Infinity", True, False, "undefined"):
                with self.subTest(rejected=raw):
                    before = (self.directory / tasks.STATE_FILE).read_bytes()
                    result = await self.submit(client, raw)
                    self.assertTrue(result.isError, result.content)
                    self.assertEqual((self.directory / tasks.STATE_FILE).read_bytes(), before)

    async def test_modularity_undefined_still_requires_zero_denominator_evidence(self):
        self.scoring_fixture("modularity")
        async with self.client() as client:
            evidence = {"zero_denominator": True, "reason": "Fixture has no tactics"}
            result = await self.submit(client, "undefined", evidence=evidence)
            self.assertFalse(result.isError, result.content)
            self.assertEqual(self.stored()["raw_value"], "undefined")
            self.assertEqual(self.stored()["evidence"], evidence)

    async def test_custom_numeric_and_qualitative_types_are_not_guessed(self):
        self.scoring_fixture("custom")
        async with self.client() as client:
            for raw in (0.75, -1.5, 18, "clearer", "18", "0.75"):
                with self.subTest(raw=raw):
                    result = await self.submit(client, raw)
                    self.assertFalse(result.isError, result.content)
                    self.assertIs(type(self.stored()["raw_value"]), type(raw))
                    self.assertEqual(self.stored()["raw_value"], raw)
            for raw in (True, False):
                with self.subTest(rejected=raw):
                    before = deepcopy(tasks.load(self.directory))
                    result = await self.submit(client, raw)
                    self.assertTrue(result.isError, result.content)
                    self.assertEqual(tasks.load(self.directory), before)


if __name__ == "__main__":
    unittest.main()
