"""Snapshot-bound critic access to Bump agent reports; no model or Lean run."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unity import bump_tasks
from unity.forum import bump_workflow_server as forum


class BumpReviewContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bump-review-context-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.forum_dir = self.root / "forum"
        self.base = "a" * 40
        bump_tasks.initialize(
            self.forum_dir, self.root, run_id="review-context", base_sha=self.base,
            version="v4.34.1", config={"revision": 3, "scope": {}},
        )
        with bump_tasks.transaction(self.forum_dir) as state:
            state["phase"] = "critic"
            state["tasks"]["command-api"] = {
                "task_id": "command-api", "files": ["A.lean"],
            }
            state["strategies"]["strategy-1"] = {
                "strategy_id": "strategy-1", "task_id": "command-api",
            }
            state["review"] = {"snapshot": {
                "snapshot_id": "review-1", "main_sha": self.base,
                "config_revision": 3,
            }}
            bump_tasks._event(state, "finding", "Sol", task_id="command-api",
                              content="API changed", evidence="Lean source lines 1-2")
            bump_tasks._event(state, "obstacle", "Terra", task_id="command-api",
                              content="Unsafe initialization concern",
                              evidence="ImportingFlag threading contract")
            bump_tasks._event(state, "question", "Luna", task_id="command-api",
                              content="Is the flag scoped?")
            bump_tasks._event(state, "answer", "Terra", task_id="command-api",
                              content="The flag is process-global.", question_id="event-3")
            bump_tasks._event(state, "assistance", "Luna", strategy_id="strategy-1",
                              contribution="Checked the call sites.")
            bump_tasks._event(state, "claim", "Sol", task_id="command-api")

    def call(self, **kwargs):
        with patch.object(forum, "FORUM_DIR", self.forum_dir), patch.object(forum, "PROFILE", "critic"):
            return forum.bump_review_context("review-1", **kwargs)

    def test_pages_preserve_reports_and_evidence_at_fixed_event_cutoff_without_writes(self):
        state_path = self.forum_dir / bump_tasks.STATE_FILE
        before = state_path.read_bytes()
        first = self.call(limit=2)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(first["event_cutoff"], 6)
        self.assertEqual(first["total"], 5)
        self.assertEqual(first["qualification"],
                         "Agent-reported collaboration context, not verified findings or workflow status.")
        self.assertEqual(first["items"][1]["evidence"], "ImportingFlag threading contract")
        self.assertEqual(first["items"][1]["task_files"], ["A.lean"])

        with bump_tasks.transaction(self.forum_dir) as state:
            bump_tasks._event(state, "obstacle", "Sol", task_id="command-api",
                              content="Appended after review page one", evidence="later")
        second = self.call(offset=first["next_offset"], limit=2,
                           event_cutoff=first["event_cutoff"])
        third = self.call(offset=second["next_offset"], limit=2,
                          event_cutoff=first["event_cutoff"])
        items = first["items"] + second["items"] + third["items"]
        self.assertEqual([item["event_id"] for item in items],
                         ["event-1", "event-2", "event-3", "event-4", "event-5"])
        self.assertEqual(items[-1]["task_id"], "command-api")
        self.assertEqual(items[3]["question_id"], "event-3")
        self.assertEqual(items[-1]["strategy_id"], "strategy-1")
        self.assertEqual(items[-1]["content"], "Checked the call sites.")
        self.assertIsNone(third["next_offset"])
        self.assertEqual(third["total"], 5)

    def test_rejects_stale_snapshot_and_noncritic_profile(self):
        self.assertIn(forum.bump_review_context, forum.PROFILE_TOOLS["critic"])
        self.assertNotIn(forum.bump_review_context, forum.PROFILE_TOOLS["repairing"])
        with bump_tasks.transaction(self.forum_dir) as state:
            state["review"]["snapshot"]["snapshot_id"] = "review-2"
        with self.assertRaisesRegex(ValueError, "stale"):
            self.call()
        with patch.object(forum, "PROFILE", "repairing"), patch.object(forum, "FORUM_DIR", self.forum_dir):
            with self.assertRaisesRegex(ValueError, "critic"):
                forum.bump_review_context("review-2")


if __name__ == "__main__":
    unittest.main()
