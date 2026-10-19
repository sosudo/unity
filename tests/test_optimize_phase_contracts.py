"""Real Optimize worker/prompt/forum contracts; only the model transport is fake."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_optimize_workflow_lifecycle as lifecycle
from unity import optimize_tasks as tasks, optimize_workflow as workflow
from unity.forum import optimize_server as forum
from unity.optimize_spawn import SpawnCleanupError


class OptimizePhaseContractTests(unittest.IsolatedAsyncioTestCase):
    # Reuse fixture helpers, not its tests or a replacement Controller.worker.
    fixture = lifecycle.OptimizeWorkflowLifecycleTests
    asyncSetUp = fixture.asyncSetUp
    asyncTearDown = fixture.asyncTearDown
    git = fixture.git
    start = fixture.start
    fake_build = fixture.fake_build
    fake_worker = fixture.fake_worker
    fake_spawn = fixture.fake_spawn
    fake_runtime = fixture.fake_runtime

    def seed_baseline(self):
        state = self.controller.state()
        with self.fake_runtime():
            baseline = workflow.verification.capture_baseline(self.root, metric=self.definition,
                                                              scope=state["scope"], lake_args=[])
        tasks.set_baseline(self.paths.forum, baseline)
        tasks.install_inventory(self.paths.forum, tasks.scan_sources(self.root, ["A.lean"]))
        with tasks.transaction(self.paths.forum) as current:
            current["architect_done"] = True

    def transport(self, action):
        async def respond(agent, system, instruction, tree, servers, **kwargs):
            env = kwargs["env_overrides"]
            self.assertEqual(env["UNITY_OPTIMIZE_TASK_ID"], "")
            self.assertTrue(env["UNITY_OPTIMIZE_PHASE_DISPATCH_ID"])
            self.assertNotIn("on_normal_completion", kwargs)
            with patch.dict(os.environ, env):
                forum.configure(self.paths.forum, self.root, env["UNITY_OPTIMIZE_PROFILE"], author=agent.name)
                return action(agent, env, instruction, tree)
        return respond

    async def test_all_phase_turns_clear_inherited_task_binding(self):
        seen = []
        def respond(agent, env, instruction, tree):
            profile = env["UNITY_OPTIMIZE_PROFILE"]
            brief = forum.optimize_brief()
            self.assertFalse(brief["bound_task"])
            finding = forum.publish_finding(agent.name, "", "Phase finding", "Retained fixture evidence")
            self.assertEqual(finding["task_id"], "")
            self.assertNotIn("Work on task " + profile, instruction)
            if profile in {"architect", "exploration"}:
                forum.report_phase_result(agent.name, "skipped" if profile == "architect" else "complete",
                                          "Fixture result", "Fixture evidence")
            seen.append((profile, instruction))
        with patch.dict(os.environ, {"UNITY_OPTIMIZE_TASK_ID": "inherited-nonexistent"}), \
                patch.object(workflow, "spawn", side_effect=self.transport(respond)):
            for profile in ("architect", "exploration", "critic", "retrospective"):
                await self.controller.dispatch_phase(profile, primary=True)
            await self.controller.dispatch_phase("optimizing", primary=True, instruction="Selection-only turn")
            await self.controller.dispatch_phase("optimizing", instruction="Measurement refresh only")
        self.assertEqual(len(seen), 6)
        self.assertEqual(self.controller.state()["rounds"]["started"], 0)

    async def test_unknown_task_rejected_before_transport(self):
        with patch.object(workflow, "spawn", new=AsyncMock()) as transport:
            with self.assertRaisesRegex(ValueError, "unknown Optimize task"):
                await self.controller.worker(self.agent, "chunking", self.root, "optimizing", asyncio.Event())
        transport.assert_not_called()

    async def test_real_task_keeps_binding_and_actual_task_tool(self):
        with self.fake_runtime():
            await self.controller.prepare()
        tid = next(iter(self.controller.state()["tasks"]))
        tree = workflow.task_worktree(self.paths, self.agent.name, tid)
        async def respond(agent, system, instruction, tree, servers, **kwargs):
            env = kwargs["env_overrides"]
            self.assertEqual(env["UNITY_OPTIMIZE_TASK_ID"], tid)
            self.assertEqual(env["UNITY_OPTIMIZE_PHASE_DISPATCH_ID"], "")
            self.assertIn("Work on task " + tid, instruction)
            with patch.dict(os.environ, env):
                forum.configure(self.paths.forum, self.root, "optimizing", author=agent.name)
                self.assertEqual(forum.optimize_task(tid)["task"]["task_id"], tid)
        with patch.object(workflow, "spawn", side_effect=respond):
            await self.controller.worker(self.agent, tid, tree, "optimizing", asyncio.Event())

    async def test_normal_blocked_turn_does_not_mark_exploration_done(self):
        self.seed_baseline()
        before = deepcopy(self.controller.state()["baseline"])
        def respond(agent, env, instruction, tree):
            self.assertEqual(env["UNITY_OPTIMIZE_PROFILE"], "exploration")
            forum.report_obstacle(agent.name, "", "Cannot establish a measurement method", "Fixture reason")
            forum.report_phase_result(agent.name, "blocked", "Measurement method unresolved", "Fixture reason")
            return "I am finished, but blocked."
        with self.fake_runtime(), patch.object(workflow, "spawn", side_effect=self.transport(respond)) as spawn:
            with self.assertRaisesRegex(ValueError, "no completed phase result"):
                await workflow.run(self.paths, self.roster, metric="length", continue_=True,
                                   max_attempts=2, retrospective=False)
        state = self.controller.state()
        self.assertEqual(spawn.call_count, 1)
        self.assertFalse(state.get("exploration_done", False))
        self.assertEqual(state["phase"], "blocked")
        self.assertEqual(state["baseline"], before)
        self.assertFalse(state["baseline_sealed"])
        self.assertEqual(state["rounds"]["started"], 0)
        self.assertIn("Measurement method unresolved", state["error"])

    async def test_normal_text_without_result_is_not_completion(self):
        with patch.object(workflow, "spawn", side_effect=self.transport(lambda *args: "All done!")) as spawn:
            with self.assertRaisesRegex(ValueError, "missing results"):
                await self.controller.prepare()
        state = self.controller.state()
        self.assertEqual(spawn.call_count, 1)
        self.assertFalse(state.get("architect_done", False))
        self.assertEqual(state["rounds"]["started"], 0)

    async def test_chunking_complete_receipt_without_plan_still_rejects(self):
        self.seed_baseline()
        with tasks.transaction(self.paths.forum) as state:
            state["exploration_done"] = True
        def respond(agent, env, instruction, tree):
            forum.report_phase_result(agent.name, "complete", "Claims finished planning", "Fixture claim only")
        with patch.object(workflow, "spawn", side_effect=self.transport(respond)):
            with self.assertRaisesRegex(ValueError, "without a complete declaration plan"):
                await self.controller.prepare()
        state = self.controller.state()
        self.assertFalse(state["baseline_sealed"])
        self.assertEqual(state["tasks"], {})
        self.assertEqual(state["rounds"]["started"], 0)

    async def test_chunking_missing_measurements_does_not_install_partial_plan(self):
        self.seed_baseline()
        with tasks.transaction(self.paths.forum) as state:
            state["exploration_done"] = True
        def respond(agent, env, instruction, tree):
            rows = forum.optimize_inventory()["chunks"]
            forum.propose_chunk_plan(agent.name, [{"chunk_ids": [row["chunk_id"]]} for row in rows], "Fixture plan")
            forum.report_phase_result(agent.name, "complete", "Plan only", "No measurements supplied")
        with patch.object(workflow, "spawn", side_effect=self.transport(respond)):
            with self.assertRaisesRegex(ValueError, "missing original metric measurements"):
                await self.controller.prepare()
        self.assertFalse(self.controller.state()["baseline_sealed"])
        self.assertEqual(self.controller.state()["tasks"], {})

    async def test_file_bridge_plan_keeps_source_snapshot_and_seals_baseline(self):
        self.seed_baseline()
        with tasks.transaction(self.paths.forum) as state:
            state["exploration_done"] = True
        scope = self.controller.state()["scope"]
        original = workflow.verification.file_identity(self.root, scope=scope)
        retained = []

        async def respond(agent, system, instruction, tree, servers, **kwargs):
            env = kwargs["env_overrides"]
            scratch = Path(env["UNITY_OPTIMIZE_SCRATCH"])
            self.assertEqual(scratch, tree / ".unity/scratch/optimize")
            self.assertIn(str(scratch), system)
            self.assertTrue(scratch.is_dir())
            before = workflow.verification.file_identity(tree, scope=scope)
            # Execute the installed private bridge wrapper, not an in-memory
            # forum mock. Only provider transport and baseline build are fake.
            child_env = {**os.environ, **env,
                "PYTHONPATH": str(Path(workflow.__file__).resolve().parent.parent)}

            async def bridge(tool, arguments):
                process = await asyncio.create_subprocess_exec(
                    "unity", "mcp", "unity-forum", tool, *arguments,
                    cwd=tree, env=child_env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                self.assertEqual(process.returncode, 0, stderr.decode())
                return stdout.decode()

            inventory = await bridge("optimize_inventory", ["{}"])
            (scratch / "perm-inv.json").write_text(inventory)
            chunks = json.loads(inventory)["chunks"]
            plan = {"author": agent.name, "chunks": [{"chunk_ids": [row["chunk_id"]]} for row in chunks],
                    "reason": "Offline file-based bridge regression"}
            for name in ("plan.json", "plan2.json", "plan3.json", "plan4.json", "plan5.json", "plan6.json"):
                (scratch / name).write_text(json.dumps(plan))
            await bridge("propose_chunk_plan", ["--args-file", str(scratch / "plan6.json")])
            for row in chunks:
                await bridge("record_measurement", [json.dumps({"author": agent.name,
                    "chunk_id": row["chunk_id"], "raw_value": 3, "method": self.method,
                    "evidence": "Offline fixture: have; trivial; exact", "baseline": True})])
            report = {"author": agent.name, "status": "complete", "summary": "Plan and measurements submitted",
                      "evidence": {"chunks": len(chunks)}}
            for name in ("report.json", "report2.json"):
                (scratch / name).write_text(json.dumps(report))
            await bridge("report_phase_result", ["--args-file", str(scratch / "report2.json")])
            self.assertEqual(workflow.verification.file_identity(tree, scope=scope), before)
            self.assertEqual(workflow.verification.file_identity(self.root, scope=scope), original)
            retained.extend(scratch.iterdir())

        with patch.object(workflow, "spawn", side_effect=respond):
            self.assertTrue(await self.controller.prepare())
        state = self.controller.state()
        self.assertTrue(state["baseline_sealed"])
        self.assertEqual(len(state["tasks"]), 1)
        self.assertEqual(state["rounds"]["started"], 0)
        self.assertEqual(len(retained), 9)
        self.assertTrue(all(path.is_file() for path in retained))
        dispatch = next(iter(state["phase_dispatches"].values()))
        self.assertEqual(dispatch["status"], "complete")
        self.assertEqual(dispatch["results"]["A"]["status"], "complete")
        self.assertEqual(workflow.verification.file_identity(self.root, scope=scope), original)

    async def test_readonly_phase_still_rejects_real_private_snapshot_changes(self):
        cases = (("A.lean", "files modified"), ("New.lean", "files added"),
                 (".unity-tmp/plan.json", "files added"),
                 (".unity/agents.yaml", "configuration modified"),
                 (".unity/metrics/length.md", "metric definition/helpers changed"))
        for name, category in cases:
            with self.subTest(path=name):
                captured = []
                def respond(agent, env, instruction, tree):
                    target = tree / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("private-fixture-content-not-for-diagnostics\n")
                    captured.append(tree)
                    forum.report_phase_result(agent.name, "complete", "Fixture result", "Fixture evidence")
                with patch.object(workflow, "spawn", side_effect=self.transport(respond)):
                    with self.assertRaisesRegex(ValueError, "read-only source snapshot") as raised:
                        await self.controller.dispatch_phase("exploration")
                message = str(raised.exception)
                self.assertIn("worker A", message)
                self.assertIn(str(captured[0]), message)
                self.assertIn("dispatch ", message)
                self.assertIn(category, message)
                if "metric" not in name:
                    self.assertIn(name, message)
                self.assertNotIn("private-fixture-content", message)
                dispatch = list(self.controller.state()["phase_dispatches"].values())[-1]
                self.assertEqual(dispatch["status"], "blocked")
                self.assertIn(message, dispatch["reason"])

    async def test_readonly_phase_reports_deleted_files_and_head_changes(self):
        def respond(agent, env, instruction, tree):
            (tree / "A.lean").unlink()
            workflow.git(tree, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                         "commit", "--allow-empty", "-m", "Offline HEAD change")
            forum.report_phase_result(agent.name, "complete", "Fixture result", "Fixture evidence")
        with patch.object(workflow, "spawn", side_effect=self.transport(respond)):
            with self.assertRaisesRegex(ValueError, "read-only source snapshot") as raised:
                await self.controller.dispatch_phase("exploration")
        self.assertIn('files removed: "A.lean"', str(raised.exception))
        self.assertIn("Git HEAD changed", str(raised.exception))

    async def test_canonical_snapshot_failure_is_distinct_from_worker_failure(self):
        def respond(agent, env, instruction, tree):
            (self.root / "A.lean").write_text("canonical fixture change\n")
            forum.report_phase_result(agent.name, "complete", "Fixture result", "Fixture evidence")
        with patch.object(workflow, "spawn", side_effect=self.transport(respond)):
            with self.assertRaisesRegex(ValueError, "read-only project snapshot") as raised:
                await self.controller.dispatch_phase("exploration")
        message = str(raised.exception)
        self.assertIn("canonical project", message)
        self.assertIn('files modified: "A.lean"', message)
        self.assertNotIn("worker A", message)

    def test_snapshot_diagnostics_are_bounded_and_do_not_expose_values(self):
        original = workflow.verification.file_identity(self.root)
        changed = deepcopy(original)
        changed["files"].update({f"file-{index:03}-\n" + "x" * 300: "hidden-content" for index in range(50)})
        changed["config"][".unity/config.yaml"] = "hidden-config"
        changed["scope"] = {"targets": "hidden-targets"}
        changed["lake_args"] = ["hidden-option"]
        summary = workflow._snapshot_changes((original, self.definition), (changed, {"definition": "hidden-metric"}))
        self.assertIn("(+45 more)", summary)
        self.assertIn("\\n", summary)
        self.assertNotIn("\n", summary)
        self.assertNotIn("hidden-", summary)
        self.assertIn("scope changed", summary)
        self.assertIn("Lake arguments changed", summary)
        self.assertLess(len(summary), 2000)

    async def test_planning_scratch_is_private_per_worktree_and_critic_gets_none(self):
        directories = []
        for name in ("A", "B"):
            tree = workflow.task_worktree(self.paths, name, "chunking")
            env = workflow.runtime_env(self.paths, name, None, "chunking", tree, self.controller.state())
            scratch = Path(env["UNITY_OPTIMIZE_SCRATCH"])
            self.assertEqual(scratch, tree / ".unity/scratch/optimize")
            self.assertEqual(scratch.resolve(), scratch)
            self.assertNotEqual(scratch, Path(env["TMPDIR"]))
            directories.append(scratch)
        self.assertNotEqual(*directories)

        async def respond(agent, system, instruction, tree, servers, **kwargs):
            self.assertEqual(kwargs["env_overrides"]["UNITY_OPTIMIZE_SCRATCH"], "")
            self.assertFalse((tree / ".unity/scratch").exists())
            self.assertIn("Final review remains read-only", system)
            self.assertIn("do not create planning scratch files", system)
        with patch.dict(os.environ, {"UNITY_OPTIMIZE_SCRATCH": "inherited-scratch"}), \
                patch.object(workflow, "spawn", side_effect=respond):
            await self.controller.dispatch_phase("critic", primary=True)

    def test_planning_scratch_rejects_redirected_directories(self):
        for index, relative in enumerate((".unity", ".unity/scratch", ".unity/scratch/optimize")):
            with self.subTest(path=relative):
                tree = self.root / f"redirect-fixture-{index}"
                target = tree / relative
                target.parent.mkdir(parents=True)
                outside = self.root / f"outside-fixture-{index}"
                outside.mkdir()
                target.symlink_to(outside, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, "private|symlinks"):
                    workflow._planning_scratch(tree)
                self.assertEqual(list(outside.iterdir()), [])

    async def test_blocked_contributor_does_not_veto_collective_plan(self):
        self.seed_baseline()
        with tasks.transaction(self.paths.forum) as state:
            state["exploration_done"] = True
            state["config"]["agents"] = ["A", "B"]
        second = SimpleNamespace(name="B")
        self.roster.agents.append(second)
        def respond(agent, env, instruction, tree):
            if agent.name == "A":
                forum.report_phase_result("A", "blocked", "A could not derive a method", "A fixture evidence")
                return
            rows = forum.optimize_inventory()["chunks"]
            forum.propose_chunk_plan("B", [{"chunk_ids": [row["chunk_id"]]} for row in rows], "Collaborative plan")
            for row in rows:
                forum.record_measurement("B", row["chunk_id"], 3, self.method, "have; trivial; exact", baseline=True)
            forum.report_phase_result("B", "complete", "Plan and original measurements", {"chunks": len(rows)})
        with patch.object(workflow, "spawn", side_effect=self.transport(respond)):
            self.assertTrue(await self.controller.prepare())
        state = self.controller.state()
        self.assertTrue(state["baseline_sealed"])
        self.assertEqual(len(state["tasks"]), 1)
        self.assertEqual(state["rounds"]["started"], 0)
        self.assertEqual(state["plan_proposals"]["plan-1"]["author"], "B")

    async def test_initial_inventory_available_during_exploration(self):
        seen = []
        async def respond(*args, **kwargs):
            env = kwargs["env_overrides"]
            if env["UNITY_OPTIMIZE_PROFILE"] == "exploration":
                self.assertTrue(self.controller.state()["chunks"])
                seen.append("exploration")
            return await self.fake_spawn(*args, **kwargs)
        with self.fake_runtime(), patch.object(workflow, "spawn", side_effect=respond):
            self.assertTrue(await self.controller.prepare())
        self.assertEqual(seen, ["exploration"])

    async def test_legacy_completed_flags_do_not_replay_setup_or_reset_baseline(self):
        self.seed_baseline()
        with tasks.transaction(self.paths.forum) as state:
            state["exploration_done"] = True
        before = deepcopy(self.controller.state())
        with self.fake_runtime():
            self.assertTrue(await self.controller.prepare())
        after = self.controller.state()
        self.assertEqual([phase for phase, _ in self.phases], ["chunking"])
        self.assertEqual(after["base_sha"], before["base_sha"])
        self.assertEqual(after["scope"], before["scope"])
        self.assertEqual(after["rounds"], before["rounds"])
        self.assertEqual(after["baseline"]["source_identity"], before["baseline"]["source_identity"])

    async def test_interrupted_phase_resume_requires_verified_closure(self):
        self.seed_baseline()
        with tasks.transaction(self.paths.forum) as state:
            state.update(phase="chunking", exploration_done=True)
        previous = tasks.begin_phase_dispatch(self.paths.forum, "chunking", ["A"])
        with self.fake_runtime(), \
                patch("unity.optimize_processes.assert_closed", side_effect=ValueError("Prior worker still live")):
            with self.assertRaisesRegex(ValueError, "Prior worker still live"):
                await workflow.run(self.paths, self.roster, metric="length", continue_=True,
                                   max_attempts=2, retrospective=False)
        self.assertEqual(self.controller.state()["phase_dispatches"][previous["dispatch_id"]]["status"], "running")
        self.assertEqual(self.phases, [])
        with self.fake_runtime():
            result = await workflow.run(self.paths, self.roster, metric="length", continue_=True,
                                        max_attempts=2, retrospective=False)
        self.assertEqual(result["status"], "accepted")
        state = self.controller.state()
        self.assertEqual(state["phase_dispatches"][previous["dispatch_id"]]["status"], "interrupted")
        self.assertNotIn("architect", [phase for phase, _ in self.phases])
        self.assertNotIn("exploration", [phase for phase, _ in self.phases])

    def architect_edit(self, agent, env, instruction, tree):
        (tree / "lakefile.toml").write_text('name = "fixture"\n')
        workflow.git(tree, "add", "lakefile.toml")
        workflow.git(tree, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "-m", "Fixture dependency setup")

    async def test_architect_clean_skip_cannot_leave_dependency_edits(self):
        def respond(*args):
            self.architect_edit(*args)
            forum.report_phase_result("A", "skipped", "Claims clean skip", "Fixture dependency edit remains")
        with patch.object(workflow, "spawn", side_effect=self.transport(respond)):
            with self.assertRaisesRegex(ValueError, "clean skip left"):
                await self.controller.prepare()
        self.assertFalse(self.controller.state().get("architect_done", False))
        self.assertFalse((self.root / "lakefile.toml").exists())

    async def test_architect_completed_result_does_not_bypass_build_check(self):
        def respond(*args):
            self.architect_edit(*args)
            forum.report_phase_result("A", "complete", "Dependency setup done", "Fixture commit")
        with patch.object(workflow, "spawn", side_effect=self.transport(respond)), \
                patch.object(workflow.verification, "build_scope", return_value={"passed": False}) as build:
            with self.assertRaisesRegex(ValueError, "did not pass controller build"):
                await self.controller.prepare()
        build.assert_called_once()
        self.assertFalse(self.controller.state().get("architect_done", False))
        self.assertFalse((self.root / "lakefile.toml").exists())

    async def test_owned_cleanup_error_is_not_retried_or_marked_complete(self):
        fatal = SpawnCleanupError("Fixture retained owned worker")
        with patch.object(workflow, "spawn", new=AsyncMock(side_effect=fatal)) as spawn:
            with self.assertRaises(SpawnCleanupError) as raised:
                await self.controller.prepare()
        self.assertIs(raised.exception, fatal)
        self.assertEqual(spawn.call_count, 1)
        self.assertFalse(self.controller.state().get("architect_done", False))

    async def test_phase_receipt_write_failure_does_not_mask_original_fault(self):
        error = ValueError("Fixture provider failure")
        with patch.object(workflow, "spawn", new=AsyncMock(side_effect=error)), \
                patch.object(tasks, "close_phase_dispatch", side_effect=OSError("Fixture receipt I/O failure")):
            with self.assertRaises(ValueError) as raised:
                await self.controller.prepare()
        self.assertIs(raised.exception, error)
        self.assertIn("phase receipt", raised.exception.__notes__[0])


if __name__ == "__main__":
    unittest.main()
