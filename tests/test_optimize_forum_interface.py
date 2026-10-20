"""Offline model-visible Optimize contracts: actual FastMCP tools, no providers."""
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastmcp import Client
from fastmcp.exceptions import ToolError
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._errors import CLIJSONDecodeError
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from unity import artifacts, optimize_mcp_bridge as bridge, optimize_tasks as tasks
from unity import optimize_metrics as metrics, optimize_verification as verification
from unity.forum import optimize_server as forum


class OptimizeForumInterfaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "project"
        self.root.mkdir()
        self.directory = self.root / ".unity/forum/optimize"
        self.metric = {"name": "length", "builtin": "length", "direction": "lower",
                       "definition": "Count tactic invocations", "units": "tactic invocations"}
        tasks.initialize(self.directory, self.root, run_id="optimize-interface", base_sha="base",
            metric=self.metric, scope={"targets": "A.lean only", "files": ["A.lean", "B.lean"]},
            config={"primary": "Alice", "agents": ["Alice", "Bob"], "targets": "A.lean only"})
        (self.root / "A.lean").write_text("theorem one : True := by trivial\ntheorem two : True := by trivial\n")
        (self.root / "B.lean").write_text("theorem other : True := by trivial\n")
        tasks.install_inventory(self.directory, tasks.scan_sources(self.root, ["A.lean", "B.lean"]))
        tasks.set_baseline(self.directory, {"build": {"passed": True}, "source_identity": {"main_sha": "base"}})
        with tasks.transaction(self.directory) as state:
            state["phase"] = "chunking"
        self.dispatch = tasks.begin_phase_dispatch(self.directory, "chunking", ["Alice", "Bob"])
        self.tree = self.root / "private-worker"
        (self.tree / ".unity").mkdir(parents=True)
        (self.tree / ".unity/optimize-worktree.json").write_text(json.dumps({
            "schema": "optimize-worktree-v1", "project_root": str(self.root)}))
        (self.tree / ".unity/forum").symlink_to(self.root / ".unity/forum", target_is_directory=True)
        self.environment = {
            "UNITY_OPTIMIZE_WORKFLOW": "metric-optimization-v1", "UNITY_OPTIMIZE_PROFILE": "chunking",
            "UNITY_AGENT_NAME": "Alice", "UNITY_OPTIMIZE_AUTHOR": "Alice", "UNITY_OPTIMIZE_TASK_ID": "",
            "UNITY_OPTIMIZE_PHASE_DISPATCH_ID": self.dispatch["dispatch_id"],
            "UNITY_OPTIMIZE_PROJECT_ROOT": str(self.root), "UNITY_OPTIMIZE_WORKTREE": str(self.tree),
        }
        environment = patch.dict(os.environ, self.environment, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        expected_errors = patch.object(logging.getLogger("fastmcp.server.server"), "exception")
        expected_errors.start()
        self.addCleanup(expected_errors.stop)
        forum.configure(self.directory, self.root, "chunking", author="Alice")

    async def native(self, tool, arguments):
        async with Client(forum.build_server(forum.PROFILE)) as client:
            result = await client.call_tool(tool, arguments)
        return result.structured_content

    async def native_raw(self, tool, arguments):
        """Retain actual FastMCP text AND structured output for wire-size tests."""
        async with Client(forum.build_server(forum.PROFILE)) as client:
            result = await client.call_tool_mcp(tool, arguments)
        self.assertFalse(result.isError, result.content)
        return result

    def large_candidate_fixture(self):
        tasks.close_phase_dispatch(self.directory, self.dispatch["dispatch_id"], "complete")
        state = tasks.load(self.directory)
        plan = tasks.propose_plan(self.directory, "Alice", [
            {"chunk_ids": [cid]} for cid in state["chunks"]], "All declaration fixtures")
        tasks.accept_plan(self.directory, plan["proposal_id"])
        state = tasks.load(self.directory)
        task_id = next(iter(state["tasks"]))
        # A realistic repository-sized source map, not just a long display message.
        files = {f"Batteries/Fixture/Module{index:05}.lean": hashlib.sha256(str(index).encode()).hexdigest()
                 for index in range(12000)}
        candidate = {"candidate_id": "candidate-1", "task_id": task_id, "author": "Alice",
            "status": "checked", "base_main_sha": "base", "task_revision": 0, "config_revision": 0,
            "assignment_id": "assignment-fixture", "strategy_id": "strategy-fixture",
            "worktree": str(self.tree), "notes": "Shorter equivalent proof, Unicode evidence 雪 😀",
            "measurements": {}, "endorsements": [], "objections": [],
            "verification": {"passed": True, "source_identity": {"main_sha": "base", "files": files},
                             "issues": [], "build": {"passed": True}}}
        with tasks.transaction(self.directory) as current:
            current["phase"] = "optimizing"
            current["candidates"][candidate["candidate_id"]] = candidate
        forum.configure(self.directory, self.root, "optimizing", author="Alice")
        os.environ.update(UNITY_OPTIMIZE_PROFILE="optimizing", UNITY_OPTIMIZE_PHASE_DISPATCH_ID="",
                          UNITY_OPTIMIZE_BASE_SHA="base")
        return task_id, deepcopy(candidate)

    def measurement_fixture(self):
        """Actual Git + grouped forum contract; no compiler/model invocation."""
        self.root = self.root.parent / "measurement-project"
        self.root.mkdir()
        self.directory = self.root / ".unity/forum/optimize"
        (self.root / "A.lean").write_text("theorem one : True := by trivial\ntheorem two : True := by trivial\n")
        (self.root / "B.lean").write_text("theorem other : True := by trivial\n")
        def git(root, *args):
            return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True,
                                  check=True).stdout.strip()
        self.git = git
        (self.root / ".gitignore").write_text(".unity/\n.worktrees/\nprivate-worker/\n")
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "Offline Fixture")
        git(self.root, "config", "user.email", "fixture@example.invalid")
        git(self.root, "add", "A.lean", "B.lean", ".gitignore")
        git(self.root, "commit", "-qm", "Original offline source")
        head = git(self.root, "rev-parse", "HEAD")
        metric_dir = self.root / ".unity/metrics"
        metric_dir.mkdir(parents=True)
        (metric_dir / "length.md").write_text(
            (Path(metrics.__file__).parent / "defaults/metrics/length.md").read_text())
        self.metric = metrics.load_metric(self.root, "length")
        scope = {"targets": "A.lean only", "files": ["A.lean", "B.lean"]}
        tasks.initialize(self.directory, self.root, run_id="optimize-measurement-interface", base_sha=head,
            metric=self.metric, scope=scope,
            config={"primary": "Alice", "agents": ["Alice", "Bob"], "targets": "A.lean only"})
        identity = verification.file_identity(self.root, scope=scope)
        tasks.set_baseline(self.directory, {"identity": identity, "source_identity": identity,
            "metric": self.metric, "scope": scope, "measurements": {}})
        tasks.install_inventory(self.directory, tasks.scan_sources(self.root, ["A.lean", "B.lean"]))
        with tasks.transaction(self.directory) as current:
            current["phase"] = "chunking"
        self.dispatch = tasks.begin_phase_dispatch(self.directory, "chunking", ["Alice", "Bob"])
        forum.configure(self.directory, self.root, "chunking", author="Alice")
        state = tasks.load(self.directory)
        self.required = sorted(cid for cid, row in state["chunks"].items() if row["path"] == "A.lean")
        proposal = tasks.propose_plan(self.directory, "Alice", [{"chunk_ids": self.required,
            "reason": "Two declarations share the assigned file in this grouped-task fixture"}],
            "Grouped source fixture", exclusions={cid: "Outside A.lean target"
                for cid, row in state["chunks"].items() if row["path"] != "A.lean"})
        tasks.accept_plan(self.directory, proposal["proposal_id"])
        self.method = {"description": "Explicit tactic invocation count", "direction": "lower"}
        for chunk_id in self.required:
            forum.record_measurement("Alice", chunk_id, 1, self.method, {"count": "trivial"}, baseline=True)
        tasks.close_phase_dispatch(self.directory, self.dispatch["dispatch_id"], "complete")
        tasks.seal_baseline(self.directory)
        with tasks.transaction(self.directory) as current:
            current["measurements"] = deepcopy(current["baseline"]["measurements"])
            current["phase"] = "optimizing"
        tasks.begin_round(self.directory, max_rounds=5)
        self.task_id = next(iter(tasks.load(self.directory)["tasks"]))
        self.tree = self.root / ".worktrees/candidate"
        git(self.root, "worktree", "add", "--detach", str(self.tree), head)
        (self.tree / ".unity").mkdir()
        (self.tree / ".unity/optimize-worktree.json").write_text(json.dumps({
            "schema": "optimize-worktree-v1", "project_root": str(self.root)}))
        (self.tree / ".unity/forum").symlink_to(self.root / ".unity/forum", target_is_directory=True)
        os.environ.update(UNITY_OPTIMIZE_PROFILE="optimizing", UNITY_OPTIMIZE_PHASE_DISPATCH_ID="",
            UNITY_OPTIMIZE_BASE_SHA=head, UNITY_OPTIMIZE_TASK_ID=self.task_id,
            UNITY_OPTIMIZE_WORKTREE=str(self.tree), UNITY_OPTIMIZE_PROJECT_ROOT=str(self.root))
        forum.configure(self.directory, self.root, "optimizing", author="Alice")
        strategy = forum.register_strategy("Alice", self.task_id, "Explicit shorter proof", "Offline fixture")
        self.assignment = forum.claim_strategy("Alice", strategy["strategy_id"])
        (self.tree / "A.lean").write_text("theorem one : True := True.intro\ntheorem two : True := by trivial\n")
        git(self.tree, "add", "A.lean")
        git(self.tree, "commit", "-qm", "Offline candidate with unchanged second declaration")
        return self.assignment

    def measurement_args(self, chunk_id, **overrides):
        return {"author": "Alice", "assignment_id": self.assignment["assignment_id"],
                "chunk_id": chunk_id, "raw_value": 0 if chunk_id == self.required[0] else 1,
                "method": self.method, "evidence": {"count": "Explicit fixture source inspection"}, **overrides}

    @staticmethod
    def claude_tool_message(raw):
        result = raw.model_dump(mode="json", by_alias=True, exclude_none=True)
        return {"type": "user", "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "offline-fixture", "content": result["content"],
            "mcpMeta": {"serverName": "unity-forum", "toolName": "offline-fixture",
                        "structuredContent": result.get("structuredContent")}}]}}

    async def sdk_messages(self, messages):
        """Use the installed SDK parser without connect(), CLI lookup or provider calls."""
        payload = "\n".join(json.dumps(row, ensure_ascii=True, separators=(",", ":")) for row in messages) + "\n"
        async def fragments():
            offset = 0
            while offset < len(payload):
                end = min(offset + 4096, len(payload))
                # The SDK strips chunk-edge whitespace; keep whitespace within a chunk.
                while end < len(payload) and (payload[end - 1].isspace() or payload[end].isspace()):
                    end += 1
                yield payload[offset:end]
                offset = end
        transport = SubprocessCLITransport("offline fixture", ClaudeAgentOptions())
        self.assertEqual(transport._max_buffer_size, 1024 * 1024)
        transport._process = SimpleNamespace(wait=AsyncMock(return_value=0))
        transport._stdout_stream = fragments()
        return [message async for message in transport.read_messages()]

    async def test_large_status_native_output_passes_actual_sdk_default_buffer(self):
        self.large_candidate_fixture()
        raw = await self.native_raw("optimize_status", {})
        message = self.claude_tool_message(raw)
        following = {"type": "result", "subtype": "success", "result": "next message retained"}
        self.assertEqual(await self.sdk_messages([message, following]), [message, following])
        encoded = raw.model_dump_json(by_alias=True, exclude_none=True).encode()
        self.assertLess(len(encoded), 256 * 1024)

    async def test_two_large_candidates_keep_comparison_provenance_and_review_bindings(self):
        task_id, first = self.large_candidate_fixture()
        comparison = {"subject": task_id, "before": 4, "after": 1, "improved": True,
                      "regressed": False, "evidence_kind": "agent_judged"}
        with tasks.transaction(self.directory) as state:
            state["candidates"]["candidate-1"]["verification"]["comparisons"] = {task_id: comparison}
            second = deepcopy(first)
            second.update(candidate_id="candidate-2", author="Bob")
            second["verification"]["comparisons"] = {task_id: {**comparison, "evidence_kind": "measured"}}
            state["candidates"]["candidate-2"] = second
            state["review"] = {"reviewer": "Alice", "snapshot": {
                **deepcopy(first["verification"]), "snapshot_id": "current-snapshot", "sha256": "snapshot-hash",
                "main_sha": "base", "config_revision": 0},
                "repair_requests": [{"task_ids": [task_id], "reason": "Inspect exact patch"}]}
        before = tasks.load(self.directory)
        response = await self.assert_bounded_native(await self.native_raw("optimize_status", {}))
        self.assertEqual(response["candidate_counts"], {"checked": 2})
        for summary in response["candidates"]:
            stored = before["candidates"][summary["candidate_id"]]
            self.assertEqual(summary["comparisons"], stored["verification"]["comparisons"])
            self.assertEqual(self.artifact_value(summary["records"]["candidate"]), stored)
        self.assertEqual(response["review"]["reviewer"], "Alice")
        for key in ("snapshot_id", "sha256", "main_sha", "config_revision", "passed"):
            self.assertEqual(response["review"]["snapshot"][key], before["review"]["snapshot"][key])
        self.assertEqual(response["review"]["repair_request_count"], 1)
        self.assertEqual(self.artifact_value(response["records"]["review"]), before["review"])
        self.assertEqual(tasks.load(self.directory), before)

    def artifact_value(self, record):
        payload = artifacts.artifact_bytes(self.root / ".unity/artifacts", record["artifact_id"])
        self.assertEqual(len(payload), record["bytes"])
        self.assertEqual(hashlib.sha256(payload).hexdigest(), record["sha256"])
        return json.loads(payload)

    def response_index(self, response):
        return self.artifact_value(response["records"]["response"]) if response.get("summary_only") else response

    def semantic_response(self, value):
        # Artifact IDs are intentionally fresh per call; compare exact referenced content instead.
        if isinstance(value, dict):
            if {"artifact_id", "sha256", "bytes"} <= value.keys():
                return {"artifact_value": self.semantic_response(self.artifact_value(value))}
            return {key: self.semantic_response(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.semantic_response(item) for item in value]
        return value

    async def assert_bounded_native(self, raw):
        self.assertEqual(forum.MAX_RESPONSE_BYTES, 65_536)
        result = raw.structuredContent
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=True).encode()), forum.MAX_RESPONSE_BYTES)
        self.assertLess(len(raw.model_dump_json(by_alias=True, exclude_none=True).encode()), 256 * 1024)
        message = self.claude_tool_message(raw)
        following = {"type": "result", "subtype": "success", "result": "subsequent message"}
        self.assertEqual(await self.sdk_messages([message, following]), [message, following])
        return result

    async def test_actual_sdk_rejects_legacy_oversized_structured_content_even_with_short_text(self):
        _, candidate = self.large_candidate_fixture()
        # Text-only compaction is insufficient: Claude also emits the structured payload.
        message = {"type": "user", "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "legacy-fixture", "content": "Short preview",
            "mcpMeta": {"structuredContent": {"candidates": [candidate]}}}]}}
        self.assertGreater(len(json.dumps(message)), 1024 * 1024)
        with self.assertRaisesRegex(CLIJSONDecodeError, "1048576"):
            await self.sdk_messages([message])

    async def test_status_task_and_brief_preserve_exact_records_without_state_or_source_mutation(self):
        task_id, candidate = self.large_candidate_fixture()
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.root, text=True, capture_output=True,
                                  check=True).stdout
        (self.root / ".gitignore").write_text(".unity/\nprivate-worker/\n")
        git("init", "-q")
        git("add", "A.lean", "B.lean", ".gitignore")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-qm", "Read-only response source fixture")
        git_before = (git("rev-parse", "HEAD", "HEAD^{tree}"), git("status", "--porcelain", "--untracked-files=all"))
        state_path = self.directory / tasks.STATE_FILE
        before_bytes = state_path.read_bytes()
        before = tasks.load(self.directory)
        source_before = {path.name: path.read_bytes() for path in self.root.glob("*.lean")}
        for tool, args in (("optimize_status", {}), ("optimize_task", {"task_id": task_id}), ("optimize_brief", {})):
            with self.subTest(tool=tool):
                response = await self.assert_bounded_native(await self.native_raw(tool, args))
                index = self.response_index(response)
                records = index["records"]
                expected_candidates = ([row for row in before["candidates"].values() if row["task_id"] == task_id]
                                       if tool == "optimize_task" else before["candidates"])
                self.assertEqual(self.artifact_value(records["candidates"]), expected_candidates)
                if tool != "optimize_task":
                    for key in ("tasks", "rounds", "review"):
                        self.assertEqual(self.artifact_value(records[key]), before[key])
                if tool != "optimize_brief":
                    summary = next(row for row in index["candidates"] if row["candidate_id"] == candidate["candidate_id"])
                    for field in ("candidate_id", "task_id", "author", "status", "base_main_sha", "task_revision", "config_revision"):
                        self.assertEqual(summary[field], candidate[field])
                    self.assertEqual(self.artifact_value(summary["records"]["candidate"]), candidate)
                self.assertEqual(state_path.read_bytes(), before_bytes)
                self.assertEqual({path.name: path.read_bytes() for path in self.root.glob("*.lean")}, source_before)
                self.assertEqual((git("rev-parse", "HEAD", "HEAD^{tree}"),
                                  git("status", "--porcelain", "--untracked-files=all")), git_before)

    async def test_large_non_candidate_fields_fall_back_to_bounded_exact_index(self):
        task_id, _ = self.large_candidate_fixture()
        with tasks.transaction(self.directory) as state:
            original = deepcopy(state["candidates"]["candidate-1"])
            original["verification"] = {"passed": True}
            state["candidates"] = {f"candidate-{index}": {**deepcopy(original), "candidate_id": f"candidate-{index}"}
                                   for index in range(1, 241)}
            state["tasks"][task_id]["reason"] = "long rationale\\\"\n" * 12000
            state["rounds"]["history"] = [{"number": index, "feedback": "round detail" * 1000} for index in range(40)]
            state["review"] = {"snapshot": {"notes": "review evidence" * 30000}}
            state["metric"]["definition"] = "Unicode metric 雪\\\"\n" * 3000
        before = tasks.load(self.directory)
        for tool, args in (("optimize_status", {}), ("optimize_task", {"task_id": task_id})):
            with self.subTest(tool=tool):
                response = await self.assert_bounded_native(await self.native_raw(tool, args))
                self.assertTrue(response["summary_only"])
                index = self.response_index(response)
                self.assertEqual(len(index["candidates"]), len(before["candidates"]))
                expected_candidates = list(before["candidates"].values()) if tool == "optimize_task" else before["candidates"]
                self.assertEqual(self.artifact_value(index["records"]["candidates"]), expected_candidates)
        self.assertEqual(tasks.load(self.directory), before)

    async def test_compact_views_keep_scope_chunk_locations_and_exact_long_ids_and_scores(self):
        task_id, candidate = self.large_candidate_fixture()
        long_id, long_score = "task-" + "x" * 700, "qualitative exact value " * 50
        with tasks.transaction(self.directory) as state:
            task = state["tasks"].pop(task_id)
            task["task_id"] = long_id
            state["tasks"][long_id] = task
            state["candidates"][candidate["candidate_id"]]["task_id"] = long_id
            state["candidates"][candidate["candidate_id"]]["measurements"] = {
                task["chunk_ids"][0]: {"raw_value": long_score}}
            state["config"].pop("targets")
        before = tasks.load(self.directory)
        status = await self.assert_bounded_native(await self.native_raw("optimize_status", {}))
        self.assertEqual(status["scope"]["targets"], before["scope"]["targets"])
        response = await self.assert_bounded_native(await self.native_raw("optimize_task", {"task_id": long_id}))
        self.assertEqual(response["task"]["task_id"], long_id)
        self.assertEqual(response["candidates"][0]["task_id"], long_id)
        self.assertEqual(next(iter(response["candidates"][0]["measurements"].values()))["raw_value"], long_score)
        for chunk in response["chunks"]:
            original = before["chunks"][chunk["chunk_id"]]
            for field in ("name", "path", "start_line", "end_line"):
                self.assertEqual(chunk[field], original[field])
        self.assertEqual(tasks.load(self.directory), before)

    async def test_oversized_unicode_identifier_is_exact_in_fallback_index_not_clipped(self):
        _, original = self.large_candidate_fixture()
        candidate_id = "candidate-" + "雪" * 12000
        with tasks.transaction(self.directory) as state:
            candidate = state["candidates"].pop(original["candidate_id"])
            candidate["candidate_id"] = candidate_id
            state["candidates"][candidate_id] = candidate
        response = await self.assert_bounded_native(await self.native_raw("select_candidate", {
            "author": "Alice", "candidate_id": candidate_id, "reason": "Exact identifier fixture"}))
        self.assertTrue(response["summary_only"])
        self.assertNotIn("candidate_id", response)
        index = self.response_index(response)
        self.assertEqual(index["candidate_id"], candidate_id)
        self.assertEqual(self.artifact_value(index["records"]["candidate"]),
                         tasks.load(self.directory)["candidates"][candidate_id])

    async def test_candidate_artifact_pages_are_exact_bounded_and_snapshot_stable(self):
        _, candidate = self.large_candidate_fixture()
        response = await self.native("optimize_status", {})
        index = self.response_index(response)
        record = index["candidates"][0]["records"]["candidate"]
        parts, messages, offset = [], [], 0
        async with Client(forum.build_server(forum.PROFILE)) as client:
            while True:
                raw = await client.call_tool_mcp("artifact_read", {"artifact_id": record["artifact_id"],
                                                                   "offset": offset, "limit": 32000})
                self.assertFalse(raw.isError)
                page = raw.structuredContent
                self.assertLessEqual(page["returned_bytes"], 32000)
                self.assertLess(len(raw.model_dump_json(by_alias=True).encode()), 256 * 1024)
                messages.append(self.claude_tool_message(raw))
                parts.append(page["content"])
                if page["next_offset"] is None:
                    break
                self.assertGreater(page["next_offset"], offset)
                offset = page["next_offset"]
        payload = "".join(parts).encode()
        self.assertEqual(len(payload), record["bytes"])
        self.assertEqual(hashlib.sha256(payload).hexdigest(), record["sha256"])
        self.assertEqual(json.loads(payload), candidate)
        self.assertGreater(len(messages), 1)
        following = {"type": "result", "result": "all pages read"}
        self.assertEqual(await self.sdk_messages([*messages, following]), [*messages, following])
        with tasks.transaction(self.directory) as state:
            state["candidates"][candidate["candidate_id"]]["notes"] = "Later live change"
        self.assertEqual(self.artifact_value(record), candidate)

    async def test_native_and_fi_bridge_have_same_compact_candidate_view(self):
        task_id, _ = self.large_candidate_fixture()
        for tool, args in (("optimize_status", {}), ("optimize_task", {"task_id": task_id})):
            with self.subTest(tool=tool):
                native = await self.native(tool, args)
                output = io.StringIO()
                with redirect_stdout(output):
                    shell = await bridge.call_tool("forum", tool, args, cwd=self.tree)
                self.assertEqual(self.semantic_response(native), self.semantic_response(shell.structured_content))
                self.assertLess(len(output.getvalue().encode()), 65_536)
                self.assertNotIn("Module11999.lean", output.getvalue())

    async def test_select_returns_compact_receipt_after_exactly_one_unchanged_selection_mutation(self):
        _, original = self.large_candidate_fixture()
        before = tasks.load(self.directory)
        response = await self.assert_bounded_native(await self.native_raw("select_candidate", {
            "author": "Alice", "candidate_id": original["candidate_id"], "reason": "Best checked proof"}))
        after = tasks.load(self.directory)
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(len(after["events"]), len(before["events"]) + 1)
        self.assertEqual(after["events"][-1]["kind"], "candidate_selected")
        candidate = after["candidates"][original["candidate_id"]]
        self.assertEqual(candidate["status"], "selected")
        self.assertEqual(candidate["selection"]["author"], "Alice")
        self.assertEqual(candidate["verification"], original["verification"])
        self.assertEqual(self.artifact_value(response["records"]["candidate"]), candidate)
        self.assertEqual(response["candidate_id"], original["candidate_id"])
        self.assertEqual(response["status"], "selected")
        for key in ("tasks", "rounds", "review", "baseline", "measurements", "config", "main_sha"):
            self.assertEqual(after[key], before[key])

    async def test_submit_returns_compact_receipt_without_truncating_stored_measurements(self):
        assignment = self.measurement_fixture()
        measurements = {cid: forum.measure_candidate(**self.measurement_args(cid,
            evidence={"retained_details": "Unicode exact evidence 雪\\\"\n" * 3000})) for cid in self.required}
        before = tasks.load(self.directory)
        response = await self.assert_bounded_native(await self.native_raw("submit_candidate", {
            "author": "Alice", "assignment_id": assignment["assignment_id"], "notes": "Shorter checked fixture"}))
        after = tasks.load(self.directory)
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(len(after["events"]), len(before["events"]) + 1)
        self.assertEqual(after["events"][-1]["kind"], "candidate_submitted")
        self.assertEqual(len(after["candidates"]), len(before["candidates"]) + 1)
        candidate = after["candidates"][response["candidate_id"]]
        self.assertEqual(candidate["status"], "submitted")
        self.assertEqual(candidate["measurements"], measurements)
        self.assertEqual(self.artifact_value(response["records"]["candidate"]), candidate)
        for key in ("rounds", "review", "baseline", "measurements", "config", "main_sha"):
            self.assertEqual(after[key], before[key])

    async def test_grouped_task_measurement_errors_are_bounded_actionable_and_recoverable(self):
        assignment = self.measurement_fixture()
        arguments = {"author": "Alice", "assignment_id": assignment["assignment_id"],
                     "notes": "PRIVATE-RAW-DIAGNOSTIC-MUST-NOT-BE-COPIED"}
        for measurements in ({}, {cid: 0 for cid in self.required}):
            before = (self.directory / tasks.STATE_FILE).read_bytes()
            async with Client(forum.build_server("optimizing")) as client:
                raw = await client.call_tool_mcp("submit_candidate", {**arguments, "measurements": measurements})
            self.assertTrue(raw.isError)
            text = "\n".join(block.text for block in raw.content)
            self.assertIn("candidate_measurement_validation", text)
            self.assertIn("same assignment", text)
            self.assertIn("artifact-", text)
            self.assertNotIn("PRIVATE-RAW-DIAGNOSTIC", text)
            self.assertLess(len(text.encode()), 12000)
            message = self.claude_tool_message(raw)
            following = {"type": "result", "result": "Author can correct the evidence"}
            self.assertEqual(await self.sdk_messages([message, following]), [message, following])
            self.assertEqual((self.directory / tasks.STATE_FILE).read_bytes(), before)
            with self.assertRaisesRegex(ToolError, "same assignment"):
                with redirect_stdout(io.StringIO()):
                    await bridge.call_tool("forum", "submit_candidate", {**arguments, "measurements": measurements}, cwd=self.tree)
            self.assertEqual((self.directory / tasks.STATE_FILE).read_bytes(), before)
        before = (self.directory / tasks.STATE_FILE).read_bytes()
        with self.assertRaisesRegex(ToolError, "candidate_measurement_validation"):
            await self.native("measure_candidate", self.measurement_args(self.required[0],
                method={"description": "PRIVATE-RAW-DIAGNOSTIC new wording", "direction": "lower"}))
        self.assertEqual((self.directory / tasks.STATE_FILE).read_bytes(), before)
        await self.native("measure_candidate", self.measurement_args(self.required[0]))
        before = (self.directory / tasks.STATE_FILE).read_bytes()
        with self.assertRaisesRegex(ToolError, "same assignment"):
            await self.native("submit_candidate", arguments)
        self.assertEqual((self.directory / tasks.STATE_FILE).read_bytes(), before)
        await self.native("measure_candidate", self.measurement_args(self.required[1]))
        result = await self.native("submit_candidate", arguments)
        state = tasks.load(self.directory)
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(len(state["candidates"]), 1)
        self.assertEqual(set(state["candidates"][result["candidate_id"]]["measurements"]), set(self.required))
        for record in artifacts.list_artifacts(self.root / ".unity/artifacts"):
            if record["kind"] == "optimize_measurement_validation":
                text = artifacts.artifact_bytes(self.root / ".unity/artifacts", record["artifact_id"]).decode()
                self.assertNotIn("PRIVATE-RAW-DIAGNOSTIC", text)
                self.assertEqual(json.loads(text)["schema"], "optimize-measurement-validation-v1")

    async def test_task_measurement_contract_is_exact_task_local_and_bounded(self):
        self.measurement_fixture()
        before = tasks.load(self.directory)
        native = await self.assert_bounded_native(await self.native_raw("optimize_task", {"task_id": self.task_id}))
        contract = self.response_index(native)["measurement_contract"]
        self.assertEqual(contract["required_chunk_ids"], self.required)
        self.assertIn("Follow error-specific guidance", contract["instructions"])
        self.assertIn("preserve private work", contract["instructions"])
        self.assertIn("end the turn for normal controller handling", contract["instructions"])
        self.assertIn("do not reset, rebind, update source or claim no_improvement", contract["instructions"])
        assignment = self.response_index(native)["assignments"][0]
        self.assertEqual((assignment["required_count"], assignment["recorded_count"], assignment["missing_count"]), (2, 0, 2))
        self.assertFalse(assignment["measurement_map_malformed"])
        self.assertIn("not measurement validity or acceptance", assignment["coverage_qualification"])
        for kind, source in (("original", before["baseline"]["measurements"]), ("current", before["measurements"])):
            self.assertEqual(set(contract[kind]), set(self.required))
            for cid in self.required:
                self.assertEqual(contract[kind][cid]["method"], source[cid]["method"])
                self.assertEqual(contract[kind][cid]["method_sha256"], source[cid]["method_sha256"])
        full = self.artifact_value(self.response_index(native)["records"]["measurement_contract"])
        self.assertEqual(full["original"], before["baseline"]["measurements"])
        with redirect_stdout(io.StringIO()):
            shell = await bridge.call_tool("forum", "optimize_task", {"task_id": self.task_id}, cwd=self.tree)
        self.assertEqual(self.semantic_response(native), self.semantic_response(shell.structured_content))
        self.assertEqual(tasks.load(self.directory), before)
        for records, recorded_count, missing_count, malformed in (
                ({self.required[0]: 7, "not-an-assigned-chunk": 9}, 1, 1, False),
                ({cid: None for cid in self.required}, 2, 0, False),
                ([self.required[0]], 0, 2, True)):
            with self.subTest(records=records):
                with tasks.transaction(self.directory) as state:
                    state["assignments"][self.assignment["assignment_id"]]["measurements"] = records
                state_before = tasks.load(self.directory)
                raw = await self.native_raw("optimize_task", {"task_id": self.task_id})
                response = self.response_index(await self.assert_bounded_native(raw))
                summary = response["assignments"][0]
                self.assertEqual((summary["required_count"], summary["recorded_count"], summary["missing_count"]),
                                 (2, recorded_count, missing_count))
                self.assertEqual(summary["measurement_map_malformed"], malformed)
                self.assertEqual(self.artifact_value(response["records"]["assignments"])[0]["measurements"], records)
                self.assertEqual(tasks.load(self.directory), state_before)

    async def test_source_invalidation_errors_preserve_work_instead_of_repairing_assignment(self):
        self.measurement_fixture()
        arguments = {"author": "Alice", "assignment_id": self.assignment["assignment_id"],
                     "notes": "Preserve candidate work"}
        before = (self.directory / tasks.STATE_FILE).read_bytes()
        source_before = (self.tree / "A.lean").read_bytes()
        for code in ("assignment_base_changed", "current_source_changed",
                     "source_changed_during_measurement", "source_changed_during_submission"):
            with self.subTest(code=code):
                error = tasks.MeasurementValidationError([{"code": code}])
                with patch.object(tasks, "submit_candidate", side_effect=error):
                    async with Client(forum.build_server("optimizing")) as client:
                        raw = await client.call_tool_mcp("submit_candidate", arguments)
                self.assertTrue(raw.isError)
                text = "\n".join(block.text for block in raw.content)
                payload = json.loads(text[text.index("{"):])
                self.assertLess(len(text.encode()), 12000)
                if code in {"assignment_base_changed", "current_source_changed"}:
                    self.assertIn("Preserve private work", payload["next_action"])
                    self.assertIn("report the obstacle", payload["next_action"])
                    self.assertIn("end this turn", payload["next_action"])
                    self.assertIn("normal controller handling", payload["next_action"])
                    self.assertIn("Do not reset, rebind or update source", payload["next_action"])
                    self.assertIn("do not claim no_improvement", payload["next_action"])
                    self.assertNotIn("same assignment", text)
                else:
                    self.assertIn("tree is stable", payload["next_action"])
                    self.assertIn("measure every required chunk again", payload["next_action"])
                    self.assertIn("same assignment", payload["next_action"])
                self.assertEqual((self.directory / tasks.STATE_FILE).read_bytes(), before)
                self.assertEqual((self.tree / "A.lean").read_bytes(), source_before)

    async def test_historical_raw_integer_measurements_do_not_break_status_or_task(self):
        task_id, original = self.large_candidate_fixture()
        chunk_id = tasks.load(self.directory)["tasks"][task_id]["chunk_ids"][0]
        with tasks.transaction(self.directory) as state:
            state["candidates"][original["candidate_id"]].update(status="rejected", measurements={chunk_id: 7})
        before = tasks.load(self.directory)
        for tool, args in (("optimize_status", {}), ("optimize_task", {"task_id": task_id})):
            response = await self.assert_bounded_native(await self.native_raw(tool, args))
            candidate = self.response_index(response)["candidates"][0]
            self.assertEqual(candidate["measurements"][chunk_id], {"malformed": True, "value_type": "int"})
            self.assertEqual(self.artifact_value(candidate["records"]["candidate"])["measurements"], {chunk_id: 7})
        self.assertEqual(tasks.load(self.directory), before)

    async def test_large_exact_method_contract_uses_existing_artifact_paging(self):
        task_id, _ = self.large_candidate_fixture()
        chunk_id = tasks.load(self.directory)["tasks"][task_id]["chunk_ids"][0]
        method = {"description": "Exact Unicode convention 雪\\\"\n" * 5000, "direction": "lower"}
        with tasks.transaction(self.directory) as state:
            state["measurements"][chunk_id] = {"subject": chunk_id, "method": method,
                                                "method_sha256": metrics.digest(method)}
        before = tasks.load(self.directory)
        response = await self.assert_bounded_native(await self.native_raw("optimize_task", {"task_id": task_id}))
        self.assertTrue(response["summary_only"])
        index = self.response_index(response)
        self.assertEqual(index["measurement_contract"]["current"][chunk_id]["method"], method)
        exact = self.artifact_value(index["records"]["measurement_contract"])
        self.assertEqual(exact["current"][chunk_id], before["measurements"][chunk_id])
        self.assertEqual(tasks.load(self.directory), before)

    async def test_compact_selection_does_not_relax_author_phase_or_stale_checks(self):
        _, candidate = self.large_candidate_fixture()
        args = {"author": "Alice", "candidate_id": candidate["candidate_id"], "reason": "Fixture reason"}
        for field, invalid, expected in (("status", "submitted", "controller-checked"),
                ("verification", {"passed": False}, "controller-checked"),
                ("base_main_sha", "old", "stale"), ("config_revision", 3, "stale"),
                ("task_revision", 3, "stale")):
            with self.subTest(field=field):
                with tasks.transaction(self.directory) as state:
                    state["candidates"][candidate["candidate_id"]] = {**deepcopy(candidate), field: invalid}
                before = (self.directory / tasks.STATE_FILE).read_bytes()
                with self.assertRaisesRegex(ToolError, expected):
                    await self.native("select_candidate", args)
                self.assertEqual((self.directory / tasks.STATE_FILE).read_bytes(), before)
        with tasks.transaction(self.directory) as state:
            state["candidates"][candidate["candidate_id"]] = deepcopy(candidate)
        forum.configure(self.directory, self.root, "optimizing", author="Bob")
        with patch.dict(os.environ, {"UNITY_AGENT_NAME": "Bob", "UNITY_OPTIMIZE_AUTHOR": "Bob"}):
            before = (self.directory / tasks.STATE_FILE).read_bytes()
            with self.assertRaisesRegex(ToolError, "configured primary"):
                await self.native("select_candidate", {**args, "author": "Bob"})
            self.assertEqual((self.directory / tasks.STATE_FILE).read_bytes(), before)
        forum.configure(self.directory, self.root, "critic", author="Alice")
        async with Client(forum.build_server("critic")) as client:
            self.assertNotIn("select_candidate", {tool.name for tool in await client.list_tools()})

    async def test_phase_brief_and_task_lookup_do_not_invent_a_task(self):
        brief = await self.native("optimize_brief", {})
        self.assertEqual(brief["turn_kind"], "phase")
        self.assertEqual(brief["bound_task"], "")
        self.assertEqual(brief["phase_dispatch_id"], self.dispatch["dispatch_id"])
        self.assertEqual(tasks.load(self.directory)["tasks"], {})
        with self.assertRaisesRegex(ToolError, "phase names are not tasks"):
            await self.native("optimize_task", {"task_id": "chunking"})
        with patch.dict(os.environ, {"UNITY_OPTIMIZE_TASK_ID": "chunking"}):
            with self.assertRaisesRegex(ToolError, "phase names are not tasks"):
                await self.native("optimize_brief", {})

    async def test_phase_communications_bind_author_phase_and_dispatch(self):
        for tool, key in (("publish_finding", "finding"), ("report_obstacle", "obstacle"),
                          ("ask_question", "question")):
            args = {"author": "Alice", "task_id": "", key: "Concrete preparation observation"}
            if tool != "ask_question":
                args["evidence"] = "Read source and checked inventory"
            event = await self.native(tool, args)
            self.assertEqual(event["phase"], "chunking")
            self.assertEqual(event["phase_dispatch_id"], self.dispatch["dispatch_id"])
            self.assertEqual(event["task_id"], "")
        with self.assertRaisesRegex(ToolError, "bound worker"):
            await self.native("ask_question", {"author": "Bob", "task_id": "", "question": "spoof"})
        with self.assertRaisesRegex(ToolError, "use task_id='' for phase"):
            await self.native("ask_question", {"author": "Alice", "task_id": "chunking", "question": "bad id"})

    async def test_phase_result_requires_current_bound_dispatch_and_author(self):
        arguments = {"author": "Alice", "status": "blocked", "summary": "Plan needs an inventory page",
                     "evidence": {"inventory": "not yet fetched"}}
        result = await self.native("report_phase_result", arguments)
        self.assertEqual(result["status"], "blocked")
        state = tasks.load(self.directory)
        self.assertEqual(state["phase"], "chunking")
        self.assertFalse(state["baseline_sealed"])
        self.assertEqual(state["rounds"]["started"], 0)
        status = await self.native("optimize_status", {})
        self.assertEqual(status["phase_dispatches"]["recent"][-1]["results"][0]["summary"], arguments["summary"])
        with self.assertRaisesRegex(ToolError, "bound worker"):
            await self.native("report_phase_result", {**arguments, "author": "Bob"})
        with patch.dict(os.environ, {"UNITY_OPTIMIZE_PHASE_DISPATCH_ID": ""}):
            with self.assertRaisesRegex(ToolError, "bound phase dispatch"):
                await self.native("report_phase_result", arguments)
        tasks.close_phase_dispatch(self.directory, self.dispatch["dispatch_id"], "blocked")
        with self.assertRaisesRegex(ToolError, "running dispatch"):
            await self.native("report_phase_result", arguments)

    async def test_architect_and_exploration_have_phase_bindings_and_only_architect_can_skip(self):
        tasks.close_phase_dispatch(self.directory, self.dispatch["dispatch_id"], "complete")
        for profile in ("architect", "exploration"):
            with tasks.transaction(self.directory) as state:
                state["phase"] = profile
            dispatch = tasks.begin_phase_dispatch(self.directory, profile, ["Alice"])
            with patch.dict(os.environ, {"UNITY_OPTIMIZE_PHASE_DISPATCH_ID": dispatch["dispatch_id"]}):
                forum.configure(self.directory, self.root, profile, author="Alice")
                brief = await self.native("optimize_brief", {})
                self.assertEqual(brief["bound_task"], "")
                self.assertEqual(brief["turn_kind"], "phase")
                event = await self.native("publish_finding", {"author": "Alice", "task_id": "",
                    "finding": "Checked the available source inventory", "evidence": "A.lean/B.lean"})
                self.assertEqual(event["phase"], profile)
                args = {"author": "Alice", "status": "skipped", "summary": "Optional setup unnecessary",
                        "evidence": "Existing matching toolchain needs no Architect setup"}
                if profile == "architect":
                    self.assertEqual((await self.native("report_phase_result", args))["status"], "skipped")
                else:
                    with self.assertRaisesRegex(ToolError, "only optional architect"):
                        await self.native("report_phase_result", args)
                    await self.native("report_phase_result", {**args, "status": "complete"})
            tasks.close_phase_dispatch(self.directory, dispatch["dispatch_id"], "complete")

    async def test_real_task_binding_remains_valid(self):
        state = tasks.load(self.directory)
        proposal = tasks.propose_plan(self.directory, "Alice", [
            {"chunk_ids": [cid]} for cid in state["chunks"]], "All declarations for binding fixture")
        tasks.accept_plan(self.directory, proposal["proposal_id"])
        task_id = next(iter(tasks.load(self.directory)["tasks"]))
        with patch.dict(os.environ, {"UNITY_OPTIMIZE_TASK_ID": task_id}):
            brief = await self.native("optimize_brief", {})
            self.assertEqual(brief["turn_kind"], "task")
            self.assertEqual(brief["bound_task"], task_id)
            self.assertEqual((await self.native("optimize_task", {"task_id": task_id}))["task"]["task_id"], task_id)

    async def test_inventory_native_and_bridge_agree_and_reject_stale_hash(self):
        args = {"path": "A.lean", "limit": 1}
        native = await self.native("optimize_inventory", args)
        with redirect_stdout(io.StringIO()):
            shell = await bridge.call_tool("forum", "optimize_inventory", args, cwd=self.tree)
        self.assertEqual(native, shell.structured_content)
        self.assertEqual(native["matched"], 2)
        self.assertEqual(native["next_offset"], 1)
        second = await self.native("optimize_inventory", {**args, "offset": native["next_offset"],
            "inventory_sha256": native["inventory_sha256"]})
        self.assertIsNone(second["next_offset"])
        self.assertNotEqual(native["chunks"][0]["chunk_id"], second["chunks"][0]["chunk_id"])
        await self.native("ask_question", {"author": "Alice", "task_id": "", "question": "Unrelated event"})
        unchanged = await self.native("optimize_inventory", {**args, "inventory_sha256": native["inventory_sha256"]})
        self.assertEqual(unchanged["inventory_sha256"], native["inventory_sha256"])
        with tasks.transaction(self.directory) as state:
            state["chunks"][native["chunks"][0]["chunk_id"]]["source_sha256"] = "changed"
        with self.assertRaisesRegex(ToolError, "stale"):
            await self.native("optimize_inventory", {**args, "inventory_sha256": native["inventory_sha256"]})

    async def test_compact_brief_keeps_large_data_in_readable_exact_artifacts(self):
        with tasks.transaction(self.directory) as state:
            original = next(iter(state["chunks"].values()))
            state["chunks"] = {f"chunk-{index:05}": {**deepcopy(original), "chunk_id": f"chunk-{index:05}",
                "name": f"Example.theorem{index}", "path": "A.lean" if index < 50 else "B.lean"}
                for index in range(3020)}
            state["baseline_measurements"] = {"chunk-00000": {"evidence": "a" * 40000}}
        before = tasks.load(self.directory)
        brief = await self.native("optimize_brief", {})
        self.assertLess(len(json.dumps(brief).encode()), 12000)
        self.assertNotIn("chunks", brief)
        self.assertEqual(brief["inventory"]["count"], 3020)
        selected = await self.native("optimize_inventory", {"path": "A.lean"})
        self.assertEqual(selected["matched"], 50)
        record = brief["records"]["chunks"]
        pages, offset = [], 0
        while True:
            page = forum.artifact_read(record["artifact_id"], offset=offset, limit=32000)
            pages.append(page["content"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertEqual(json.loads("".join(pages)), before["chunks"])
        self.assertEqual(forum.artifact_info(record["artifact_id"])["sha256"], record["sha256"])
        full = {key: json.loads(artifacts.artifact_bytes(self.root / ".unity/artifacts", value["artifact_id"]))
                for key, value in brief["records"].items()}
        for key in ("baseline", "scope", "metric", "plan_proposals", "baseline_measurements"):
            self.assertEqual(full[key], before[key])
        self.assertEqual(tasks.load(self.directory), before)

    async def test_compact_exclusion_plan_reaches_validator_and_retains_build_scope(self):
        page = await self.native("optimize_inventory", {"path": "A.lean"})
        before = tasks.load(self.directory)
        result = await self.native("propose_chunk_plan", {"author": "Alice",
            "chunks": [{"chunk_ids": [row["chunk_id"]]} for row in page["chunks"]],
            "reason": "One declaration per task", "exclude_outside_paths": ["A.lean"],
            "exclusion_reason": "Only A.lean requested", "inventory_sha256": page["inventory_sha256"]})
        self.assertEqual(len(result["tasks"]), 2)
        self.assertEqual(len(result["exclusions"]), 1)
        after = tasks.load(self.directory)
        self.assertEqual(before["scope"], after["scope"])
        self.assertEqual(before["baseline"], after["baseline"])

    async def test_rejected_plan_has_bounded_details_artifact_without_state_mutation(self):
        before = tasks.load(self.directory)
        with self.assertRaises(ToolError) as error:
            await self.native("propose_chunk_plan", {"author": "Alice", "chunks": [],
                "reason": "PRIVATE-RAW-DIAGNOSTIC-MUST-NOT-BE-COPIED"})
        message = str(error.exception)
        self.assertIn("optimize_inventory", message)
        self.assertIn("artifact-", message)
        self.assertLess(len(message), 4000)
        self.assertEqual(tasks.load(self.directory), before)
        records = artifacts.list_artifacts(self.root / ".unity/artifacts")
        diagnostic = next(row for row in records if row["kind"] == "optimize_plan_validation")
        text = artifacts.artifact_bytes(self.root / ".unity/artifacts", diagnostic["artifact_id"]).decode()
        self.assertNotIn("PRIVATE-RAW-DIAGNOSTIC", text)
        self.assertEqual(json.loads(text)["schema"], "optimize-plan-validation-v1")

    async def test_native_catalog_and_shell_policy_match_without_phase_permission_expansion(self):
        from unity.optimize_spawn import _optimize_codex_tool_policy
        from unity.optimize_workflow import mcp_servers, paths_for
        for profile in forum.PROFILES:
            async with Client(forum.build_server(profile)) as client:
                names = {tool.name for tool in await client.list_tools()}
            policy = _optimize_codex_tool_policy(mcp_servers(paths_for(self.root), profile), profile)
            self.assertEqual(names, set(policy["unity-forum"]))
            self.assertIn("optimize_inventory", names)
            self.assertEqual("report_phase_result" in names, profile in {"architect", "exploration", "chunking"})
            self.assertEqual("submit_candidate" in names, profile == "optimizing")

    async def test_bridge_errors_explain_exact_names_and_current_catalog(self):
        with self.assertRaisesRegex(ValueError, "exact forum server name 'unity-forum' or alias 'forum'"):
            await bridge.call_tool("unity_forum", "optimize_brief", {}, cwd=self.tree)
        with self.assertRaisesRegex(ValueError, "Available tools for chunking/unity-forum:.*optimize_inventory"):
            await bridge.call_tool("forum", "submit_candidate", {}, cwd=self.tree)
        with redirect_stdout(io.StringIO()):
            result = await bridge.call_tool("unity-forum", "report_phase_result", {
                "author": "Alice", "status": "blocked", "summary": "Need measurement evidence",
                "evidence": "No original metric records yet"}, cwd=self.tree)
        self.assertEqual(result.structured_content["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
