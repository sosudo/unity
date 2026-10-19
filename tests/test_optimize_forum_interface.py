"""Offline model-visible Optimize contracts: actual FastMCP tools, no providers."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastmcp import Client
from fastmcp.exceptions import ToolError

from unity import artifacts, optimize_mcp_bridge as bridge, optimize_tasks as tasks
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
