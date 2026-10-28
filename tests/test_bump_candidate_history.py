"""Bounded, exact-task controller outcome summaries."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity import bump_tasks
from unity.forum import bump_workflow_server as forum


class CandidateHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.forum = Path(directory.name) / "forum"
        bump_tasks.initialize(self.forum, Path(directory.name), run_id="history", base_sha="a" * 40,
                              version="v4.34.1", config={"scope": {}})
        with bump_tasks.transaction(self.forum) as state:
            state["tasks"]["one"] = bump_tasks._task("one", "command", [], ["A.lean"])
            state["tasks"]["two"] = bump_tasks._task("two", "command", [], ["A.lean"])

    def get(self, task="one"):
        with patch.object(forum, "FORUM_DIR", self.forum):
            return forum.bump_task(task)["candidate_outcomes"]

    def test_empty_history(self):
        result = self.get()
        self.assertEqual((result["total"], result["omitted"], result["items"]), (0, 0, []))
        self.assertIn("not final acceptance", result["qualification"])

    def test_exact_task_order_limit_feedback_and_read_only(self):
        error = {"path": "A.lean", "line": 42, "column": 3, "kind": "declaration", "message": "E" * 5000}
        feedback = {"accepted": False, "made_progress": False, "reason": "not reached",
                    "resolved_diagnostic_ids": ["d1", "d2"], "remaining_errors": [error] * 100,
                    "regressions": [{**error, "path": "Other.lean"}],
                    "build": {"command": ["lake", "--rehash", "build", "+A"],
                              "log_ref": {"artifact_id": "log-1", "sha256": "f" * 64,
                                          "metadata": {"big": "x" * 100000}}, "output": "x" * 100000}}
        with bump_tasks.transaction(self.forum) as state:
            for cid, timestamp, status in (("old", "2026-01-01", "rejected"),
                                           ("b", "2026-01-02", "merged"),
                                           ("a", "2026-01-02", "rejected"),
                                           ("new", "2026-01-03", "rejected")):
                state["candidates"][cid] = {"candidate_id": cid, "task_id": "one", "submitted": timestamp,
                    "status": status, "author": "Luna", "base_main_sha": "b" * 40,
                    "commit_sha": "c" * 40, "diff_sha256": "d" * 64, "notes": "n" * 5000,
                    **({"feedback": deepcopy(feedback)} if status == "rejected" else {})}
            state["candidates"]["other"] = {"candidate_id": "other", "task_id": "two", "submitted": "9999"}
        state_path = self.forum / bump_tasks.STATE_FILE
        original = state_path.read_bytes()
        result = self.get()
        self.assertEqual(state_path.read_bytes(), original)
        self.assertEqual((result["total"], result["omitted"]), (4, 1))
        self.assertEqual([r["candidate_id"] for r in result["items"]], ["new", "b", "a"])
        self.assertEqual(result["items"][1]["status"], "merged")
        self.assertIsNone(result["items"][1]["controller_feedback"])
        row = result["items"][0]
        self.assertEqual(row["diff_sha256"], "d" * 64)
        self.assertLessEqual(len(row["worker_notes"]), 401)
        fb = row["controller_feedback"]
        self.assertEqual((fb["accepted"], fb["made_progress"], fb["reason"]), (False, False, "not reached"))
        self.assertEqual(fb["resolved_diagnostic_count"], 2)
        self.assertEqual(fb["remaining_errors"]["count"], 100)
        self.assertEqual(len(fb["remaining_errors"]["items"]), 3)
        self.assertEqual(fb["regressions"]["items"][0]["path"], "Other.lean")
        self.assertEqual(fb["build_log_ref"]["artifact_id"], "log-1")
        self.assertLess(len(json.dumps(result)), 10000)

    def test_missing_feedback_fields_and_prioritizes_assigned_file(self):
        with bump_tasks.transaction(self.forum) as state:
            state["candidates"]["pending"] = {"candidate_id": "pending", "task_id": "one", "status": "submitted"}
            state["candidates"]["rejected"] = {"candidate_id": "rejected", "task_id": "one", "status": "rejected",
                "feedback": {"remaining_errors": [{"path": "Other.lean", "message": "other"}] * 4
                             + [{"path": "A.lean", "message": "assigned"}]}}
        rows = {r["candidate_id"]: r for r in self.get()["items"]}
        self.assertIsNone(rows["pending"]["controller_feedback"])
        feedback = rows["rejected"]["controller_feedback"]
        self.assertIsNone(feedback["resolved_diagnostic_count"])
        self.assertEqual(feedback["remaining_errors"]["items"][0]["message"], "assigned")
        self.assertEqual(feedback["remaining_errors"]["count"], 5)


if __name__ == "__main__":
    unittest.main()
