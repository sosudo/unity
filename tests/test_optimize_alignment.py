"""Offline regressions for the approved Optimize workflow, not an evaluation."""
from pathlib import Path
import tempfile
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unity import optimize_metrics as metrics, optimize_tasks as tasks, optimize_workflow as workflow
from tests import test_optimize_workflow_lifecycle as lifecycle


def sealed(value):
    return {**value, "sha256": tasks.digest(value)}


class OptimizePlanningAlignmentTests(unittest.IsolatedAsyncioTestCase):
    fake_build = lifecycle.OptimizeWorkflowLifecycleTests.fake_build
    fake_worker = lifecycle.OptimizeWorkflowLifecycleTests.fake_worker
    fake_spawn = lifecycle.OptimizeWorkflowLifecycleTests.fake_spawn

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.root.mkdir()
        (self.root / "A.lean").write_text("theorem a : True := by trivial\ntheorem b : True := by trivial\n")
        self.paths = workflow.paths_for(self.root)
        (self.root / ".gitignore").write_text(".unity/\n.worktrees/\n.lake/\n")
        for args in (("init", "-q"), ("config", "user.name", "Fixture"),
                     ("config", "user.email", "fixture@example.invalid"), ("add", "."),
                     ("commit", "-qm", "Original fixture")):
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        metric_path = self.root / ".unity/metrics/length.md"
        metric_path.parent.mkdir(parents=True)
        metric_path.write_text((Path(metrics.__file__).parent / "defaults/metrics/length.md").read_text())
        self.metric = metrics.load_metric(self.root, "length")
        self.real_job = workflow.jobs.run
        self.compiler_calls, self.phases = [], []
        self.method = {"description": "Explicit offline fixture tactic count", "direction": "lower"}
        tasks.initialize(self.paths.forum, self.root, run_id="offline-optimize", base_sha=head,
            metric=self.metric, scope={"targets": "Universe", "files": ["A.lean"], "modules": []},
            config={"primary": "Alice", "agents": ["Alice", "Bob"], "real_lake": sys.executable})
        with tasks.transaction(self.paths.forum) as state:
            state.update(architect_done=True, exploration_done=True)
        agents = [SimpleNamespace(name="Alice"), SimpleNamespace(name="Bob")]
        self.controller = workflow.Controller(self.paths, SimpleNamespace(agents=agents, primary=agents[0]), max_attempts=2)
        layout = patch.object(workflow.workspace, "discover", return_value={"build_dir": ".lake/build"})
        layout.start()
        self.addCleanup(layout.stop)
        build = patch.object(workflow.jobs, "run", side_effect=self.fake_build)
        build.start()
        self.addCleanup(build.stop)
        transport = patch.object(workflow, "spawn", side_effect=self.fake_spawn)
        transport.start()
        self.addCleanup(transport.stop)
        state = self.controller.state()
        baseline = workflow.verification.capture_baseline(self.root, metric=self.metric,
            scope=state["scope"], lake_args=[])
        tasks.set_baseline(self.paths.forum, baseline)

    async def test_later_complete_plan_replaces_conflicting_draft_without_unanimity_gate(self):
        async def chunking(profile, **kwargs):
            self.assertEqual(profile, "chunking")
            with tasks.transaction(self.paths.forum) as state: state["phase"] = "chunking"
            chunks = list(tasks.load(self.paths.forum)["chunks"])
            tasks.propose_plan(self.paths.forum, "Alice", [{"chunk_ids": chunks,
                "reason": "Initial grouping proposal for shared tactic pattern"}], "draft")
            final = tasks.propose_plan(self.paths.forum, "Bob", [{"chunk_ids": [cid]} for cid in chunks],
                                       "After forum discussion, declarations can be optimized independently")
            self.final_plan = final["proposal_id"]
            return [object()]
        with patch.object(self.controller, "dispatch_phase", side_effect=chunking), \
                patch.object(workflow.verification, "capture_baseline", side_effect=AssertionError("baseline already exists")):
            self.assertTrue(await self.controller.prepare())
        state = tasks.load(self.paths.forum)
        self.assertEqual(len(state["tasks"]), 2)
        self.assertEqual(state["plan_proposals"][self.final_plan]["status"], "accepted")
        self.assertTrue(state["baseline_sealed"])
        self.assertEqual(len(state["plan_proposals"]), 2)

    async def test_resume_planning_can_supersede_previous_invocation_drafts(self):
        tasks.install_inventory(self.paths.forum, tasks.scan_sources(self.root, ["A.lean"]))
        with tasks.transaction(self.paths.forum) as state: state["phase"] = "chunking"
        chunks = list(tasks.load(self.paths.forum)["chunks"])
        tasks.propose_plan(self.paths.forum, "Alice", [{"chunk_ids": chunks,
            "reason": "Old interrupted grouping proposal"}])

        async def resumed_chunking(profile, **kwargs):
            with tasks.transaction(self.paths.forum) as state: state["phase"] = "chunking"
            tasks.propose_plan(self.paths.forum, "Bob", [{"chunk_ids": [cid]} for cid in chunks],
                               "Current collaboratively revised complete plan")
            return [object()]
        with patch.object(self.controller, "dispatch_phase", side_effect=resumed_chunking):
            self.assertTrue(await self.controller.prepare())
        self.assertEqual(len(tasks.load(self.paths.forum)["tasks"]), 2)

    async def test_more_than_nine_drafts_use_numeric_order_when_timestamps_match(self):
        async def chunking(profile, **kwargs):
            with tasks.transaction(self.paths.forum) as state: state["phase"] = "chunking"
            chunks = list(tasks.load(self.paths.forum)["chunks"])
            with patch.object(tasks, "now", return_value="2026-10-17T12:00:00+00:00"):
                for index in range(11):
                    tasks.propose_plan(self.paths.forum, "Alice", [{"chunk_ids": chunks,
                        "reason": "Prior grouped draft " + str(index)}])
                latest = tasks.propose_plan(self.paths.forum, "Bob", [{"chunk_ids": [cid]} for cid in chunks],
                                            "Final declaration-level plan after discussion")
            self.assertEqual(latest["proposal_id"], "plan-12")
            return [object()]
        with patch.object(self.controller, "dispatch_phase", side_effect=chunking):
            self.assertTrue(await self.controller.prepare())
        state = tasks.load(self.paths.forum)
        self.assertEqual(state["plan_proposals"]["plan-12"]["status"], "accepted")
        self.assertEqual(len(state["tasks"]), 2)

    async def test_missing_verifier_reports_are_not_fabricated_from_complete_plan(self):
        async def chunking(profile, **kwargs):
            with tasks.transaction(self.paths.forum) as state: state["phase"] = "chunking"
            chunks = list(tasks.load(self.paths.forum)["chunks"])
            tasks.propose_plan(self.paths.forum, "Bob", [{"chunk_ids": [cid]} for cid in chunks])
            return [object()]
        async def no_score(*args, **kwargs):
            self.assertTrue(kwargs["env_overrides"]["UNITY_OPTIMIZE_SCORE_REQUEST_ID"])
        with patch.object(self.controller, "dispatch_phase", side_effect=chunking), \
                patch.object(workflow, "spawn", side_effect=no_score):
            with self.assertRaisesRegex(ValueError, "without all requested subject reports"):
                await self.controller.prepare()
        self.assertFalse(tasks.load(self.paths.forum)["baseline_sealed"])
        self.assertEqual(tasks.load(self.paths.forum)["baseline_measurements"], {})


if __name__ == "__main__":
    unittest.main()
