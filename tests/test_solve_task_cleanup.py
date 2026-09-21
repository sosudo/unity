"""Accepted informal papers supersede unused work without claiming it was proved."""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from unity import solve_state
from unity.forum import solve_server


class SolveTaskCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.forum = self.root / ".unity" / "forum"
        solve_state.initialize(self.forum, "a" * 64, "b" * 40)

    def task(self, title, status="open", revision=1):
        task = solve_state.create_informal_task(
            self.forum, "Ada", "lemma", title, "Investigate " + title
        )["task"]
        with solve_state.transaction(self.forum) as state:
            state["informal_tasks"][task["task_id"]].update(
                status=status, solution_revision=revision
            )
        return task["task_id"]

    def candidate(self, sha="c" * 64):
        return solve_state.submit_solution_candidate(
            self.forum, "Ada", "artifact-paper", sha, "paper.tex"
        )["candidate"]["candidate_id"]

    def approve(self, candidate_id):
        solve_state.review_solution_candidate(
            self.forum, candidate_id, "Bert", "approve", "Complete proof checked"
        )

    def accept(self, candidate_id):
        return solve_state.accept_solution_candidate(self.forum, candidate_id, "Unity")

    def test_acceptance_supersedes_only_unfinished_current_revision_tasks(self):
        unfinished = {self.task(status, status) for status in (
            "open", "result_available", "blocked"
        )}
        unchanged = {self.task(status, status): status for status in (
            "resolved", "superseded", "cancelled"
        )}
        unchanged[self.task("old revision", revision=0)] = "open"
        before = solve_state.load_state(self.forum)["informal_tasks"]
        candidate_id = self.candidate()
        self.approve(candidate_id)
        self.accept(candidate_id)
        state = solve_state.load_state(self.forum)

        self.assertEqual(state["phase"], "chunking")
        for task_id in unfinished:
            task = state["informal_tasks"][task_id]
            self.assertEqual(task["status"], "superseded")
            self.assertEqual(task["superseded_by"], candidate_id)
            self.assertEqual(task["cancellation_reason"], "accepted complete informal solution")
            self.assertIsNone(task["resolved_result"])
            self.assertGreaterEqual(task["updated_at"], before[task_id]["updated_at"])
        for task_id, status in unchanged.items():
            self.assertEqual(state["informal_tasks"][task_id], before[task_id])
            self.assertEqual(state["informal_tasks"][task_id]["status"], status)
        events = [event for event in state["events"]
                  if event["kind"] == "informal_task_superseded"]
        self.assertEqual({event["task_id"] for event in events}, unfinished)
        self.assertTrue(all(event["superseded_by"] == candidate_id for event in events))
        self.assertEqual(solve_state.ready_informal_tasks(state), [])

    def test_incorporated_component_stays_resolved(self):
        task_id = self.task("Useful component")
        strategy = solve_state.register_strategy(
            self.forum, "Ada", "Prove the component", target=task_id
        )["strategy"]["strategy_id"]
        solve_state.claim_strategy(self.forum, strategy, "Ada")
        result = solve_state.submit_informal_result(
            self.forum, "Ada", task_id, strategy, "artifact-component", "d" * 64,
            "component.md", "Component proof",
        )["result"]
        solve_state.review_informal_result(
            self.forum, result["result_id"], "Bert", "support", "Checked"
        )
        unused_id = self.task("Unneeded alternative")
        candidate_id = self.candidate()
        self.approve(candidate_id)
        self.accept(candidate_id)
        state = solve_state.load_state(self.forum)

        self.assertEqual(state["informal_tasks"][task_id]["status"], "resolved")
        self.assertEqual(state["informal_tasks"][task_id]["resolved_result"], result["result_id"])
        self.assertNotIn("superseded_by", state["informal_tasks"][task_id])
        self.assertEqual(state["informal_results"][result["result_id"]]["status"], "incorporated")
        self.assertEqual(state["informal_tasks"][unused_id]["status"], "superseded")

    def test_corrected_candidate_resolves_repair_before_superseding_unused_work(self):
        unused_id = self.task("Unused alternative")
        rejected_id = self.candidate()
        review = solve_state.review_solution_candidate(
            self.forum, rejected_id, "Bert", "object", "Missing endpoint",
            issues=[{"kind": "missing_case", "description": "Prove the endpoint"}],
        )
        issue_id = review["review"]["issues"][0]
        solve_state.reject_solution_candidate(self.forum, rejected_id, "Unity", "Fix endpoint")
        corrected_id = self.candidate(sha="e" * 64)
        self.approve(corrected_id)
        self.accept(corrected_id)
        state = solve_state.load_state(self.forum)
        issue = state["review_issues"][issue_id]
        repair = state["informal_tasks"][issue["repair_task"]]

        self.assertEqual(issue["status"], "resolved")
        self.assertEqual(issue["resolved_by"], corrected_id)
        self.assertEqual(repair["status"], "resolved")
        self.assertNotIn("superseded_by", repair)
        self.assertEqual(state["informal_tasks"][unused_id]["status"], "superseded")

    def test_brief_hides_superseded_tasks(self):
        self.task("Obsolete speculation")
        self.task("Verified component", status="resolved")
        candidate_id = self.candidate()
        self.approve(candidate_id)
        self.accept(candidate_id)
        with patch.object(solve_server, "FORUM_DIR", self.forum), \
             patch.object(solve_server, "PROJECT_ROOT", self.root), \
             patch.object(solve_server, "PROFILE", "solving"), \
             patch.dict("os.environ", {"UNITY_AGENT_NAME": "Ada"}):
            brief = solve_server.solve_brief("Ada")
        self.assertNotIn("Obsolete speculation", brief)
        self.assertIn("Verified component", brief)
        self.assertIn("1/1 tasks resolved", brief)
        self.assertIn(candidate_id, brief)

    def test_reopen_allows_new_task_without_reviving_superseded_revision(self):
        old_task_id = self.task("Alternative argument")
        candidate_id = self.candidate()
        self.approve(candidate_id)
        self.accept(candidate_id)
        old_task = solve_state.load_state(self.forum)["informal_tasks"][old_task_id]
        solve_state.reopen_solution(self.forum, "Bert", "Investigate a stronger statement")
        new_task = solve_state.create_informal_task(
            self.forum, "Ada", "lemma", "Alternative argument", "Prove the stronger version"
        )
        state = solve_state.load_state(self.forum)

        self.assertEqual(new_task["status"], "created")
        self.assertEqual(new_task["task"]["solution_revision"], 2)
        self.assertNotEqual(old_task_id, new_task["task"]["task_id"])
        self.assertEqual(state["informal_tasks"][old_task_id], old_task)
        self.assertEqual(
            [task["task_id"] for task in solve_state.ready_informal_tasks(state)],
            [new_task["task"]["task_id"]],
        )

    def test_failed_acceptance_does_not_supersede_tasks(self):
        task_id = self.task("Still useful work")
        candidate_id = self.candidate()
        before = solve_state.load_state(self.forum)
        with self.assertRaisesRegex(ValueError, "no independent approval"):
            self.accept(candidate_id)
        self.assertEqual(solve_state.load_state(self.forum), before)
        self.assertEqual(before["informal_tasks"][task_id]["status"], "open")

    def test_duplicate_concurrent_acceptance_cannot_repeat_cleanup_events(self):
        self.task("Obsolete work")
        candidate_id = self.candidate()
        self.approve(candidate_id)
        barrier = Barrier(2)

        def accept_once():
            barrier.wait(timeout=5)
            try:
                self.accept(candidate_id)
                return "accepted"
            except ValueError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(accept_once) for _ in range(2)]
            outcomes = [future.result(timeout=10) for future in futures]
        self.assertCountEqual(outcomes, ["accepted", "candidate is not reviewable"])
        state = solve_state.load_state(self.forum)
        for kind in ("informal_task_superseded", "solution_candidate_accepted"):
            self.assertEqual(sum(event["kind"] == kind for event in state["events"]), 1)
        with self.assertRaisesRegex(ValueError, "candidate is not reviewable"):
            self.accept(candidate_id)
        self.assertEqual(solve_state.load_state(self.forum), state)

    def test_late_reviews_cannot_revive_closed_or_stale_tasks(self):
        for scenario in ("accepted", "cancelled", "stale_revision"):
            with self.subTest(scenario=scenario):
                solve_state.initialize(self.forum, "a" * 64, "b" * 40, reset=True)
                task_id = self.task("Unused component")
                strategy = solve_state.register_strategy(
                    self.forum, "Ada", "Prove a component", target=task_id
                )["strategy"]["strategy_id"]
                solve_state.claim_strategy(self.forum, strategy, "Ada")
                result_id = solve_state.submit_informal_result(
                    self.forum, "Ada", task_id, strategy, "artifact-unused", "f" * 64,
                    "unused.md", "A component not needed by the final paper",
                )["result"]["result_id"]
                if scenario == "accepted":
                    candidate_id = self.candidate()
                    self.approve(candidate_id)
                    self.accept(candidate_id)
                else:
                    with solve_state.transaction(self.forum) as state:
                        task = state["informal_tasks"][task_id]
                        if scenario == "cancelled":
                            task["status"] = "cancelled"
                        else:
                            task["solution_revision"] = 0
                before = solve_state.load_state(self.forum)
                for verdict in ("support", "object"):
                    with self.subTest(verdict=verdict):
                        message = "component review is closed" if scenario == "accepted" else "stale or closed task"
                        with self.assertRaisesRegex(ValueError, message):
                            solve_state.review_informal_result(
                                self.forum, result_id, "Bert", verdict, "Delayed review"
                            )
                        self.assertEqual(solve_state.load_state(self.forum), before)


if __name__ == "__main__":
    unittest.main()
