"""Offline regressions for the approved Optimize workflow, not an evaluation."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unity import optimize_metrics as metrics, optimize_tasks as tasks, optimize_workflow as workflow


def sealed(value):
    return {**value, "sha256": tasks.digest(value)}


class OptimizePlanningAlignmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.root.mkdir()
        (self.root / "A.lean").write_text("theorem a : True := by trivial\ntheorem b : True := by trivial\n")
        self.paths = workflow.paths_for(self.root)
        self.metric = sealed({"name": "length", "builtin": "length", "direction": "lower", "units": "tactic invocations"})
        self.identity = sealed({"main_sha": "baseline", "files": {"A.lean": "original-bytes"}, "config": {}})
        baseline = sealed({"metric": self.metric, "identity": self.identity, "source_identity": self.identity,
                           "measurements": {}, "build": {"passed": True}})
        tasks.initialize(self.paths.forum, self.root, run_id="offline-optimize", base_sha="baseline",
            metric=self.metric, scope={"targets": "Universe", "files": ["A.lean"], "modules": []},
            config={"primary": "Alice", "agents": ["Alice", "Bob"]}, baseline=baseline)
        with tasks.transaction(self.paths.forum) as state:
            state.update(architect_done=True, exploration_done=True)
        agents = [SimpleNamespace(name="Alice"), SimpleNamespace(name="Bob")]
        self.controller = workflow.Controller(self.paths, SimpleNamespace(agents=agents, primary=agents[0]), max_attempts=2)
        layout = patch.object(workflow.workspace, "discover", return_value={"build_dir": ".lake/build"})
        layout.start()
        self.addCleanup(layout.stop)

    def measurements(self):
        state = tasks.load(self.paths.forum)
        for cid in state["chunks"]:
            row = metrics.record_measurement(self.metric, subject=cid, raw_value=1,
                source_identity=self.identity, method={"description": "explicit tactic inventory"},
                evidence="trivial is one invocation")
            tasks.record_baseline_measurement(self.paths.forum, "Bob", cid, row)

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
            self.measurements()
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
            self.measurements()
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
            self.measurements()
            return [object()]
        with patch.object(self.controller, "dispatch_phase", side_effect=chunking):
            self.assertTrue(await self.controller.prepare())
        state = tasks.load(self.paths.forum)
        self.assertEqual(state["plan_proposals"]["plan-12"]["status"], "accepted")
        self.assertEqual(len(state["tasks"]), 2)

    async def test_missing_measurements_are_reported_not_fabricated(self):
        async def chunking(profile, **kwargs):
            with tasks.transaction(self.paths.forum) as state: state["phase"] = "chunking"
            chunks = list(tasks.load(self.paths.forum)["chunks"])
            tasks.propose_plan(self.paths.forum, "Bob", [{"chunk_ids": [cid]} for cid in chunks])
            return [object()]
        with patch.object(self.controller, "dispatch_phase", side_effect=chunking):
            with self.assertRaisesRegex(ValueError, "missing original metric measurements"):
                await self.controller.prepare()
        self.assertFalse(tasks.load(self.paths.forum)["baseline_sealed"])


if __name__ == "__main__":
    unittest.main()
