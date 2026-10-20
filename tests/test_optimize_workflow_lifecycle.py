"""Optimize lifecycle through real workers and fake transport; no service/build calls."""
import asyncio
from copy import deepcopy
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

from fastmcp import Client

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
        request_id = os.getenv("UNITY_OPTIMIZE_SCORE_REQUEST_ID")
        if request_id:
            request = state["scoring_requests"][request_id]
            for cid in request["subjects"]:
                text = (tree / state["chunks"][cid]["path"]).read_text()
                value = 3 if "have h : True := by trivial" in text else 1
                forum.submit_score_report(agent.name, request_id, cid, value,
                    request["original_methods"].get(cid) or self.method, {"fixture": "known proof value"})
            return
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
            forum.report_phase_result(agent.name, "complete", "Declaration plan submitted",
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
            tasks.submit_candidate(self.paths.forum, agent.name, assignment["assignment_id"],
                                   "Same theorem; remove intermediate have/exact")
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
            self.assertTrue(env["UNITY_OPTIMIZE_PHASE_DISPATCH_ID"] or env["UNITY_OPTIMIZE_SCORE_REQUEST_ID"])
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

    def scoring_request(self):
        state = self.controller.state()
        return {"source_identity": state["baseline"]["identity"],
            "subjects": list(state["baseline"]["measurements"]),
            "original_methods": {cid: row["method"] for cid, row in state["baseline"]["measurements"].items()},
            "metric": state["metric"], "build_result": state["baseline"]["build"]}

    async def test_stop_during_verifier_scoring_closes_turn_without_using_round(self):
        with self.fake_runtime():
            await self.controller.prepare()
        baseline = deepcopy(self.controller.state()["baseline"])
        entered, closed = asyncio.Event(), asyncio.Event()
        async def scoring_turn(*args, **kwargs):
            self.assertTrue(kwargs["env_overrides"]["UNITY_OPTIMIZE_SCORE_REQUEST_ID"])
            entered.set()
            await kwargs["interrupt_event"].wait()
            closed.set()
        with patch.object(workflow, "spawn", side_effect=scoring_turn):
            pending = self.start(self.controller.judge_snapshot(self.scoring_request()))
            await entered.wait()
            (self.paths.unity / "stop-requested").write_text("offline scoring cancellation\n")
            with self.assertRaises(workflow.jobs.JobCancelled):
                await asyncio.wait_for(pending, 5)
        state = self.controller.state()
        self.assertTrue(closed.is_set())
        self.assertEqual(list(state["scoring_requests"].values())[-1]["status"], "interrupted")
        self.assertEqual(state["rounds"]["started"], 0)
        self.assertEqual(state["baseline"], baseline)
        self.assertEqual(state["candidates"], {})

    async def test_verifier_scoring_requires_reports_and_preserves_source(self):
        with self.fake_runtime():
            await self.controller.prepare()
        baseline = deepcopy(self.controller.state()["baseline"])
        async def missing(*args, **kwargs):
            return "All scores are good"
        with patch.object(workflow, "spawn", side_effect=missing):
            with self.assertRaisesRegex(ValueError, "without all requested"):
                await self.controller.judge_snapshot(self.scoring_request())
        async def edits_source(agent, system, instruction, tree, servers, **kwargs):
            (tree / "A.lean").write_text("theorem a : True := by trivial\n")
        with patch.object(workflow, "spawn", side_effect=edits_source):
            with self.assertRaisesRegex(ValueError, "read-only source"):
                await self.controller.judge_snapshot(self.scoring_request())
        state = self.controller.state()
        self.assertEqual([row["status"] for row in state["scoring_requests"].values()][-2:], ["blocked", "blocked"])
        self.assertEqual(state["baseline"], baseline)
        self.assertEqual(state["rounds"]["started"], 0)

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


class OptimizeSelectionSchedulingTests(unittest.IsolatedAsyncioTestCase):
    """Real multi-task scheduling; only transport and compiler execution are fake."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="optimize-selection-offline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.git(self.root, "init", "-q")
        self.git(self.root, "config", "user.name", "Fixture")
        self.git(self.root, "config", "user.email", "fixture@example.invalid")
        (self.root / ".gitignore").write_text(".unity/\n.worktrees/\n.lake/\n")
        self.files = {"task-" + name.lower(): name + ".lean" for name in "ABCD"}
        for name in "ABCD":
            (self.root / (name + ".lean")).write_text(
                f"theorem {name.lower()} : True := by\n  have h : True := by trivial\n  exact h\n")
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "-qm", "four independent declaration tasks")
        self.original_main = self.git(self.root, "rev-parse", "HEAD")
        self.paths = workflow.paths_for(self.root)
        metric_path = self.root / ".unity/metrics/length.md"
        metric_path.parent.mkdir(parents=True)
        metric_path.write_text((Path(workflow.metrics.__file__).parent / "defaults/metrics/length.md").read_text())
        (self.paths.unity / "agents.yaml").write_text("offline scheduling fixture")
        self.metric = workflow.metrics.load_metric(self.root, "length")
        self.primary, self.peer = SimpleNamespace(name="Primary"), SimpleNamespace(name="Peer")
        self.roster = SimpleNamespace(primary=self.primary, agents=[self.primary, self.peer])
        self.scope = {"targets": "Universe", "files": list(self.files.values()), "modules": list("ABCD")}
        self.method = {"description": "Explicit offline fixture tactic count", "direction": "lower"}
        self.timeline, self.selection_signatures, self.compiler_calls = [], [], []
        self.select_candidates = True
        self.writer_action = None
        self.real_job = workflow.jobs.run
        for replacement in (
            patch.object(workflow.jobs, "run", side_effect=self.fake_build),
            patch.object(workflow.workspace, "discover", return_value={
                "modules": {name + ".lean": name for name in "ABCD"}, "unmatched": [], "issues": []}),
            patch.object(workflow, "spawn", side_effect=self.fake_transport),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        tasks.initialize(self.paths.forum, self.root, run_id="selection-fixture", base_sha=self.original_main,
            metric=self.metric, scope=self.scope, config={"primary": "Primary", "agents": ["Primary", "Peer"],
                "max_rounds": 2, "lake_args": [], "real_lake": sys.executable})
        tasks.set_baseline(self.paths.forum, workflow.verification.capture_baseline(
            self.root, metric=self.metric, scope=self.scope))
        inventory = tasks.scan_sources(self.root, self.scope["files"])
        tasks.install_inventory(self.paths.forum, inventory)
        self.chunks = {tid: next(cid for cid, chunk in inventory["chunks"].items() if chunk["path"] == path)
                       for tid, path in self.files.items()}
        workflow.phase(self.paths, "chunking")
        forum.configure(self.paths.forum, self.root, "chunking", "Primary")
        plan = forum.propose_chunk_plan("Primary", [
            {"task_id": tid, "chunk_ids": [cid]} for tid, cid in self.chunks.items()
        ], "Independent declaration fixtures")
        tasks.accept_plan(self.paths.forum, plan["proposal_id"])
        for cid in self.chunks.values():
            tasks.record_baseline_measurement(self.paths.forum, "Primary", cid,
                workflow.metrics.record_measurement(self.metric, subject=cid, raw_value=3,
                    source_identity=tasks.load(self.paths.forum)["baseline"]["identity"],
                    method=self.method, evidence={"fixture": "known original proof"}))
        tasks.seal_baseline(self.paths.forum)
        with tasks.transaction(self.paths.forum) as state:
            state["measurements"] = deepcopy(state["baseline"]["measurements"])
        self.baseline = deepcopy(tasks.load(self.paths.forum)["baseline"])
        tasks.begin_round(self.paths.forum, max_rounds=2)
        self.controller = workflow.Controller(self.paths, self.roster, max_attempts=2)
        workflow.phase(self.paths, "optimizing")

    async def asyncTearDown(self):
        # These are fixture transport coroutines, never native worker processes.
        workers = [row["task"] for row in self.controller.workers.values()]
        for worker in workers:
            if not worker.done():
                worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    def git(self, root, *args):
        return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True,
                              check=True).stdout.strip()

    def fake_build(self, root, command, **kwargs):
        if command[0] == "git":
            return self.real_job(root, command, **kwargs)
        self.assertEqual(command[0], "lake", "no native or provider probes in offline regression")
        self.assertNotIn("--version", command)
        self.compiler_calls.append(command)
        return subprocess.CompletedProcess(command, 0, "Built fixture modules\n", "")

    def writer_result(self, author, task_id, tree, env, *, candidate=False):
        # Keep process-global forum/environment bindings synchronous: transports may overlap.
        with patch.dict(os.environ, env):
            forum.configure(self.paths.forum, self.root, "optimizing", author)
            strategy = forum.register_strategy(author, task_id, "Fixture attempt",
                "Offline distinct approach " + str(len(self.controller.state()["strategies"])))
            assignment = forum.claim_strategy(author, strategy["strategy_id"])
            if not candidate:
                return forum.record_no_improvement(author, assignment["assignment_id"],
                    "Fixture alternative did not shorten the proof", "Original proof retained")
            name = self.files[task_id]
            (tree / name).write_text(f"theorem {Path(name).stem.lower()} : True := by trivial\n")
            self.git(tree, "add", name)
            self.git(tree, "commit", "-qm", "shorter offline fixture proof")
            return forum.submit_candidate(author, assignment["assignment_id"],
                                          "Same theorem, direct proof with fewer tactics")

    async def seed_checked(self, task_id="task-a", author="Primary"):
        tree = workflow.task_worktree(self.paths, "seed-" + author, task_id)
        state = self.controller.state()
        env = workflow.runtime_env(self.paths, author, task_id, "optimizing", tree, state)
        candidate = self.writer_result(author, task_id, tree, env, candidate=True)
        await self.controller.check_submissions()
        row = self.controller.state()["candidates"][candidate["candidate_id"]]
        self.assertEqual(row["status"], "checked", row.get("verification"))
        return row["candidate_id"]

    async def fake_transport(self, agent, system, instruction, tree, servers, **kwargs):
        env = kwargs["env_overrides"]
        self.assertEqual(env["UNITY_OPTIMIZE_PROFILE"], "optimizing")
        task_id = env["UNITY_OPTIMIZE_TASK_ID"]
        if env["UNITY_OPTIMIZE_SCORE_REQUEST_ID"]:
            with patch.dict(os.environ, env):
                forum.configure(self.paths.forum, self.root, "optimizing", agent.name)
                request = self.controller.state()["scoring_requests"][env["UNITY_OPTIMIZE_SCORE_REQUEST_ID"]]
                state = self.controller.state()
                for cid in request["subjects"]:
                    text = (tree / state["chunks"][cid]["path"]).read_text()
                    value = 3 if "have h : True := by trivial" in text else 1
                    forum.submit_score_report(agent.name, request["request_id"], cid, value,
                        request["original_methods"].get(cid) or self.method, {"fixture": "known proof value"})
            return
        if not task_id:
            self.assertEqual(agent.name, "Primary")
            self.assertTrue(env["UNITY_OPTIMIZE_PHASE_DISPATCH_ID"])
            self.assertTrue(instruction.startswith("Selection-only"))
            state = self.controller.state()
            checked = [row for row in state["candidates"].values() if row["status"] == "checked"]
            self.selection_signatures.append((state["main_sha"], tuple(sorted(row["candidate_id"] for row in checked))))
            self.timeline.append(("selection", agent.name, "", state["main_sha"]))
            with patch.dict(os.environ, env):
                forum.configure(self.paths.forum, self.root, "optimizing", agent.name)
                if self.select_candidates:
                    for candidate in checked:
                        forum.select_candidate(agent.name, candidate["candidate_id"], "Best checked fixture proof")
            return
        self.assertIn("Work on task " + task_id, instruction)
        self.assertFalse(env["UNITY_OPTIMIZE_PHASE_DISPATCH_ID"])
        self.timeline.append(("writer", agent.name, task_id, self.controller.state()["main_sha"]))
        action = await self.writer_action(agent, task_id, kwargs["interrupt_event"]) if self.writer_action else "none"
        self.writer_result(agent.name, task_id, tree, env, candidate=action == "candidate")
        self.timeline.append(("finished", agent.name, task_id, self.controller.state()["main_sha"]))

    async def run_round(self):
        # Timeout only bounds a broken scheduler; event ordering makes the assertions deterministic.
        await asyncio.wait_for(self.controller.optimize_round(), timeout=20)
        state = self.controller.state()
        self.assertEqual(state["baseline"], self.baseline)
        self.assertEqual(state["rounds"]["started"], 1)
        self.assertEqual(state["rounds"]["completed"], 0)
        self.assertEqual(state["config"]["max_rounds"], 2)
        self.assertFalse(self.controller.workers)
        return state

    async def test_second_task_after_merge_scores_current_source_without_refresh(self):
        async def select_and_merge(cid):
            forum.configure(self.paths.forum, self.root, "optimizing", "Primary")
            forum.select_candidate("Primary", cid, "Checked fixture improvement")
            self.assertTrue(await self.controller.integrate_selected(self.controller.state()["candidates"][cid]))

        first = await self.seed_checked("task-a")
        await select_and_merge(first)
        first_main = self.controller.state()["main_sha"]
        old_b = self.controller.state()["measurements"][self.chunks["task-b"]]
        self.assertNotEqual(old_b["source_sha256"],
            workflow.verification.file_identity(self.root, scope=self.scope)["sha256"])

        # No worker measurement or global refresh is required before B submits.
        second = await self.seed_checked("task-b", "Peer")
        checked = self.controller.state()["candidates"][second]
        self.assertEqual(checked["verification"]["comparison"]["identity"]["main_sha"], first_main)
        comparison = checked["verification"]["comparisons"][self.chunks["task-b"]]
        self.assertTrue(comparison["improved"])
        await select_and_merge(second)
        state = self.controller.state()
        self.assertEqual(state["candidates"][first]["status"], "merged")
        self.assertEqual(state["candidates"][second]["status"], "merged")
        self.assertEqual(state["baseline"], self.baseline)
        self.assertEqual(state["rounds"]["started"], 1)
        self.assertEqual(state["rounds"]["completed"], 0)
        self.assertEqual(state.get("phase_dispatches", {}), {})
        self.assertFalse(self.controller.workers)

    async def test_idle_primary_selects_and_integrates_before_next_writer(self):
        candidate_id = await self.seed_checked()
        state = await self.run_round()
        self.assertEqual(self.timeline[0][0], "selection", self.timeline)
        candidate = state["candidates"][candidate_id]
        self.assertEqual(candidate["status"], "merged")
        self.assertEqual(candidate["selection"]["author"], "Primary")
        self.assertTrue(candidate["integrated_verification"]["passed"])
        self.assertEqual(state["tasks"]["task-a"]["status"], "complete")
        self.assertEqual((self.root / "A.lean").read_text(), "theorem a : True := by trivial\n")
        writers = [row for row in self.timeline if row[0] == "writer"]
        self.assertEqual({row[1] for row in writers}, {"Primary", "Peer"})
        self.assertTrue(all(row[3] == candidate["integrated_sha"] for row in writers), writers)
        self.assertTrue(any(row[1] == "Primary" and row[2] != "task-a" for row in writers), writers)
        self.assertGreaterEqual(len(self.compiler_calls), 4)

    async def test_large_candidate_selection_uses_bounded_mcp_evidence_and_integrates(self):
        candidate_id = await self.seed_checked()
        verbose_evidence = "offline compiler verification detail\n" * 32000
        self.assertGreater(len(verbose_evidence.encode()), 1024 * 1024)
        with tasks.transaction(self.paths.forum) as state:
            state["candidates"][candidate_id]["verification"]["offline_verbose_detail"] = verbose_evidence
        original_candidate = deepcopy(self.controller.state()["candidates"][candidate_id])
        original_baseline = deepcopy(self.controller.state()["baseline"])
        original_rounds = deepcopy(self.controller.state()["rounds"])
        selection_calls, page_counts, response_sizes = [], [], []

        async def selection_transport(agent, system, instruction, tree, servers, **kwargs):
            if not instruction.startswith("Selection-only"):
                return await self.fake_transport(agent, system, instruction, tree, servers, **kwargs)
            self.assertEqual(agent.name, "Primary")
            env = kwargs["env_overrides"]
            self.assertEqual(env["UNITY_OPTIMIZE_TASK_ID"], "")
            self.assertTrue(env["UNITY_OPTIMIZE_PHASE_DISPATCH_ID"])
            with patch.dict(os.environ, env):
                forum.configure(self.paths.forum, self.root, "optimizing", author=agent.name)
                async with Client(forum.build_server("optimizing")) as client:
                    async def native(tool, arguments):
                        response = await client.call_tool(tool, arguments)
                        self.assertFalse(response.is_error)
                        # Exercise actual FastMCP text plus structured serialization,
                        # not just a direct Python function's abbreviated return.
                        size = len(json.dumps({
                            "content": [block.model_dump(mode="json") for block in response.content],
                            "structuredContent": response.structured_content,
                        }, ensure_ascii=False).encode())
                        response_sizes.append(size)
                        self.assertLessEqual(size, 65536, tool)
                        return response.structured_content

                    status = await native("optimize_status", {})
                    self.assertTrue({"candidates", "tasks", "rounds", "review"} <= set(status["records"]))
                    candidate = next(row for row in status["candidates"] if row["candidate_id"] == candidate_id)
                    self.assertEqual(candidate["status"], "checked")
                    reference = candidate["records"]["candidate"]
                    parts, offset = [], 0
                    while True:
                        page = await native("artifact_read", {
                            "artifact_id": reference["artifact_id"], "offset": offset, "limit": 16384})
                        self.assertEqual(page["offset"], offset)
                        parts.append(page["content"])
                        next_offset = page["next_offset"]
                        if next_offset is None:
                            break
                        self.assertGreater(next_offset, offset)
                        offset = next_offset
                    page_counts.append(len(parts))
                    self.assertEqual(json.loads("".join(parts)), original_candidate)
                    before = self.controller.state()
                    acknowledgement = await native("select_candidate", {
                        "author": agent.name, "candidate_id": candidate_id,
                        "reason": "Read the complete checked evidence; shorter same-theorem proof"})
                    self.assertEqual(acknowledgement["candidate_id"], candidate_id)
                    self.assertEqual(acknowledgement["status"], "selected")
                    self.assertIn("candidate", acknowledgement["records"])
                    after = self.controller.state()
                    selected_events = [row for row in after["events"][len(before["events"]):]
                                       if row["kind"] == "candidate_selected"]
                    self.assertEqual(len(selected_events), 1)
                    self.assertEqual(selected_events[0]["candidate_id"], candidate_id)
                    self.assertEqual(after["candidates"][candidate_id]["verification"],
                                     original_candidate["verification"])
                    self.assertEqual(after["baseline"], original_baseline)
                    self.assertEqual(after["rounds"], original_rounds)
                    selection_calls.append(candidate_id)

        with patch.object(workflow, "spawn", side_effect=selection_transport):
            state = await self.run_round()
        self.assertEqual(selection_calls, [candidate_id])
        self.assertGreater(page_counts[0], 1)
        self.assertTrue(response_sizes)
        self.assertEqual(state["candidates"][candidate_id]["status"], "merged")
        self.assertTrue(state["candidates"][candidate_id]["integrated_verification"]["passed"])
        self.assertEqual(state["tasks"]["task-a"]["status"], "complete")
        self.assertEqual((self.root / "A.lean").read_text(), "theorem a : True := by trivial\n")
        self.assertEqual(state["baseline"], original_baseline)
        self.assertEqual(state["rounds"]["started"], original_rounds["started"])
        self.assertEqual(state["rounds"]["completed"], original_rounds["completed"])
        self.assertEqual(state["config"]["max_rounds"], 2)
        self.assertEqual(len([row for row in state["events"] if row["kind"] == "candidate_selected"]), 1)
        self.assertGreaterEqual(len(self.compiler_calls), 4)

    async def test_busy_primary_finishes_without_interrupt_before_selection_and_reassignment(self):
        candidate_id = await self.seed_checked("task-c", "Peer")
        primary_started, release_primary, peer_reassigned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        primary_finished = asyncio.Event()
        async def action(agent, task_id, interrupt):
            if agent.name == "Primary" and not primary_started.is_set():
                primary_started.set()
                await release_primary.wait()
                self.assertFalse(interrupt.is_set(), "selection must not interrupt an active writer")
                primary_finished.set()
            elif agent.name == "Peer" and task_id == "task-c":
                self.assertFalse(primary_finished.is_set())
                peer_reassigned.set()
                release_primary.set()
            return "none"
        self.writer_action = action
        self.controller.launch_ready()
        await asyncio.wait_for(primary_started.wait(), timeout=5)
        state = await self.run_round()
        self.assertTrue(peer_reassigned.is_set(), "idle peer should receive work while primary is busy")
        self.assertTrue(primary_finished.is_set())
        selection = next(i for i, row in enumerate(self.timeline) if row[0] == "selection")
        finish = next(i for i, row in enumerate(self.timeline) if row[:3] == ("finished", "Primary", "task-a"))
        primary_writers = [i for i, row in enumerate(self.timeline) if row[:2] == ("writer", "Primary")]
        self.assertLess(finish, selection, self.timeline)
        self.assertGreater(len(primary_writers), 1, self.timeline)
        self.assertTrue(all(i > selection for i in primary_writers[1:]), self.timeline)
        self.assertEqual(state["candidates"][candidate_id]["status"], "merged")

    async def test_select_none_deduplicates_snapshot_but_new_checked_candidate_gets_selection(self):
        await self.seed_checked()
        self.select_candidates = False
        async def action(agent, task_id, interrupt):
            return "candidate" if agent.name == "Primary" and task_id == "task-c" else "none"
        self.writer_action = action
        state = await self.run_round()
        self.assertEqual(self.timeline[0][0], "selection", self.timeline)
        self.assertEqual(len(self.selection_signatures), 2, self.timeline)
        first, second = self.selection_signatures
        self.assertEqual(first[0], second[0])
        self.assertEqual(len(first[1]), 1)
        self.assertEqual(len(second[1]), 2)
        self.assertTrue(set(first[1]) < set(second[1]))
        selections = [i for i, row in enumerate(self.timeline) if row[0] == "selection"]
        self.assertTrue(any(row[0] == "writer" for row in self.timeline[selections[0] + 1:selections[1]]))
        self.assertEqual({row["status"] for row in state["candidates"].values()}, {"checked"})
        self.assertEqual(state["main_sha"], self.original_main)
        self.assertFalse(any(event["kind"] == "candidate_selected" for event in state["events"]))

    async def test_without_checked_candidates_both_agents_optimize_normally(self):
        state = await self.run_round()
        self.assertEqual(self.selection_signatures, [])
        self.assertEqual({row[1] for row in self.timeline if row[0] == "writer"}, {"Primary", "Peer"})
        self.assertEqual({row["status"] for row in state["tasks"].values()}, {"no_improvement"})
        self.assertFalse(state["phase_dispatches"] if "phase_dispatches" in state else {})
        self.assertEqual(state["main_sha"], self.original_main)


if __name__ == "__main__":
    unittest.main()
