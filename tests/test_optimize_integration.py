"""Real temporary Git/forum integration with fake compiler and zero inference."""
from copy import deepcopy
import asyncio
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from unity import optimize_jobs as jobs, optimize_metrics as metrics, optimize_tasks as tasks
from unity import optimize_verification as verification, optimize_workflow as workflow
from unity.forum import optimize_server as forum
from unity.roster import Agent, Roster


class OptimizeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="optimize-integration-offline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git(self.root, "init", "--quiet")
        self.git(self.root, "config", "user.name", "Fixture")
        self.git(self.root, "config", "user.email", "fixture@localhost")
        self.write(".gitignore", ".unity/\n.worktrees/\n.lake/\n.env\n")
        self.write("Example.lean", "theorem first : True := by\n  have h : True := by trivial\n  exact h\n")
        self.write("lean-toolchain", "leanprover/lean4:v4.34.1\n")
        self.write("lakefile.toml", 'name = "Example"\n')
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "--quiet", "-m", "fixture baseline")
        metric_text = (Path(metrics.__file__).parent / "defaults" / "metrics" / "length.md").read_text()
        self.write(".unity/metrics/length.md", metric_text)
        self.metric = metrics.load_metric(self.root, "length")
        self.scope = {"files": ["Example.lean"], "modules": ["Example"], "targets": "Universe"}
        self.paths = workflow.paths_for(self.root)
        self.roster = Roster([Agent(name, "offline-fixture", "openai", "codex", 5, None, None, None, None, name == "Primary")
                              for name in ("Primary", "Peer")])
        self.method = {"description": "Explicit fixture tactic count", "direction": "lower"}
        self.real_job = jobs.run
        self.build_calls = []
        mock = patch.object(jobs, "run", side_effect=self.fake_job)
        mock.start()
        self.addCleanup(mock.stop)
        discover = patch("unity.optimize_workspace.discover", return_value={"modules": {"Example.lean": "Example"}, "unmatched": [], "issues": []})
        discover.start()
        self.addCleanup(discover.stop)
        self.addCleanup(patch.stopall)
        self.head = self.git(self.root, "rev-parse", "HEAD")
        tasks.initialize(self.paths.forum, self.root, run_id="optimize-offline-fixture", base_sha=self.head,
                         metric=self.metric, scope=self.scope,
                         config={"primary": "Primary", "agents": ["Primary", "Peer"], "targets": "Universe", "lake_args": []})
        tasks.set_baseline(self.paths.forum, verification.capture_baseline(self.root, metric=self.metric, scope=self.scope))
        inventory = tasks.scan_sources(self.root, self.scope["files"])
        tasks.install_inventory(self.paths.forum, inventory)
        self.cid = next(iter(inventory["chunks"]))
        workflow.phase(self.paths, "chunking")
        forum.configure(self.paths.forum, self.root, "chunking", "Primary")
        plan = forum.propose_chunk_plan("Primary", [{"chunk_ids": [self.cid]}], "Single declaration fixture")
        tasks.accept_plan(self.paths.forum, plan["proposal_id"])
        self.seed_measurement(self.cid)
        tasks.seal_baseline(self.paths.forum)
        with tasks.transaction(self.paths.forum) as state:
            state["measurements"] = deepcopy(state["baseline"]["measurements"])
        self.baseline = deepcopy(tasks.load(self.paths.forum)["baseline"])
        tasks.begin_round(self.paths.forum, max_rounds=5)
        self.controller = workflow.Controller(self.paths, self.roster, max_attempts=5)
        self.controller.evaluator = lambda: self.fake_evaluator

    def seed_measurement(self, cid):
        baseline = tasks.load(self.paths.forum)["baseline"]
        tasks.record_baseline_measurement(self.paths.forum, "Primary", cid,
            metrics.record_measurement(self.metric, subject=cid, raw_value=3,
                source_identity=baseline["identity"], method=self.method,
                evidence={"fixture": "known original proof"}))

    async def fake_evaluator(self, request):
        # Fixture answers only. This is not a production tactic counter.
        root = Path(request["root"])
        inventory = tasks.scan_sources(root, self.scope["files"])["chunks"]
        rows = {}
        for cid in request["subjects"]:
            chunk = inventory[cid]
            lines = (root / chunk["path"]).read_text().splitlines()
            body = "\n".join(lines[chunk["start_line"] - 1:chunk["end_line"]])
            value = 3 if "have h : True := by trivial" in body else 2 if "have h := True.intro" in body else 1
            rows[cid] = {"raw_value": value, "method": request["original_methods"].get(cid) or self.method,
                         "evidence": {"fixture": "known proof value"}}
        return {"measurements": rows}

    def write(self, name, text, root=None):
        path = (root or self.root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def git(self, root, *args):
        result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def fake_job(self, root, command, **kwargs):
        if command[0] == "lake":
            self.build_calls.append(command)
            self.assertNotIn("--version", command)
            return subprocess.CompletedProcess(command, 0, "✔ Built Example\n", "")
        return self.real_job(root, command, **kwargs)

    def submit(self, author="Primary", *, value=1, text="theorem first : True := by trivial\n"):
        tree = workflow.task_worktree(self.paths, author, self.cid)
        state = tasks.load(self.paths.forum)
        forum.configure(self.paths.forum, self.root, "optimizing", author)
        with patch.dict(os.environ, {"UNITY_OPTIMIZE_TASK_ID": self.cid, "UNITY_OPTIMIZE_WORKTREE": str(tree),
                                     "UNITY_OPTIMIZE_BASE_SHA": state["main_sha"], "UNITY_AGENT_NAME": author}):
            strategy = forum.register_strategy(author, self.cid, author + " simplification", author + " fixture proof simplification")
            assignment = forum.claim_strategy(author, strategy["strategy_id"])
            self.write("Example.lean", text, tree)
            self.git(tree, "add", "Example.lean")
            self.git(tree, "commit", "--quiet", "-m", "fixture candidate")
            candidate = forum.submit_candidate(author, assignment["assignment_id"], "Same statement; direct proof, fewer tactics")
        return candidate

    async def select_and_merge(self, candidate):
        await self.controller.check_submissions()
        state = tasks.load(self.paths.forum)
        checked = state["candidates"][candidate["candidate_id"]]
        self.assertEqual(checked["status"], "checked", checked.get("verification"))
        forum.configure(self.paths.forum, self.root, "optimizing", "Primary")
        selected = forum.select_candidate("Primary", candidate["candidate_id"], "Best checked fixture improvement")
        selected = tasks.load(self.paths.forum)["candidates"][selected["candidate_id"]]
        integrated = await self.controller.integrate_selected(selected)
        row = tasks.load(self.paths.forum)["candidates"][candidate["candidate_id"]]
        self.assertTrue(integrated, {"status": row["status"], "feedback": row.get("feedback"),
                                     "issues": row.get("verification", {}).get("issues")})

    def review_turns(self, *, value=1, no_improvement=False, change_during_review=False):
        async def dispatch(profile, *, primary=False, instruction=""):
            workflow.phase(self.paths, profile)
            tree = workflow.task_worktree(self.paths, "offline-" + profile, profile)
            forum.configure(self.paths.forum, self.root, profile, "Primary")
            if profile == "critic":
                snapshot = tasks.load(self.paths.forum)["review"]["snapshot"]
                if change_during_review:
                    self.write("Example.lean", "theorem first : True := by trivial\n-- changed after snapshot\n")
                forum.submit_optimization_verdict("Primary", snapshot["snapshot_id"], not no_improvement,
                                                  "Fixture source and metric evidence reviewed", no_improvement_found=no_improvement)
            else:
                self.fail("Unexpected fake phase " + profile)
            return [(None, None, tree)]
        return dispatch

    async def test_submit_select_integrate_refresh_primary_review_accepts(self):
        candidate = self.submit("Primary")
        await self.select_and_merge(candidate)
        state = tasks.load(self.paths.forum)
        self.assertEqual(state["candidates"][candidate["candidate_id"]]["status"], "merged")
        self.assertEqual(state["tasks"][self.cid]["status"], "complete")
        self.assertNotEqual(state["main_sha"], self.head)
        self.assertEqual(state["baseline"], self.baseline)
        self.assertTrue(metrics.validate_measurement_binding(state["measurements"][self.cid], self.metric,
                         verification.file_identity(self.root, self.scope)))

        # Real forum, verification and publication; deterministic model turns.
        with patch.object(self.controller, "dispatch_phase", side_effect=self.review_turns()):
            self.assertTrue(await self.controller.review())
        final = tasks.load(self.paths.forum)
        self.assertEqual(verification.completion_report(final)["status"], "accepted")
        self.assertEqual(final["review"]["verdict"]["author"], candidate["author"])
        self.assertEqual(final["rounds"]["completed"], 1)
        self.assertIsNone(final["rounds"]["active"])
        self.assertIn("final_report", final["final_report"])
        self.assertTrue(self.build_calls)

    async def test_competing_checked_candidate_becomes_stale_after_selected_merge(self):
        first = self.submit("Primary", value=1)
        second = self.submit("Peer", value=2, text="theorem first : True := by\n  have h := True.intro\n  exact h\n")
        await self.controller.check_submissions()
        state = tasks.load(self.paths.forum)
        self.assertTrue(all(row["status"] == "checked" for row in state["candidates"].values()))
        await self.select_and_merge(first)
        state = tasks.load(self.paths.forum)
        self.assertEqual(state["candidates"][second["candidate_id"]]["status"], "stale")
        self.assertFalse((await self.controller.check_candidate(state["candidates"][second["candidate_id"]]))["passed"])

    async def test_publication_crash_recovers_once_without_second_merge(self):
        candidate = self.submit()
        await self.controller.check_submissions()
        forum.configure(self.paths.forum, self.root, "optimizing", "Primary")
        selected = forum.select_candidate("Primary", candidate["candidate_id"], "Choose checked improvement")
        selected = tasks.load(self.paths.forum)["candidates"][selected["candidate_id"]]
        with patch.object(self.controller, "recover_integration", side_effect=OSError("offline interruption after fast-forward")):
            with self.assertRaises(OSError):
                await self.controller.integrate_selected(selected)
        published = self.git(self.root, "rev-parse", "HEAD")
        self.assertNotEqual(published, self.head)
        self.controller.recover_integration()
        first = tasks.load(self.paths.forum)
        self.controller.recover_integration()
        second = tasks.load(self.paths.forum)
        self.assertEqual(first, second)
        self.assertEqual(second["candidates"][candidate["candidate_id"]]["status"], "merged")
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), published)

    async def test_unassigned_file_patch_is_not_integrated(self):
        candidate = self.submit()
        tree = Path(tasks.load(self.paths.forum)["candidates"][candidate["candidate_id"]]["worktree"])
        self.write("Other.lean", "def unrelated := 1\n", tree)
        self.git(tree, "add", "Other.lean")
        self.git(tree, "commit", "--quiet", "-m", "unassigned change")
        await self.controller.check_submissions()
        state = tasks.load(self.paths.forum)
        rejected = state["candidates"][candidate["candidate_id"]]
        self.assertEqual(rejected["status"], "rejected")
        self.assertIn("unassigned", rejected["verification"]["reason"])
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), self.head)

    async def test_no_improvement_is_honest_nonaccepted_terminal_outcome(self):
        tree = workflow.task_worktree(self.paths, "Primary-noop", self.cid)
        forum.configure(self.paths.forum, self.root, "optimizing", "Primary")
        with patch.dict(os.environ, {"UNITY_OPTIMIZE_TASK_ID": self.cid, "UNITY_OPTIMIZE_WORKTREE": str(tree),
                                     "UNITY_OPTIMIZE_BASE_SHA": self.head, "UNITY_AGENT_NAME": "Primary"}):
            strategy = forum.register_strategy("Primary", self.cid, "Noop fixture", "Try an approach without a valid improvement")
            assignment = forum.claim_strategy("Primary", strategy["strategy_id"])
            forum.record_no_improvement("Primary", assignment["assignment_id"], "No better checked candidate found", "Retained baseline score 3")
        async def exited():
            return None
        worker = asyncio.create_task(exited())
        await worker
        self.controller.workers["Primary"] = {"task": worker, "interrupt": asyncio.Event(), "task_id": self.cid, "tree": tree}
        await self.controller.reap_workers()
        self.controller.close_unsuccessful_tasks()
        with patch.object(self.controller, "dispatch_phase", side_effect=self.review_turns(value=3, no_improvement=True)):
            self.assertFalse(await self.controller.review())
        state = tasks.load(self.paths.forum)
        self.assertEqual(state["phase"], "no_improvement")
        self.assertEqual(verification.completion_report(state)["status"], "no_improvement_found")
        self.assertFalse(state["review"]["verdict"]["approved"])
        self.assertEqual(state["baseline"], self.baseline)

    async def test_source_change_during_primary_review_blocks_acceptance(self):
        candidate = self.submit()
        await self.select_and_merge(candidate)
        with patch.object(self.controller, "dispatch_phase", side_effect=self.review_turns(change_during_review=True)):
            with self.assertRaisesRegex(ValueError, "Source or metric changed"):
                await self.controller.review()
        self.assertNotEqual(tasks.load(self.paths.forum)["phase"], "complete")

    async def test_grouped_candidate_submits_without_measurements_then_verification_scores_all(self):
        # A separate real repository keeps the existing single-chunk fixtures
        # unchanged while exercising a group the size of the observed failure.
        real_job = self.real_job
        def git_only(root, command, **kwargs):
            self.assertEqual(command[0], "git", "no model or native probes in the offline grouped regression")
            return real_job(root, command, **kwargs)
        self.real_job = git_only
        self.root = self.root / "grouped-project"
        self.root.mkdir()
        self.git(self.root, "init", "--quiet")
        self.git(self.root, "config", "user.name", "Fixture")
        self.git(self.root, "config", "user.email", "fixture@localhost")
        self.write(".gitignore", ".unity/\n.worktrees/\n.lake/\n.env\n")
        source = "".join(f"theorem grouped_{index:02d} : True := by\n  have h : True := by trivial\n  exact h\n"
                         for index in range(23))
        self.write("Example.lean", source)
        self.write("lean-toolchain", "leanprover/lean4:v4.34.1\n")
        self.write("lakefile.toml", 'name = "Example"\n')
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "--quiet", "-m", "grouped fixture baseline")
        self.write(".unity/metrics/length.md", self.metric["definition"])
        self.head = self.git(self.root, "rev-parse", "HEAD")
        self.paths = workflow.paths_for(self.root)
        tasks.initialize(self.paths.forum, self.root, run_id="optimize-grouped-offline-fixture", base_sha=self.head,
                         metric=self.metric, scope=self.scope,
                         config={"primary": "Primary", "agents": ["Primary", "Peer"], "targets": "Universe", "lake_args": []})
        tasks.set_baseline(self.paths.forum, verification.capture_baseline(self.root, metric=self.metric, scope=self.scope))
        inventory = tasks.scan_sources(self.root, self.scope["files"])
        tasks.install_inventory(self.paths.forum, inventory)
        chunk_ids = sorted(inventory["chunks"])
        self.assertEqual(len(chunk_ids), 23)
        changed_chunk = next(cid for cid, row in inventory["chunks"].items() if row["name"] == "grouped_00")
        workflow.phase(self.paths, "chunking")
        forum.configure(self.paths.forum, self.root, "chunking", "Primary")
        plan = forum.propose_chunk_plan("Primary", [{"chunk_ids": chunk_ids,
            "reason": "These 23 declarations share Example.lean; group their overlapping file edit scope"}],
            "Same-file grouped declaration fixture")
        tasks.accept_plan(self.paths.forum, plan["proposal_id"])
        for chunk_id in chunk_ids:
            self.seed_measurement(chunk_id)
        tasks.seal_baseline(self.paths.forum)
        with tasks.transaction(self.paths.forum) as state:
            state["measurements"] = deepcopy(state["baseline"]["measurements"])
        self.baseline = deepcopy(tasks.load(self.paths.forum)["baseline"])
        tasks.begin_round(self.paths.forum, max_rounds=5)
        self.controller = workflow.Controller(self.paths, self.roster, max_attempts=5)
        self.controller.evaluator = lambda: self.fake_evaluator
        task_id = next(iter(tasks.load(self.paths.forum)["tasks"]))
        tree = workflow.task_worktree(self.paths, "Primary-grouped", task_id)
        forum.configure(self.paths.forum, self.root, "optimizing", "Primary")
        env = {"UNITY_OPTIMIZE_TASK_ID": task_id, "UNITY_OPTIMIZE_WORKTREE": str(tree),
               "UNITY_OPTIMIZE_BASE_SHA": self.head, "UNITY_AGENT_NAME": "Primary"}
        with patch.dict(os.environ, env):
            strategy = forum.register_strategy("Primary", task_id, "Shorten one grouped proof",
                                               "Optimize one declaration and retain the other 22")
            assignment = forum.claim_strategy("Primary", strategy["strategy_id"])
            self.write("Example.lean", source.replace("by\n  have h : True := by trivial\n  exact h", "by trivial", 1), tree)
            self.git(tree, "add", "Example.lean")
            self.git(tree, "commit", "--quiet", "-m", "shorter grouped fixture proof")
            before = tasks.load(self.paths.forum)
            build_count = len(self.build_calls)
            candidate = forum.submit_candidate("Primary", assignment["assignment_id"],
                                               "One shorter proof; other 22 unchanged")
            submitted = tasks.load(self.paths.forum)
            self.assertEqual(submitted["rounds"], before["rounds"])
            self.assertEqual(submitted["baseline"], self.baseline)
            self.assertEqual(len(self.build_calls), build_count)
        await self.controller.check_submissions()
        checked = tasks.load(self.paths.forum)["candidates"][candidate["candidate_id"]]
        self.assertEqual(checked["status"], "checked", checked.get("verification"))
        self.assertEqual(len(checked["verification"]["comparisons"]), 23)
        self.assertEqual(sum(row["improved"] for row in checked["verification"]["comparisons"].values()), 1)
        self.assertTrue(all(row["passed"] for row in checked["verification"]["builds"]))
        selected = forum.select_candidate("Primary", candidate["candidate_id"], "All grouped measurements checked; one proof shorter")
        selected = tasks.load(self.paths.forum)["candidates"][selected["candidate_id"]]
        self.assertTrue(await self.controller.integrate_selected(selected))
        final = tasks.load(self.paths.forum)
        self.assertEqual(final["candidates"][candidate["candidate_id"]]["status"], "merged")
        self.assertEqual(final["tasks"][task_id]["status"], "complete")
        self.assertNotEqual(final["main_sha"], self.head)
        self.assertEqual(final["baseline"], self.baseline)
        integrated = verification.file_identity(self.root, self.scope)
        self.assertTrue(all(metrics.validate_measurement_binding(final["measurements"][cid], self.metric, integrated)
                            for cid in chunk_ids))


if __name__ == "__main__":
    unittest.main()
