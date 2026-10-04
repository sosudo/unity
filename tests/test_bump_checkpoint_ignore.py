"""Real-Git checkpoint regressions; no Lean, services, or model calls."""

import base64
from copy import deepcopy
import json
import unittest

import test_bump_worktree_preservation as preservation
from unity import artifacts, bump_representation, bump_state
from unity.forum import bump_server


class BumpCheckpointIgnoreTests(unittest.TestCase):
    def setUp(self):
        # Compose the existing fixture instead of inheriting its whole test suite.
        for owner, keys in (
            (bump_server, ("FORUM_DIR", "PROJECT_ROOT", "PROFILE")),
            (bump_server.discussion, ("FORUM_DIR", "PROJECT_ROOT", "ICRL_ENABLED")),
        ):
            for key in keys:
                self.addCleanup(setattr, owner, key, getattr(owner, key))
        self.fixture = preservation.BumpWorktreePreservationTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.root, self.tree, self.forum = self.fixture.root, self.fixture.tree, self.fixture.forum

        # Poly has regular tracked files beneath an ignored parent, not a gitlink.
        website = self.root / "blueprint/web"
        website.mkdir(parents=True)
        for index in range(21):
            (website / f"page-{index:02}.html").write_text(f"original page {index}\n")
        self.git(self.root, "add", "--", "blueprint/web")
        ignore = self.root / ".gitignore"
        ignore.write_text(ignore.read_text() + "blueprint/web/\n")
        self.git(self.root, "add", "--", ".gitignore")
        self.git(self.root, "commit", "-qm", "tracked website under ignored parent")
        self.main = self.git(self.root, "rev-parse", "HEAD")
        with bump_state.transaction(self.forum) as current:
            current["formalization"]["main_sha"] = self.main
        self.git(self.tree, "merge", "--ff-only", self.main)
        inventory = self.git(self.tree, "ls-files", "--stage", "--", "blueprint/web").splitlines()
        self.assertEqual(len(inventory), 21)
        self.assertTrue(all(row.startswith("100644 ") for row in inventory))
        self.assertIn("blueprint/web/page-00.html", self.git(
            self.tree, "check-ignore", "--no-index", "--", "blueprint/web/page-00.html",
        ))

    @staticmethod
    def git(root, *args):
        return preservation.git(root, *args)

    def private_changes(self):
        (self.tree / "blueprint/web/page-00.html").write_text("private tracked website edit\n")
        (self.tree / "blueprint/web/page-01.html").unlink()
        (self.tree / "private-visible.txt").write_text("new nonignored private work\n")
        (self.tree / "blueprint/web/private.html").write_text("new ignored private work\n")
        (self.tree / ".unity/checkpoint-runtime-sentinel").write_text("shared runtime\n")
        (self.tree / ".lake/checkpoint-cache-sentinel").write_text("private cache\n")

    def assert_checkpoint(self, checkpoint):
        reference = checkpoint["ref"]
        self.assertEqual(self.git(self.root, "rev-parse", reference), checkpoint["commit_sha"])
        names = set(self.git(self.root, "ls-tree", "-r", "--name-only", reference).splitlines())
        self.assertEqual(self.git(self.root, "show", reference + ":blueprint/web/page-00.html"),
                         "private tracked website edit")
        self.assertNotIn("blueprint/web/page-01.html", names)
        self.assertEqual(self.git(self.root, "show", reference + ":private-visible.txt"),
                         "new nonignored private work")
        self.assertNotIn("blueprint/web/private.html", names)
        self.assertFalse(any(name == ".unity" or name.startswith(".unity/")
                             or name == ".lake" or name.startswith(".lake/") for name in names))
        archived = json.loads(artifacts.artifact_bytes(
            self.root / ".unity/artifacts", checkpoint["ignored_artifact"],
        ))
        self.assertEqual({row["path"] for row in archived["files"]}, {"blueprint/web/private.html"})
        self.assertEqual(base64.b64decode(archived["files"][0]["data"]), b"new ignored private work\n")
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), self.main)
        self.assertEqual((self.root / "blueprint/web/page-00.html").read_text(), "original page 0\n")
        self.assertEqual((self.root / "blueprint/web/page-01.html").read_text(), "original page 1\n")
        self.assertEqual((self.tree / ".unity/checkpoint-runtime-sentinel").read_text(), "shared runtime\n")
        self.assertEqual((self.tree / ".lake/checkpoint-cache-sentinel").read_text(), "private cache\n")
        self.assertEqual((self.tree / ".unity").resolve(), self.root / ".unity")
        self.assertEqual((self.tree / ".lake/packages").resolve(), self.root / ".lake/packages")

    def test_checkpoint_preserves_tracked_ignored_edits_deletions_and_private_files(self):
        self.private_changes()
        current = bump_state.load_state(self.forum)
        checkpoint = bump_server._checkpoint_task_worktree(
            self.tree, "Ada", "sharpness", current["formal_tasks"]["sharpness"]["revision"],
        )
        self.assert_checkpoint(checkpoint)
        self.assertEqual(bump_server._saved_task_checkpoint("Ada", "sharpness", current), checkpoint)

    def test_prerequisite_handoff_restores_tracked_ignored_changes_and_archived_new_file(self):
        repair = self.fixture.rejected_prerequisite()
        self.fixture.claim("sharpness")
        current = bump_state.load_state(self.forum)
        bump_server._record_worktree_assignment(
            "Ada", "sharpness", current["formal_tasks"]["sharpness"]["revision"], current,
        )
        self.private_changes()
        parked = bump_server.prepare_formal_worktree(
            "Ada", "sharpness", "existence", repair_id=repair["repair_id"],
            repair_input_sha256=repair["input_sha256"],
        )
        self.assertTrue(parked["ok"], parked)
        self.assert_checkpoint(parked["parked_checkpoint"])
        self.assertEqual((self.tree / "blueprint/web/page-00.html").read_text(), "original page 0\n")
        self.assertTrue((self.tree / "blueprint/web/page-01.html").is_file())
        self.assertFalse((self.tree / "private-visible.txt").exists())
        self.assertFalse((self.tree / "blueprint/web/private.html").exists())

        # Existing fixture's independent representation-review transition; no model.
        self.fixture.complete("existence")
        with bump_state.transaction(self.forum) as current:
            contract = current["formalization"]["contract"]
            contract["targets"]["existence"]["fingerprint"] = "f" * 64
            contract["sha256"] = bump_state._contract_digest(contract)
            bump_representation.queue_representation_review(current, "existence")
        claim = bump_representation.claim_representation_review(self.forum, "existence", "Reviewer")
        self.assertEqual(claim["status"], "claimed")
        review = bump_representation.representation_review_input(
            bump_state.load_state(self.forum), "existence",
        )
        bump_representation.submit_representation_review(self.forum, "Reviewer", "existence", {
            "input_sha256": review["input_sha256"], "verdict": "aligned",
            "checked_anchor_ids": [row["id"] for row in review["anchors"]],
            "rationale": "The corrected fixture interface matches its source.",
            "evidence": "Canned fixture review, not a semantic model evaluation.",
        })
        restored = bump_server.prepare_formal_worktree("Ada", "existence", "sharpness")
        self.assertTrue(restored["ok"], restored)
        self.assertTrue(restored["checkpoint_restored"])
        self.assertEqual((self.tree / "blueprint/web/page-00.html").read_text(), "private tracked website edit\n")
        self.assertFalse((self.tree / "blueprint/web/page-01.html").exists())
        self.assertEqual((self.tree / "private-visible.txt").read_text(), "new nonignored private work\n")
        self.assertEqual((self.tree / "blueprint/web/private.html").read_text(), "new ignored private work\n")
        self.assertEqual(bump_state.load_state(self.forum)["worker_tasks"]["ada"], "sharpness")
        self.assert_checkpoint(parked["parked_checkpoint"])

    def test_completed_retirement_handoff_checkpoints_before_reset(self):
        self.fixture.claim("sharpness")
        self.fixture.complete("sharpness")
        current = bump_state.load_state(self.forum)
        previous = deepcopy(current["formal_tasks"]["sharpness"])
        bump_server._record_worktree_assignment("Ada", "sharpness", previous["revision"], current)
        self.private_changes()
        # A sibling repair can clear this declaration before its worker stops.
        # Completion here is diagnostic bookkeeping, not native acceptance.
        with bump_state.transaction(self.forum) as state:
            state["formal_tasks"]["sharpness"]["migration"] = {
                "kind": "declaration", "original_ids": ["sharpness"],
                "path": "Sharpness.lean", "module": "Sharpness",
            }
        result = bump_server.prepare_formal_worktree("Ada", "sharpness", "next")
        self.assertTrue(result["ok"], result)
        self.assert_checkpoint(result["parked_checkpoint"])
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), self.main)
        self.assertEqual((self.tree / "blueprint/web/page-00.html").read_text(), "original page 0\n")
        self.assertTrue((self.tree / "blueprint/web/page-01.html").is_file())
        self.assertFalse((self.tree / "private-visible.txt").exists())
        self.assertFalse((self.tree / "blueprint/web/private.html").exists())
        self.assertEqual(bump_state.load_state(self.forum)["worker_tasks"]["ada"], "next")


if __name__ == "__main__":
    unittest.main()
