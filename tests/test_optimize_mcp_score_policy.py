"""Offline shell-bridge scoring policy; no providers, compiler, or native workers."""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastmcp.exceptions import ToolError

from unity import optimize_mcp_bridge as bridge
from unity import optimize_metrics as metrics, optimize_tasks as tasks
from unity import optimize_verification as verification, optimize_worktree
from unity.forum import optimize_server


MUTATING_TOOLS = (
    ("lean-lsp", "lean_multi_attempt"),
    ("axle", "repair_proofs"),
    ("axle", "theorem2sorry"),
    ("aristotle", "aristotle_submit"),
    ("aristotle", "aristotle_result"),
    ("aristotle", "aristotle_cancel"),
)
INSPECTION_TOOLS = (
    ("lean-lsp", "lean_diagnostic_messages"),
    ("axle", "check"),
    ("aristotle", "aristotle_status"),
)


class OptimizeMcpScorePolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="optimize-bridge-score-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "A.lean").write_text("theorem one : True := by trivial\n")
        (self.root / ".gitignore").write_text(".unity/\n.worktrees/\n")
        self.git("init", "-q")
        self.git("config", "user.name", "Offline Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("add", "A.lean", ".gitignore")
        self.git("commit", "-qm", "Original offline source")
        metric_dir = self.root / ".unity/metrics"
        metric_dir.mkdir(parents=True)
        definition = Path(metrics.__file__).parent / "defaults/metrics/length.md"
        (metric_dir / "length.md").write_text(definition.read_text())
        self.scope = {"targets": "A.lean only", "files": ["A.lean"]}
        self.directory = self.root / ".unity/forum/optimize"
        tasks.initialize(self.directory, self.root, run_id="optimize-bridge-score",
            base_sha=self.git("rev-parse", "HEAD"), metric=metrics.load_metric(self.root, "length"),
            scope=self.scope, config={"primary": "Alice", "agents": ["Alice"], "targets": "A.lean only"})
        tasks.install_inventory(self.directory, tasks.scan_sources(self.root, ["A.lean"]))
        self.subject = next(iter(tasks.load(self.directory)["chunks"]))
        self.tree = optimize_worktree.create_worktree("score", self.root)
        self.identity = verification.file_identity(self.tree, scope=self.scope)
        self.method = {"description": "Offline bridge score fixture", "direction": "lower"}
        self.request = tasks.begin_scoring_request(self.directory, author="Alice", worktree=self.tree,
            source_identity=self.identity, subjects=[self.subject], original_methods={self.subject: self.method})
        self.environment = {
            "UNITY_OPTIMIZE_WORKFLOW": "metric-optimization-v1",
            "UNITY_OPTIMIZE_PROJECT_ROOT": str(self.root), "UNITY_OPTIMIZE_WORKTREE": str(self.tree),
            "UNITY_OPTIMIZE_PROFILE": "optimizing", "UNITY_AGENT_NAME": "Alice",
            "UNITY_OPTIMIZE_SCORE_REQUEST_ID": self.request["request_id"],
        }
        self.external = AsyncMock(return_value=(SimpleNamespace(content=[]), ""))
        self.enterContext(patch.object(bridge, "_external_call", self.external))
        self.enterContext(patch("unity.optimize_workflow.mcp_servers", side_effect=self.servers))

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, text=True,
                              capture_output=True, check=True).stdout.strip()

    def servers(self, paths, profile):
        return {
            "unity-forum": {"command": sys.executable, "args": ["-m", "unity.forum.optimize_server",
                "--forum-dir", str(paths.forum), "--project-root", str(self.root), "--profile", profile]},
            "lean-lsp": {"command": "uvx", "args": ["lean-lsp-mcp"]},
            "axle": {"command": "uvx", "args": ["--from", "axiom-axle-mcp", "axle-mcp-server"]},
            "aristotle": {"command": sys.executable, "args": ["-m", "unity.aristotle"]},
        }

    async def call(self, server, tool, arguments=None, *, environment=None):
        env = self.environment if environment is None else environment
        # The real in-process forum server reads the same binding from os.environ.
        with patch.dict(os.environ, env), redirect_stdout(io.StringIO()):
            return await bridge.call_tool(server, tool, arguments or {}, environ=env, cwd=self.tree)

    async def test_bound_scoring_rejects_external_mutation_before_transport_call(self):
        before = (self.directory / tasks.STATE_FILE).read_bytes()
        for server, tool in MUTATING_TOOLS:
            with self.subTest(server=server, tool=tool):
                with self.assertRaisesRegex(ValueError, "tool is not exposed"):
                    await self.call(server, tool)
        self.external.assert_not_awaited()
        self.assertEqual((self.directory / tasks.STATE_FILE).read_bytes(), before)

    async def test_bound_scoring_keeps_external_inspection_tools(self):
        for server, tool in INSPECTION_TOOLS:
            with self.subTest(server=server, tool=tool):
                await self.call(server, tool)
                self.assertEqual(self.external.await_args.args[1], server)
                self.assertEqual(self.external.await_args.args[3], tool)
                self.assertEqual(self.external.await_args.kwargs["cwd"], self.tree)
        self.assertEqual(self.external.await_count, len(INSPECTION_TOOLS))

    async def test_writer_catalog_is_unchanged_without_score_binding(self):
        for binding in (None, "", "  "):
            env = {**self.environment}
            env.pop("UNITY_OPTIMIZE_SCORE_REQUEST_ID")
            if binding is not None:
                env["UNITY_OPTIMIZE_SCORE_REQUEST_ID"] = binding
            for server, tool in MUTATING_TOOLS:
                with self.subTest(binding=binding, server=server, tool=tool):
                    await self.call(server, tool, environment=env)
        self.assertEqual(self.external.await_count, 3 * len(MUTATING_TOOLS))

    async def test_critic_external_catalog_is_unchanged(self):
        env = {**self.environment, "UNITY_OPTIMIZE_PROFILE": "critic", "UNITY_OPTIMIZE_SCORE_REQUEST_ID": ""}
        for server, tool in MUTATING_TOOLS:
            with self.subTest(server=server, tool=tool):
                with self.assertRaisesRegex(ValueError, "tool is not exposed"):
                    await self.call(server, tool, environment=env)
        self.external.assert_not_awaited()
        for server, tool in INSPECTION_TOOLS:
            await self.call(server, tool, environment=env)
        self.assertEqual(self.external.await_count, len(INSPECTION_TOOLS))

    async def test_bound_scoring_rejects_candidate_tools_before_forum_call(self):
        with patch.object(optimize_server, "configure") as configure:
            for tool in ("submit_candidate", "record_measurement", "select_candidate"):
                with self.subTest(tool=tool):
                    with self.assertRaisesRegex(ValueError, "tool is not exposed"):
                        await self.call("forum", tool)
            configure.assert_not_called()
        self.external.assert_not_awaited()

    async def test_bound_scoring_can_inspect_and_submit_exact_request(self):
        before = tasks.load(self.directory)
        for tool in ("optimize_status", "scoring_request"):
            result = await self.call("forum", tool)
            self.assertFalse(result.is_error)
        arguments = {"author": "Alice", "request_id": self.request["request_id"],
            "subject": self.subject, "raw_value": 1, "method": self.method,
            "evidence": {"reason": "Explicit offline fixture"}}
        result = await self.call("forum", "submit_score_report", arguments)
        self.assertFalse(result.is_error)
        state = tasks.load(self.directory)
        report = state["scoring_requests"][self.request["request_id"]]["reports"][self.subject]
        self.assertEqual(report["raw_value"], 1)
        self.assertEqual(report["evidence_kind"], "agent_judged")
        for key in ("baseline", "baseline_sealed", "baseline_measurements", "measurements", "candidates", "rounds", "review"):
            self.assertEqual(state[key], before[key])
        self.assertEqual(verification.file_identity(self.tree, scope=self.scope), self.identity)
        self.external.assert_not_awaited()

    async def test_score_submission_preserves_request_and_source_binding(self):
        arguments = {"author": "Alice", "request_id": self.request["request_id"],
            "subject": self.subject, "raw_value": 1, "method": self.method,
            "evidence": {"reason": "Explicit offline fixture"}}
        before = (self.directory / tasks.STATE_FILE).read_bytes()
        with self.assertRaisesRegex(ValueError, "author does not match"):
            await self.call("forum", "submit_score_report", {**arguments, "author": "Other"})
        with self.assertRaisesRegex(ToolError, "running bound request"):
            await self.call("forum", "submit_score_report", {**arguments, "request_id": "score-request-other"})
        self.assertEqual((self.directory / tasks.STATE_FILE).read_bytes(), before)
        source = self.tree / "A.lean"
        original = source.read_bytes()
        try:
            source.write_bytes(original + b"-- offline fixture source tamper\n")
            with self.assertRaisesRegex(ToolError, "source changed"):
                await self.call("forum", "submit_score_report", arguments)
            self.assertEqual((self.directory / tasks.STATE_FILE).read_bytes(), before)
        finally:
            source.write_bytes(original)
        self.external.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
