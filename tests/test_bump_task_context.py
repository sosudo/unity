"""Paginated access to exact-task collaboration reports."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unity import bump_tasks
from unity.forum import bump_workflow_server as forum


class BumpTaskContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bump-task-context-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.forum_dir = self.root / "forum"
        bump_tasks.initialize(
            self.forum_dir, self.root, run_id="task-context", base_sha="a" * 40,
            version="v4.34.1", config={"revision": 1, "scope": {}},
        )
        with bump_tasks.transaction(self.forum_dir) as state:
            state["tasks"]["task-a"] = {"task_id": "task-a", "files": ["Shared.lean"]}
            state["tasks"]["task-b"] = {"task_id": "task-b", "files": ["Shared.lean"]}
            state["tasks"]["task-c"] = {"task_id": "task-c", "files": ["Other.lean"],
                                         "status": "grouped", "grouped_into": "task-a"}
            state["tasks"]["task-d"] = {"task_id": "task-d", "files": ["Empty.lean"],
                                         "status": "grouped", "grouped_into": "task-a"}
            state["tasks"]["task-e"] = {"task_id": "task-e", "files": ["Descendant.lean"],
                                         "status": "grouped", "grouped_into": "task-d"}
            state["strategies"]["strategy-a"] = {
                "strategy_id": "strategy-a", "task_id": "task-a",
            }
            bump_tasks._event(state, "finding", "Terra", task_id="task-a",
                              content="The old approach fails.", evidence="Lean error at line 42")
            bump_tasks._event(state, "obstacle", "Terra", task_id="task-a",
                              content="Cannot close the dependent equality.", evidence="fallback tested")
            bump_tasks._event(state, "question", "Luna", task_id="task-a",
                              content="Does simp close this goal?")
            bump_tasks._event(state, "answer", "Terra", task_id="task-a",
                              content="No; the HEq obligation remains.", question_id="event-3")
            bump_tasks._event(state, "assistance", "Sol", strategy_id="strategy-a",
                              contribution="Tried a typed equation; same blocker.")
            bump_tasks._event(state, "yield", "Terra", task_id="task-a",
                              reason="Need a different API migration.")
            bump_tasks._event(state, "finding", "Luna", task_id="task-b",
                              content="Different task, same file.", evidence="not applicable")
            bump_tasks._event(state, "claim", "Sol", task_id="task-a")
            bump_tasks._event(state, "obstacle", "Terra", task_id="task-c",
                              content="Earlier task report.", evidence="preserve by explicit grouping")
            bump_tasks._event(state, "finding", "Sol", task_id="task-e",
                              content="Nested grouped report.", evidence="descendant remains reachable")

    def call(self, task_id="task-a", **kwargs):
        with patch.object(forum, "FORUM_DIR", self.forum_dir), \
             patch.object(forum, "PROFILE", "repairing"), \
             patch.dict("os.environ", {"UNITY_BUMP_TASK_ID": "task-a"}):
            return forum.bump_task_context(task_id, **kwargs)

    def test_pages_exact_task_reports_and_preserves_yield_with_fixed_cutoff(self):
        state_path = self.forum_dir / bump_tasks.STATE_FILE
        before = state_path.read_bytes()
        first = self.call(limit=2)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(first["event_cutoff"], 10)
        self.assertEqual(first["total"], 6)
        self.assertEqual(first["grouped_predecessor_task_ids"], ["task-c", "task-e"])
        self.assertEqual(first["omitted_empty_grouped_predecessor_count"], 1)
        self.assertEqual(first["items"][0]["evidence"], "Lean error at line 42")

        with bump_tasks.transaction(self.forum_dir) as state:
            bump_tasks._event(state, "finding", "Sol", task_id="task-a",
                              content="Appended after first page", evidence="later")
        second = self.call(offset=first["next_offset"], limit=2,
                           event_cutoff=first["event_cutoff"])
        third = self.call(offset=second["next_offset"], limit=2,
                           event_cutoff=first["event_cutoff"])
        items = first["items"] + second["items"] + third["items"]
        self.assertEqual([row["event_id"] for row in items],
                         ["event-1", "event-2", "event-3", "event-4", "event-5", "event-6"])
        self.assertEqual([row["kind"] for row in items],
                         ["finding", "obstacle", "question", "answer", "assistance", "yield"])
        self.assertEqual(items[3]["question_id"], "event-3")
        self.assertEqual(items[4]["strategy_id"], "strategy-a")
        self.assertEqual(items[4]["content"], "Tried a typed equation; same blocker.")
        self.assertEqual(items[5]["reason"], "Need a different API migration.")
        self.assertIsNone(third["next_offset"])

        grouped = self.call("task-c")
        self.assertEqual(grouped["items"][0]["event_id"], "event-9")
        self.assertEqual(grouped["items"][0]["task_id"], "task-c")
        self.assertEqual(grouped["grouped_predecessor_task_ids"], [])
        unrelated = self.call("task-b")
        self.assertEqual([row["content"] for row in unrelated["items"]],
                         ["Different task, same file."])

    def test_empty_intermediates_and_cutoff_filter_grouped_predecessors(self):
        before_nested_report = self.call(event_cutoff=9)
        self.assertEqual(before_nested_report["grouped_predecessor_task_ids"], ["task-c"])
        self.assertEqual(before_nested_report["omitted_empty_grouped_predecessor_count"], 2)
        after_nested_report = self.call()
        self.assertEqual(after_nested_report["grouped_predecessor_task_ids"], ["task-c", "task-e"])
        self.assertIn("event prefix", after_nested_report["grouped_predecessor_qualification"])

    def test_grouped_cycles_terminate_and_preserve_reporting_node(self):
        with bump_tasks.transaction(self.forum_dir) as state:
            state["tasks"]["cycle-a"] = {"task_id": "cycle-a", "status": "grouped",
                                           "grouped_into": "cycle-b"}
            state["tasks"]["cycle-b"] = {"task_id": "cycle-b", "status": "grouped",
                                           "grouped_into": "cycle-a"}
            bump_tasks._event(state, "finding", "Terra", task_id="cycle-b",
                              content="Cycle node report.", evidence="reachable once")
        context = self.call("cycle-a")
        self.assertEqual(context["grouped_predecessor_task_ids"], ["cycle-b"])
        self.assertEqual(context["omitted_empty_grouped_predecessor_count"], 0)

    def test_requires_current_worker_task_and_valid_page_bounds(self):
        self.assertIn(forum.bump_task_context, forum.PROFILE_TOOLS["preparing"])
        self.assertIn(forum.bump_task_context, forum.PROFILE_TOOLS["repairing"])
        self.assertNotIn(forum.bump_task_context, forum.PROFILE_TOOLS["critic"])
        self.assertNotIn(forum.bump_task_context, forum.PROFILE_TOOLS["retrospective"])
        with patch.object(forum, "FORUM_DIR", self.forum_dir), \
             patch.object(forum, "PROFILE", "repairing"), \
             patch.dict("os.environ", {"UNITY_BUMP_TASK_ID": "task-a"}):
            self.assertEqual(forum.bump_task_context("task-b")["total"], 1)
            with self.assertRaisesRegex(ValueError, "unknown"):
                forum.bump_task_context("unassigned-task")
            with self.assertRaisesRegex(ValueError, "limit"):
                forum.bump_task_context("task-a", limit=51)
            with self.assertRaisesRegex(ValueError, "cutoff"):
                forum.bump_task_context("task-a", event_cutoff=99)
        with patch.object(forum, "FORUM_DIR", self.forum_dir), \
             patch.object(forum, "PROFILE", "critic"), \
             patch.dict("os.environ", {"UNITY_BUMP_TASK_ID": "task-a"}):
            with self.assertRaisesRegex(ValueError, "workers"):
                forum.bump_task_context("task-a")


if __name__ == "__main__":
    unittest.main()
