"""Optimize lifecycle through real workers and fake transport; no service/build calls."""
import asyncio
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from unity import optimize_tasks as tasks, optimize_workflow as workflow
from unity.forum import optimize_server as forum
from unity.optimize_spawn import SpawnCleanupError


class OptimizeWorkflowLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="optimize-lifecycle-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.root / "A.lean").write_text("theorem a : True := by\n  have h : True := by trivial\n  exact h\n")
        (self.root / ".gitignore").write_text(".unity/\n.worktrees/\n.lake/\n")
        self.git("add", "A.lean", ".gitignore")
        self.git("commit", "-qm", "fixture")
        (self.root / ".unity").mkdir()
        (self.root / ".unity/agents.yaml").write_text("fixture")
        self.paths = workflow.paths_for(self.root)
        metric_path = self.root / ".unity/metrics/length.md"
        metric_path.parent.mkdir()
        metric_path.write_text((Path(workflow.metrics.__file__).parent / "defaults/metrics/length.md").read_text())
        self.definition = workflow.metrics.load_metric(self.root, "length")
        self.agent = SimpleNamespace(name="A")
        self.roster = SimpleNamespace(primary=self.agent, agents=[self.agent])
        tasks.initialize(self.paths.forum, self.root, run_id="fixture", base_sha=self.git("rev-parse", "HEAD"),
            metric=self.definition, scope={"targets": "Universe", "files": ["A.lean"], "modules": ["A"]},
            config={"primary": "A", "agents": ["A"], "max_rounds": 2, "lake_args": [], "real_lake": sys.executable})
        self.controller = workflow.Controller(self.paths, self.roster, max_attempts=2)
        self.started = []
        self.method = {"description": "Explicit offline fixture tactic count", "direction": "lower"}
        self.phases = []
        self.compiler_calls = []
        self.stop_next_writer = False
        self.real_job = workflow.jobs.run

    def fake_build(self, root, command, **kwargs):
        if command[0] == "git":
            return self.real_job(root, command, **kwargs)
        self.compiler_calls.append(command)
        self.assertEqual(command[0], "lake")
        self.assertNotIn("--version", command)
        return subprocess.CompletedProcess(command, 0, "Built A (offline fixture)\n", "")

    async def fake_worker(self, agent, task_id, tree, profile, interrupt, instruction=""):
        """Provider double exercises actual tools under the generated worker binding."""
        self.phases.append((profile, task_id))
        forum.configure(self.paths.forum, self.root, profile, author=agent.name)
        state = tasks.load(self.paths.forum)
        if profile in {"architect", "exploration"}:
            forum.publish_finding(agent.name, "", "Offline setup/measurement-method finding", self.method["description"])
            forum.report_phase_result(agent.name, "skipped" if profile == "architect" else "complete",
                                      "Optional setup skipped" if profile == "architect" else "Exploration findings recorded",
                                      {"method": self.method})
            return
        if profile == "chunking":
            chunks = [row["chunk_id"] for row in forum.optimize_inventory()["chunks"]]
            forum.propose_chunk_plan(agent.name, [{"chunk_ids": [cid]} for cid in chunks],
                                     "Each declaration gets a task, including compiling declarations")
            for cid in chunks:
                forum.record_measurement(agent.name, cid, 3, self.method,
                                         {"count": "have; trivial; exact"}, baseline=True)
            forum.report_phase_result(agent.name, "complete", "Plan and original measurements submitted",
                                      {"chunk_ids": chunks})
        elif profile == "critic":
            snapshot = state["review"]["snapshot"]
            self.assertTrue(snapshot["passed"], snapshot["issues"])
            forum.submit_optimization_verdict(agent.name, snapshot["snapshot_id"], True,
                "Offline reviewer fixture: same theorem, shorter proof, current checked snapshot")
        elif instruction.startswith("Selection-only"):
            for candidate in state["candidates"].values():
                if candidate["status"] == "checked":
                    forum.select_candidate(agent.name, candidate["candidate_id"], "Strictly shorter checked proof")
        elif instruction.startswith("Measurement refresh"):
            for cid in state["baseline"]["measurements"]:
                forum.record_current_measurement(agent.name, cid, 1, self.method,
                                                 {"count": "trivial"})
        elif profile == "optimizing":
            strategy = tasks.register_strategy(self.paths.forum, agent.name, task_id, "Shorten proof",
                "Use trivial directly; fixture attempt " + str(len(state["assignments"])))
            tasks.claim_strategy(self.paths.forum, agent.name, strategy["strategy_id"])
            assignment = tasks.configure_assignment(self.paths.forum, agent.name, strategy["strategy_id"],
                                                    tree, state["main_sha"])
            if self.stop_next_writer:
                self.stop_next_writer = False
                (self.paths.unity / "stop-requested").write_text("offline stop fixture\n")
                await interrupt.wait()
                return
            (tree / "A.lean").write_text("theorem a : True := by trivial\n")
            workflow.git(tree, "add", "A.lean")
            workflow.git(tree, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                         "commit", "-m", "Fixture shorter proof")
            identity = workflow.verification.file_identity(tree, scope=state["scope"], lake_args=[])
            measured = {cid: workflow.metrics.record_measurement(self.definition, subject=cid, raw_value=1,
                source_identity=identity, method=self.method, evidence={"count": "trivial"})
                for cid in state["tasks"][task_id]["chunk_ids"]}
            tasks.submit_candidate(self.paths.forum, agent.name, assignment["assignment_id"],
                                   "Same theorem; remove intermediate have/exact", measured)
        else:
            self.fail("Unexpected fake provider profile " + profile)

    async def fake_spawn(self, agent, system, instruction, tree, servers, **kwargs):
        """Only the transport is replaced: Controller.worker constructs its real contract."""
        env = kwargs["env_overrides"]
        task_id = env["UNITY_OPTIMIZE_TASK_ID"] or None
        profile = env["UNITY_OPTIMIZE_PROFILE"]
        if task_id:
            self.assertIn(task_id, self.controller.state()["tasks"])
            self.assertIn("Work on task " + task_id, instruction)
        else:
            self.assertTrue(env["UNITY_OPTIMIZE_PHASE_DISPATCH_ID"])
            self.assertNotIn("Work on task " + profile, instruction)
        self.assertIsNone(kwargs.get("on_normal_completion"), "no unbounded corrective turn loop")
        with patch.dict(os.environ, env):
            return await self.fake_worker(agent, task_id, tree, profile, kwargs["interrupt_event"], instruction)

    def fake_runtime(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(workflow.jobs, "run", side_effect=self.fake_build))
        stack.enter_context(patch.object(workflow.workspace, "discover", return_value={
            "build_dir": ".lake/build", "modules": {"A.lean": "A"}, "unmatched": [], "issues": []}))
        stack.enter_context(patch.object(workflow, "spawn", side_effect=self.fake_spawn))
        return stack

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, text=True,
            capture_output=True, check=True).stdout.strip()

    async def asyncTearDown(self):
        for task in self.started:
            if not task.done(): task.cancel()
        if self.started:
            await asyncio.gather(*self.started, return_exceptions=True)

    def start(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.started.append(task)
        return task

    async def test_stop_waits_for_owned_worker_closure(self):
        interrupt, closed = asyncio.Event(), asyncio.Event()
        async def worker():
            await interrupt.wait()
            await asyncio.sleep(0)
            closed.set()
        task = self.start(worker())
        await workflow.stop_worker(task, interrupt, grace=0.1, hard=0.1)
        self.assertTrue(closed.is_set())
        self.assertTrue(task.done())

    async def test_cancel_cleanup_failure_keeps_exact_exception(self):
        fatal = SpawnCleanupError("fixture cleanup failure")
        entered = asyncio.Event()
        async def worker():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                raise fatal
        task = self.start(worker())
        await entered.wait()
        with self.assertRaises(SpawnCleanupError) as raised:
            await workflow.stop_worker(task, asyncio.Event(), grace=0.001, hard=0.1)
        self.assertIs(raised.exception, fatal)

    async def test_drain_cleanup_failure_does_not_release_or_reschedule(self):
        fatal = SpawnCleanupError("fixture retained group")
        async def worker():
            raise fatal
        task = self.start(worker())
        await asyncio.sleep(0)
        self.controller.workers["A"] = {"task": task, "interrupt": asyncio.Event(),
                                         "task_id": "t", "tree": self.root}
        with patch.object(self.controller, "reap_workers", new=AsyncMock()) as reap:
            with self.assertRaises(SpawnCleanupError) as raised:
                await self.controller.drain()
        self.assertIs(raised.exception, fatal)
        reap.assert_not_awaited()
        self.assertIn("A", self.controller.workers)

    async def test_runtime_env_installs_private_guard_and_bridge_without_service_probes(self):
        tree = workflow.task_worktree(self.paths, "A", "t")
        with patch("shutil.which", side_effect=AssertionError("unexpected tool lookup")):
            env = workflow.runtime_env(self.paths, "A", "t", "optimizing", tree, self.controller.state())
        first = Path(env["PATH"].split(os.pathsep)[0])
        self.assertIn("unity.optimize_lake_guard", (first / "lake").read_text())
        self.assertIn("unity.optimize_mcp_bridge", (first / "unity").read_text())
        self.assertEqual(env["UNITY_REAL_LAKE"], sys.executable)
        self.assertEqual(env["UNITY_OPTIMIZE_WORKTREE"], str(tree))
        self.assertEqual(env["UNITY_OPTIMIZE_WORKFLOW"], tasks.SCHEMA)
        self.assertEqual(env["UNITY_OPTIMIZE_PROJECT_ROOT"], str(self.root))
        self.assertNotIn("UNITY_BUMP_PROFILE", env)

    async def test_retrospective_cleanup_failure_is_not_optional_incomplete_result(self):
        fatal = SpawnCleanupError("fixture unresolved retrospective process")
        library = self.root / "library"
        library.mkdir()
        with patch("unity.library.ensure_library", return_value=library), \
                patch.object(self.controller, "dispatch_phase", new=AsyncMock(side_effect=fatal)):
            with self.assertRaises(SpawnCleanupError) as raised:
                await workflow.run_retrospective(self.controller)
        self.assertIs(raised.exception, fatal)
        self.assertEqual(self.controller.state()["phase"], "blocked")
        self.assertEqual(json.loads((self.paths.forum / "retrospective.json").read_text())["status"], "cleanup_failed")

    async def test_retrospective_canonical_drift_is_blocked_not_optional(self):
        with tasks.transaction(self.paths.forum) as state:
            state.update(phase="complete", final_report={"accepted_old_fixture": "retained"})
        async def changed(*args, **kwargs):
            (self.root / "A.lean").write_text("theorem a : False := by sorry\n")
            raise ValueError("retrospective changed read-only source")
        library = self.root / ".unity/library"
        library.mkdir()
        with patch("unity.library.ensure_library", return_value=library), \
                patch.object(self.controller, "dispatch_phase", new=changed):
            with self.assertRaisesRegex(ValueError, "canonical source or metric"):
                await workflow.run_retrospective(self.controller)
        state = self.controller.state()
        self.assertEqual(state["phase"], "blocked")
        self.assertEqual(state["prior_final_reports"], [{"accepted_old_fixture": "retained"}])
        self.assertEqual(workflow.verification.completion_report(state)["status"], "blocked")

    async def test_full_controller_run_prepares_integrates_reviews_and_finishes(self):
        original = (self.root / "A.lean").read_text()
        with self.fake_runtime():
            report = await workflow.run(self.paths, self.roster, metric="length", continue_=True,
                                        max_attempts=2, retrospective=False)
        state = self.controller.state()
        self.assertEqual(report["status"], "accepted", report)
        self.assertEqual(state["phase"], "complete")
        self.assertEqual(state["rounds"]["started"], 1)
        self.assertEqual(state["rounds"]["completed"], 1)
        self.assertEqual([c["status"] for c in state["candidates"].values()], ["merged"])
        self.assertEqual(state["review"]["verdict"]["author"], "A")
        self.assertEqual(set(p for p, _ in self.phases), {"architect", "exploration", "chunking", "optimizing", "critic"})
        self.assertNotEqual((self.root / "A.lean").read_text(), original)
        self.assertEqual(next(iter(state["baseline"]["measurements"].values()))["raw_value"], 3)
        self.assertEqual(next(iter(state["measurements"].values()))["raw_value"], 1)
        self.assertGreaterEqual(len(self.compiler_calls), 4)
        self.assertTrue(state["final_report"])

    async def test_stop_then_continue_retains_baseline_and_same_round(self):
        self.stop_next_writer = True
        with self.fake_runtime():
            stopped = await workflow.run(self.paths, self.roster, metric="length", continue_=True,
                                         max_attempts=2, retrospective=False)
            paused = self.controller.state()
            self.assertEqual(stopped["status"], "stopped")
            self.assertEqual(paused["phase"], "stopped")
            self.assertEqual(paused["rounds"]["started"], 1)
            self.assertEqual(paused["rounds"]["completed"], 0)
            baseline = json.dumps(paused["baseline"], sort_keys=True)
            self.assertTrue(all(row.get("closed") for row in paused["assignments"].values()))
            resumed = await workflow.run(self.paths, self.roster, metric="length", continue_=True,
                                         max_attempts=2, retrospective=False)
        state = self.controller.state()
        self.assertEqual(resumed["status"], "accepted", resumed)
        self.assertEqual(state["rounds"]["started"], 1)
        self.assertEqual(state["rounds"]["completed"], 1)
        self.assertEqual(json.dumps(state["baseline"], sort_keys=True), baseline)
        self.assertEqual([profile for profile, _ in self.phases].count("architect"), 1)
        self.assertEqual([profile for profile, _ in self.phases].count("chunking"), 1)
        self.assertFalse((self.paths.unity / "stop-requested").exists())
        self.assertEqual(len(state["rounds"]["history"][0]["interrupted_dispatches"]), 1)

    async def test_untracked_project_input_is_rejected_before_any_worker(self):
        extra = self.root / "Uncommitted.lean"
        extra.write_text("theorem extra : True := by trivial\n")
        with self.fake_runtime():
            with self.assertRaisesRegex(ValueError, "untracked project inputs"):
                await workflow.run(self.paths, self.roster, metric="length", continue_=True,
                                   max_attempts=2, retrospective=False)
        self.assertEqual(self.phases, [])
        self.assertEqual(self.compiler_calls, [])
        self.assertTrue(extra.exists())

    async def test_report_failure_does_not_mask_fatal_cleanup_error(self):
        fatal = SpawnCleanupError("fixture unresolved worker process")
        with patch.object(workflow.metrics, "load_metric", return_value=self.definition), \
                patch.object(workflow.Controller, "run", new=AsyncMock(side_effect=fatal)), \
                patch.object(workflow.verification, "persist_report", side_effect=OSError("fixture report write failure")):
            with self.assertRaises(SpawnCleanupError) as raised:
                await workflow.run(self.paths, self.roster, metric="length", continue_=True,
                                   max_attempts=2, retrospective=False)
        self.assertIs(raised.exception, fatal)


if __name__ == "__main__":
    unittest.main()
