"""Autoformalize task-boundary and explicit synchronization guards; real Git, no Lean/model calls."""

import fcntl
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unity import artifacts, autoformalize_representation, autoformalize_state, worktree
from unity.forum import autoformalize_server


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True,
    ).stdout.strip()


class AutoformalizeWorktreePreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.name", "Unity Test")
        git(self.root, "config", "user.email", "unity@example.test")
        (self.root / ".gitignore").write_text(".unity/\n.lake/\n*.scratch\n")
        (self.root / "Existence.lean").write_text("theorem existence : True := by sorry\n")
        (self.root / "Sharpness.lean").write_text("theorem sharpness : True := by sorry\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "scaffold")
        self.base = git(self.root, "rev-parse", "HEAD")
        self.forum = self.root / ".unity/forum"
        self.forum.mkdir(parents=True)
        (self.root / ".lake/packages").mkdir(parents=True)
        (self.root / ".unity/UNITY.md").write_text("Prove the fixture claims.\n")
        paper_text = "The claims follow by True.intro.\n"
        source_dir = self.root / ".unity/source"
        source_dir.mkdir()
        (source_dir / "PROOF.tex").write_text(paper_text)
        artifact = artifacts.store_text(self.root / ".unity/artifacts", paper_text, kind="supplied_source")
        source_sha = hashlib.sha256(paper_text.encode()).hexdigest()
        self.source = {
            "kind": "supplied_sources", "candidate_id": "source-" + source_sha,
            "sha256": source_sha,
            "source_refs": [{"ref_id": "source:PROOF.tex", "path": ".unity/source/PROOF.tex",
                             "sha256": source_sha, "artifact_id": artifact["artifact_id"]}],
        }
        autoformalize_state.initialize_source(
            self.forum, hashlib.sha256((self.root / ".unity/UNITY.md").read_bytes()).hexdigest(),
            self.base, self.source, reset=True,
        )
        chunks = [{"id": task, "dependencies": [], "lean_decl": task,
                   "lean_file": task.title() + ".lean", "source_components": ["source:PROOF.tex"]}
                  for task in ("existence", "sharpness", "next")]
        requirements = [{"id": "R1", "statement": "The source claims and arguments are faithfully formalized.",
                         "source_components": ["source:PROOF.tex"],
                         "tasks": [row["id"] for row in chunks], "anchor_ids": ["A1"]}]
        spec = {"version": 1,
                "anchors": [{"id": "A1", "source_ref": "source:PROOF.tex",
                             "location": "Main claims", "excerpt": paper_text.strip()}],
                "scope": {"targets": ["A1"], "references": [], "excluded": []},
                "prerequisites": [],
                "arguments": [{"requirement_id": "R1", "anchor_ids": ["A1"],
                               "outline": "Use True.intro.", "prerequisites": [], "repair_ids": []}]}
        contract = {"version": 2, "targets": {row["lean_decl"]: {} for row in chunks},
                    "environment": {}, "solution_candidate": self.source["candidate_id"],
                    "solution_sha256": self.source["sha256"], "requirements": requirements,
                    "spec": spec, "spec_sha256": autoformalize_state.digest(spec)}
        contract["sha256"] = autoformalize_state._contract_digest(contract)
        autoformalize_state.initialize_formal_tasks(
            self.forum, chunks, solution_candidate=self.source["candidate_id"],
            solution_sha256=self.source["sha256"], main_sha=self.base,
            requirements=requirements, contract=contract,
        )
        autoformalize_server.configure(self.forum, self.root, "formalizing")
        self.tree = worktree.create_worktree("Ada", self.root)
        worktree.symlink_lake_cache(self.tree, self.root)

    def claim(self, task="existence", owner="Ada", assistant=""):
        strategy = autoformalize_state.register_strategy(
            self.forum, owner, f"prove {task}", target=task,
        )["strategy"]
        autoformalize_state.claim_strategy(self.forum, strategy["strategy_id"], owner)
        if assistant:
            autoformalize_state.assist_strategy(self.forum, strategy["strategy_id"], assistant)
        return strategy["strategy_id"]

    def local_commit(self, filename="Existence.lean", content="theorem existence : True := by trivial\n"):
        (self.tree / filename).write_text(content)
        git(self.tree, "add", filename)
        git(self.tree, "commit", "-qm", "local proof")
        return git(self.tree, "rev-parse", "HEAD")

    def accept_main_change(self, filename="Sharpness.lean", content="theorem sharpness : True := by trivial\n"):
        (self.root / filename).write_text(content)
        git(self.root, "add", filename)
        git(self.root, "commit", "-qm", "accepted main proof")
        sha = git(self.root, "rev-parse", "HEAD")
        with autoformalize_state.transaction(self.forum) as state:
            state["formalization"]["main_sha"] = sha
        return sha

    def complete(self, task):
        with autoformalize_state.transaction(self.forum) as state:
            state["formal_tasks"][task]["status"] = "complete"
            for strategy in state["strategies"].values():
                if strategy.get("target") == task:
                    strategy["status"] = "succeeded"

    def rejected_prerequisite(self):
        with autoformalize_state.transaction(self.forum) as state:
            contract = state["formalization"]["contract"]
            contract.update(version=3, representation_review_policy=1,
                            bindings={"existence": [{"declaration": "existence", "file": "Existence.lean"}]})
            contract["targets"]["existence"]["fingerprint"] = "e" * 64
            contract["sha256"] = autoformalize_state._contract_digest(contract)
            state["formal_tasks"]["existence"].update(
                representation={"status": "adopted"}, statement_dependencies=[], proof_dependencies=[],
                outputs=contract["bindings"]["existence"],
            )
            state["formal_tasks"]["sharpness"].update(
                statement_dependencies=[], proof_dependencies=["existence"], dependencies=["existence"],
                representation={"status": "missing", "candidate_id": None}, outputs=[],
            )
            state["worker_tasks"]["ada"] = "sharpness"
            row = autoformalize_representation.queue_representation_review(state, "existence")
            state["representation_reviews"][row["input_sha256"]]["status"] = "encoding_error"
        current = autoformalize_state.reconcile_rejected_representations(self.forum)
        return autoformalize_state.current_manifest_repairs(current, "existence")[0]

    def test_exact_repair_can_checkpoint_proof_prerequisite_reassignment(self):
        repair = self.rejected_prerequisite()
        strategy_id = self.claim("sharpness")
        self.local_commit("Sharpness.lean", "private proof progress\n")
        (self.tree / "Sharpness.lean").write_text("unfinished private improvement\n")
        (self.tree / "notes.scratch").write_text("private ignored proof notes\n")
        result = autoformalize_server.prepare_formal_worktree(
            "Ada", "sharpness", "existence", repair_id=repair["repair_id"],
            repair_input_sha256=repair["input_sha256"],
        )
        self.assertTrue(result["ok"], result)
        checkpoint = result["parked_checkpoint"]
        self.assertEqual(checkpoint["task_id"], "sharpness")
        self.assertEqual(git(self.root, "show", checkpoint["ref"] + ":Sharpness.lean"),
                         "unfinished private improvement")
        self.assertTrue(checkpoint["ignored_artifact"])
        current = autoformalize_state.load_state(self.forum)
        self.assertEqual(current["worker_tasks"]["ada"], "existence")
        self.assertEqual(current["strategies"][strategy_id]["status"], "registered")
        self.assertEqual(autoformalize_server.unresolved_formal_tasks(current, "Ada"), [])
        self.assertEqual(current["task_yields"]["ada"]["sharpness"]["waiting_for"], ["existence"])

    def test_failed_repair_checkpoint_reset_retains_claim(self):
        repair = self.rejected_prerequisite()
        strategy_id = self.claim("sharpness")
        (self.tree / "Sharpness.lean").write_text("unfinished private proof\n")
        with patch.object(autoformalize_server, "_reset_formal_assignment", return_value={"ok": False}):
            result = autoformalize_server.prepare_formal_worktree(
                "Ada", "sharpness", "existence", repair_id=repair["repair_id"],
                repair_input_sha256=repair["input_sha256"],
            )
        self.assertFalse(result["ok"])
        current = autoformalize_state.load_state(self.forum)
        self.assertEqual(current["strategies"][strategy_id]["status"], "claimed")
        self.assertEqual(current["strategies"][strategy_id]["owner"], "Ada")
        self.assertEqual((self.tree / "Sharpness.lean").read_text(), "unfinished private proof\n")

    def test_return_from_repaired_prerequisite_restores_checkpoint_and_ignored_files(self):
        repair = self.rejected_prerequisite()
        self.claim("sharpness")
        with autoformalize_state.transaction(self.forum) as current:
            autoformalize_server._record_worktree_assignment(
                "Ada", "sharpness", current["formal_tasks"]["sharpness"]["revision"], current,
            )
        self.local_commit("Sharpness.lean", "private proof commit\n")
        (self.tree / "Sharpness.lean").write_text("valuable unfinished proof\n")
        (self.tree / "notes.scratch").write_text("valuable ignored notes\n")
        parked = autoformalize_server.prepare_formal_worktree(
            "Ada", "sharpness", "existence", repair_id=repair["repair_id"],
            repair_input_sha256=repair["input_sha256"],
        )
        self.assertTrue(parked["ok"], parked)
        self.assertFalse((self.tree / "notes.scratch").exists())
        # Model a newly integrated, corrected interface, then use the real
        # independent-review transition for its new semantic identity.
        self.complete("existence")
        with autoformalize_state.transaction(self.forum) as current:
            contract = current["formalization"]["contract"]
            contract["targets"]["existence"]["fingerprint"] = "f" * 64
            contract["sha256"] = autoformalize_state._contract_digest(contract)
            autoformalize_representation.queue_representation_review(current, "existence")
        claim = autoformalize_representation.claim_representation_review(self.forum, "existence", "Reviewer")
        self.assertEqual(claim["status"], "claimed")
        payload = autoformalize_representation.representation_review_input(
            autoformalize_state.load_state(self.forum), "existence",
        )
        autoformalize_representation.submit_representation_review(self.forum, "Reviewer", "existence", {
            "input_sha256": payload["input_sha256"], "verdict": "aligned",
            "checked_anchor_ids": [row["id"] for row in payload["anchors"]],
            "rationale": "The corrected encoding matches the supplied source.",
            "evidence": "Exact fixture declaration and source anchors checked.",
        })
        restored = autoformalize_server.prepare_formal_worktree("Ada", "existence", "sharpness")
        self.assertTrue(restored["ok"], restored)
        self.assertTrue(restored["checkpoint_restored"])
        self.assertEqual((self.tree / "Sharpness.lean").read_text(), "valuable unfinished proof\n")
        self.assertEqual((self.tree / "notes.scratch").read_text(), "valuable ignored notes\n")
        self.assertEqual(autoformalize_state.load_state(self.forum)["worker_tasks"]["ada"], "sharpness")

    def test_failed_underlying_repair_reset_finishes_claim_release_on_retry(self):
        repair = self.rejected_prerequisite()
        strategy_id = self.claim("sharpness")
        (self.tree / "Sharpness.lean").write_text("unfinished private proof\n")
        kwargs = {"repair_id": repair["repair_id"], "repair_input_sha256": repair["input_sha256"]}
        with patch.object(worktree, "force_sync_from_main", return_value={"ok": False}):
            first = autoformalize_server.prepare_formal_worktree("Ada", "sharpness", "existence", **kwargs)
        self.assertFalse(first["ok"])
        current = autoformalize_state.load_state(self.forum)
        self.assertEqual(current["strategies"][strategy_id]["status"], "claimed")
        second = autoformalize_server.prepare_formal_worktree("Ada", "sharpness", "existence", **kwargs)
        self.assertTrue(second["ok"], second)
        current = autoformalize_state.load_state(self.forum)
        self.assertEqual(current["worker_tasks"]["ada"], "existence")
        self.assertEqual(current["strategies"][strategy_id]["status"], "registered")
        self.assertEqual(autoformalize_server.unresolved_formal_tasks(current, "Ada"), [])
        checkpoint = current["worktree_checkpoints"]["ada"]["sharpness"]
        self.assertEqual(git(self.root, "show", checkpoint["ref"] + ":Sharpness.lean"),
                         "unfinished private proof")
        yielded = current["task_yields"]["ada"]["sharpness"]
        third = autoformalize_server.prepare_formal_worktree("Ada", "existence", "existence", **kwargs)
        self.assertTrue(third["ok"], third)
        self.assertEqual(autoformalize_state.load_state(self.forum)["task_yields"]["ada"]["sharpness"], yielded)

    def test_repair_reset_interruption_before_forum_commit_recovers_related_release(self):
        repair = self.rejected_prerequisite()
        strategy_id = self.claim("sharpness")
        (self.tree / "Sharpness.lean").write_text("durably saved private proof\n")
        kwargs = {"repair_id": repair["repair_id"], "repair_input_sha256": repair["input_sha256"]}
        with patch.object(autoformalize_server, "_finish_repair_handoff", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                autoformalize_server.prepare_formal_worktree("Ada", "sharpness", "existence", **kwargs)
        self.assertEqual(autoformalize_state.load_state(self.forum)["strategies"][strategy_id]["status"], "claimed")
        with patch.object(worktree, "force_sync_from_main") as sync:
            result = autoformalize_server.prepare_formal_worktree("Ada", "sharpness", "existence", **kwargs)
        self.assertTrue(result["ok"], result)
        sync.assert_not_called()  # The successful Git reset is not repeated.
        current = autoformalize_state.load_state(self.forum)
        self.assertEqual(current["strategies"][strategy_id]["status"], "registered")
        self.assertEqual(autoformalize_server.unresolved_formal_tasks(current, "Ada"), [])
        checkpoint = autoformalize_server._saved_task_checkpoint("Ada", "sharpness", current)
        self.assertEqual(git(self.root, "show", checkpoint["ref"] + ":Sharpness.lean"),
                         "durably saved private proof")

    def pending_repair_handoff(self):
        repair = self.rejected_prerequisite()
        strategy_id = self.claim("sharpness")
        kwargs = {"repair_id": repair["repair_id"], "repair_input_sha256": repair["input_sha256"]}
        with patch.object(worktree, "force_sync_from_main", return_value={"ok": False}):
            self.assertFalse(autoformalize_server.prepare_formal_worktree(
                "Ada", "sharpness", "existence", **kwargs)["ok"])
        return strategy_id, kwargs

    def test_pending_repair_handoff_cannot_release_new_task_revision(self):
        strategy_id, kwargs = self.pending_repair_handoff()
        with autoformalize_state.transaction(self.forum) as current:
            current["formal_tasks"]["sharpness"]["revision"] += 1
        with patch.object(worktree, "force_sync_from_main") as sync:
            result = autoformalize_server.prepare_formal_worktree("Ada", "sharpness", "existence", **kwargs)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "repair_changed")
        sync.assert_not_called()
        self.assertEqual(autoformalize_state.load_state(self.forum)["strategies"][strategy_id]["status"], "claimed")

    def test_pending_repair_handoff_cannot_abandon_new_unrelated_claim(self):
        _, kwargs = self.pending_repair_handoff()
        autoformalize_state.yield_task(self.forum, "Ada", "sharpness", "Wait for its prerequisite.", ["existence"])
        strategy_id = self.claim("next")
        self.assertEqual(autoformalize_server.unresolved_formal_tasks(autoformalize_state.load_state(self.forum), "Ada"), ["next"])
        with patch.object(worktree, "force_sync_from_main") as sync:
            result = autoformalize_server.prepare_formal_worktree("Ada", "sharpness", "existence", **kwargs)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unresolved_work")
        sync.assert_not_called()
        self.assertEqual(autoformalize_state.load_state(self.forum)["strategies"][strategy_id]["status"], "claimed")

    def test_pending_repair_handoff_cannot_release_changed_supplied_source_binding(self):
        strategy_id, kwargs = self.pending_repair_handoff()
        with autoformalize_state.transaction(self.forum) as current:
            current["input_source"]["source_refs"][0]["path"] = ".unity/source/replaced.tex"
        with patch.object(worktree, "force_sync_from_main") as sync:
            result = autoformalize_server.prepare_formal_worktree("Ada", "sharpness", "existence", **kwargs)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "repair_changed")
        sync.assert_not_called()
        self.assertEqual(autoformalize_state.load_state(self.forum)["strategies"][strategy_id]["status"], "claimed")

    def test_stale_repair_permission_is_checked_before_worktree_mutation(self):
        repair = self.rejected_prerequisite()
        (self.tree / "Sharpness.lean").write_text("keep my work\n")
        with patch.object(autoformalize_server, "_checkpoint_task_worktree") as checkpoint, \
             patch.object(worktree, "force_sync_from_main") as sync:
            result = autoformalize_server.prepare_formal_worktree(
                "Ada", "sharpness", "existence", repair_id=repair["repair_id"],
                repair_input_sha256="stale",
            )
        self.assertEqual(result["reason"], "repair_changed")
        checkpoint.assert_not_called()
        sync.assert_not_called()
        self.assertEqual((self.tree / "Sharpness.lean").read_text(), "keep my work\n")

    def test_repair_does_not_abandon_unrelated_active_claim(self):
        repair = self.rejected_prerequisite()
        self.claim("next")
        (self.tree / "Sharpness.lean").write_text("keep my work\n")
        with patch.object(autoformalize_server, "_checkpoint_task_worktree") as checkpoint:
            result = autoformalize_server.prepare_formal_worktree(
                "Ada", "sharpness", "existence", repair_id=repair["repair_id"],
                repair_input_sha256=repair["input_sha256"],
            )
        self.assertFalse(result["ok"])
        self.assertIn(result["reason"], {"unresolved_work", "repair_changed"})
        checkpoint.assert_not_called()
        self.assertEqual((self.tree / "Sharpness.lean").read_text(), "keep my work\n")

    def test_same_task_preserves_dirty_and_committed_work_on_old_main(self):
        self.claim()
        head = self.local_commit()
        (self.tree / "Existence.lean").write_text("unfinished improvements\n")
        self.accept_main_change()
        result = autoformalize_server.prepare_formal_worktree("Ada", "existence", "existence")
        self.assertTrue(result["preserved"])
        self.assertEqual(git(self.tree, "rev-parse", "HEAD"), head)
        self.assertEqual((self.tree / "Existence.lean").read_text(), "unfinished improvements\n")

    def test_previous_pending_unregistered_task_cannot_be_discarded(self):
        (self.tree / "Existence.lean").write_text("unregistered progress\n")
        result = autoformalize_server.prepare_formal_worktree("Ada", "existence", "sharpness")
        self.assertEqual(result["reason"], "unresolved_work")
        self.assertEqual((self.tree / "Existence.lean").read_text(), "unregistered progress\n")

    def test_unresolved_assistance_blocks_switch_after_other_task_completed(self):
        self.claim("existence", "Bert", "Ada")
        self.complete("sharpness")
        result = autoformalize_server.prepare_formal_worktree("Ada", "sharpness", "next")
        self.assertEqual(result["reason"], "unresolved_work")
        self.assertEqual(autoformalize_server.unresolved_formal_tasks(autoformalize_state.load_state(self.forum), "ada"), ["existence"])

    def test_obsolete_and_released_assistance_does_not_block(self):
        strategy = self.claim("existence", "Bert", "Ada")
        autoformalize_state.release_strategy(self.forum, strategy, "Bert")
        self.assertEqual(autoformalize_server.unresolved_formal_tasks(autoformalize_state.load_state(self.forum), "Ada"), [])
        with autoformalize_state.transaction(self.forum) as state:
            state["strategies"][strategy].update(status="claimed", task_revision=0)
        self.assertEqual(autoformalize_server.unresolved_formal_tasks(autoformalize_state.load_state(self.forum), "Ada"), [])

    def test_completed_assignment_can_reset_obsolete_work_and_retains_runtime_cache(self):
        self.claim("sharpness")
        self.local_commit("Sharpness.lean", "old competing proof\n")
        (self.tree / "Sharpness.lean").write_text("obsolete dirt\n")
        (self.tree / "old.txt").write_text("obsolete untracked\n")
        (self.tree / "old.scratch").write_text("obsolete ignored\n")
        self.complete("sharpness")
        main = self.accept_main_change()
        result = autoformalize_server.prepare_formal_worktree("Ada", "sharpness", "next", expected_revision=1)
        self.assertTrue(result["ok"])
        self.assertEqual(git(self.tree, "rev-parse", "HEAD"), main)
        self.assertEqual((self.tree / "Sharpness.lean").read_bytes(), (self.root / "Sharpness.lean").read_bytes())
        self.assertFalse((self.tree / "old.txt").exists())
        self.assertFalse((self.tree / "old.scratch").exists())
        self.assertEqual((self.tree / ".unity").resolve(), self.root / ".unity")
        self.assertEqual((self.tree / ".lake/packages").resolve(), self.root / ".lake/packages")

    def test_fresh_clean_tree_fast_forwards_to_accepted_main(self):
        main = self.accept_main_change()
        result = autoformalize_server.prepare_formal_worktree("Ada", next_task="existence")
        self.assertTrue(result["ok"])
        self.assertEqual(git(self.tree, "rev-parse", "HEAD"), main)

    def test_unassigned_dirty_or_committed_progress_is_not_discarded(self):
        (self.tree / "Existence.lean").write_text("unfinished\n")
        result = autoformalize_server.prepare_formal_worktree("Ada", next_task="sharpness")
        self.assertEqual(result["reason"], "dirty_worktree")
        head = self.local_commit()
        result = autoformalize_server.prepare_formal_worktree("Ada", next_task="sharpness")
        self.assertEqual(result["reason"], "local_commits")
        self.assertEqual(git(self.tree, "rev-parse", "HEAD"), head)

    def test_pending_candidates_protect_exact_branch_ancestry_and_claims(self):
        strategy = self.claim()
        sha = self.local_commit()
        submitted = autoformalize_server.emit_formalization_candidate(strategy, "Ada", "existence", sha)
        candidate = submitted["candidate"]
        self.complete("sharpness")
        self.accept_main_change()
        for status in ("submitted", "merging"):
            with autoformalize_state.transaction(self.forum) as state:
                state["formal_candidates"][candidate["candidate_id"]]["status"] = status
            with self.subTest(status=status):
                sync = autoformalize_server.sync_from_main("Ada")
                prepared = autoformalize_server.prepare_formal_worktree("Ada", "sharpness", "next")
                self.assertEqual(sync["reason"], "candidate_pending")
                self.assertEqual(prepared["reason"], "candidate_pending")
                self.assertEqual(worktree.verify_candidate_commit(self.root, "Ada", sha), sha)
                current = autoformalize_state.load_state(self.forum)
                self.assertEqual(current["strategies"][strategy]["status"], "paused")
                self.assertEqual(current["formal_candidates"][candidate["candidate_id"]]["diff_sha256"], candidate["diff_sha256"])

    def test_other_authors_or_obsolete_candidates_do_not_block(self):
        with autoformalize_state.transaction(self.forum) as state:
            state["formal_candidates"] = {
                "old": {"author": "Ada", "formalization_revision": 0, "status": "merging"},
                "other": {"author": "Bert", "formalization_revision": 1, "status": "submitted",
                          "task_id": "existence", "task_revision": 1,
                          "solution_candidate": self.source["candidate_id"], "solution_sha256": self.source["sha256"]},
            }
        current = autoformalize_state.load_state(self.forum)
        self.assertFalse(autoformalize_server.has_pending_formal_candidate(current, "Ada"))
        self.assertTrue(autoformalize_server.has_pending_formal_candidate(current))
        self.assertTrue(autoformalize_server.sync_from_main("Ada")["ok"])

    def test_candidate_with_mismatched_source_binding_is_not_current_work(self):
        with autoformalize_state.transaction(self.forum) as state:
            state["formal_candidates"] = {
                "stale": {"author": "Ada", "formalization_revision": 1, "status": "merging",
                          "task_id": "existence", "solution_candidate": "old-paper", "solution_sha256": "b" * 64},
            }
        self.assertFalse(autoformalize_server.has_pending_formal_candidate(autoformalize_state.load_state(self.forum)))
        self.assertEqual(autoformalize_server.unresolved_formal_tasks(autoformalize_state.load_state(self.forum), "Ada"), [])
        with autoformalize_state.transaction(self.forum) as state:
            state["formal_candidates"]["stale"].update(solution_candidate=self.source["candidate_id"], solution_sha256="c" * 64)
        self.assertFalse(autoformalize_server.has_pending_formal_candidate(autoformalize_state.load_state(self.forum)))

    def test_task_dependency_rechecked_before_completed_assignment_reset(self):
        self.complete("sharpness")
        (self.tree / "Sharpness.lean").write_text("preserved until allocation succeeds\n")
        with autoformalize_state.transaction(self.forum) as state:
            state["formal_tasks"]["next"]["dependencies"] = ["existence"]
        result = autoformalize_server.prepare_formal_worktree("Ada", "sharpness", "next")
        self.assertEqual(result["reason"], "dependencies_pending")
        self.assertEqual((self.tree / "Sharpness.lean").read_text(), "preserved until allocation succeeds\n")

    def test_dirty_explicit_sync_preserves_bytes_and_claim(self):
        strategy = self.claim()
        (self.tree / "Existence.lean").write_text("valuable progress\n")
        self.accept_main_change()
        result = autoformalize_server.sync_from_main("Ada", "need accepted lemma")
        self.assertEqual(result["reason"], "dirty_worktree")
        self.assertEqual((self.tree / "Existence.lean").read_text(), "valuable progress\n")
        self.assertEqual(autoformalize_state.load_state(self.forum)["strategies"][strategy]["status"], "claimed")

    def test_explicit_sync_merges_main_and_keeps_unsubmitted_commit_and_claim(self):
        strategy = self.claim()
        local = self.local_commit()
        main = self.accept_main_change()
        result = autoformalize_server.sync_from_main("Ada")
        self.assertTrue(result["ok"])
        git(self.tree, "merge-base", "--is-ancestor", local, "HEAD")
        git(self.tree, "merge-base", "--is-ancestor", main, "HEAD")
        self.assertIn("trivial", (self.tree / "Existence.lean").read_text())
        self.assertIn("trivial", (self.tree / "Sharpness.lean").read_text())
        self.assertEqual(autoformalize_state.load_state(self.forum)["strategies"][strategy]["status"], "claimed")

    def test_explicit_sync_conflict_remains_in_worktree(self):
        self.claim()
        local = self.local_commit(content="local proof\n")
        main = self.accept_main_change("Existence.lean", "accepted different proof\n")
        result = autoformalize_server.sync_from_main("Ada")
        self.assertFalse(result["ok"])
        self.assertEqual(git(self.tree, "rev-parse", "HEAD"), local)
        self.assertEqual(git(self.tree, "rev-parse", "MERGE_HEAD"), main)
        self.assertIn("UU Existence.lean", git(self.tree, "status", "--porcelain"))
        conflicted = (self.tree / "Existence.lean").read_text()
        self.assertIn("local proof", conflicted)
        self.assertIn("accepted different proof", conflicted)
        self.assertEqual(autoformalize_server.sync_from_main("Ada")["reason"], "dirty_worktree")

    def test_nondestructive_sync_does_not_overwrite_ignored_local_files(self):
        (self.tree / "notes.scratch").write_text("valuable local notes\n")
        (self.root / "notes.scratch").write_text("accepted tracked notes\n")
        git(self.root, "add", "-f", "notes.scratch")
        git(self.root, "commit", "-qm", "accepted notes")
        with autoformalize_state.transaction(self.forum) as state:
            state["formalization"]["main_sha"] = git(self.root, "rev-parse", "HEAD")
        self.assertFalse(autoformalize_server.sync_from_main("Ada")["ok"])
        self.assertFalse(autoformalize_server.prepare_formal_worktree("Ada", next_task="existence")["ok"])
        self.assertEqual((self.tree / "notes.scratch").read_text(), "valuable local notes\n")

    def test_phase_revision_and_unaccepted_main_block_task_reset(self):
        self.complete("sharpness")
        (self.tree / "Sharpness.lean").write_text("preserve\n")
        self.assertEqual(autoformalize_server.prepare_formal_worktree(
            "Ada", "sharpness", "next", expected_revision=0,
        )["reason"], "phase_changed")
        with autoformalize_state.transaction(self.forum) as state:
            state["phase"] = "chunking"
        self.assertEqual(autoformalize_server.sync_from_main("Ada")["reason"], "phase_changed")
        self.assertEqual(autoformalize_server.prepare_formal_worktree("Ada", "sharpness", "next")["reason"], "phase_changed")
        with autoformalize_state.transaction(self.forum) as state:
            state["phase"] = "formalizing"
        self.accept_main_change()
        with autoformalize_state.transaction(self.forum) as state:
            state["formalization"]["main_sha"] = self.base
        self.assertEqual(autoformalize_server.prepare_formal_worktree("Ada", "sharpness", "next")["reason"], "main_changed")
        self.assertEqual((self.tree / "Sharpness.lean").read_text(), "preserve\n")

    def test_old_base_candidate_can_be_submitted_after_unrelated_main_merge(self):
        strategy = self.claim()
        sha = self.local_commit()
        main = self.accept_main_change()
        result = autoformalize_server.emit_formalization_candidate(strategy, "Ada", "existence", sha)
        candidate = result["candidate"]
        self.assertEqual(candidate["base_main_sha"], self.base)
        self.assertEqual(candidate["commit_sha"], sha)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), main)
        diff = subprocess.run([
            "git", "diff", "--no-ext-diff", "--no-textconv", "--binary", "--full-index", self.base, sha,
        ], cwd=self.root, capture_output=True, text=True, check=True).stdout
        self.assertEqual(candidate["diff_sha256"], hashlib.sha256(diff.encode()).hexdigest())
        apply = subprocess.run(["git", "apply", "--check", "--3way", "-"], cwd=self.root,
                               input=diff, capture_output=True, text=True)
        self.assertEqual(apply.returncode, 0, apply.stderr)

    def test_compatibility_submission_holds_author_lock(self):
        strategy = self.claim()
        sha = self.local_commit()
        original = autoformalize_server._submit_formal_commit

        def check_lock(*args, **kwargs):
            with (self.forum / "autoformalize-finalize-Ada.lock").open("a+") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return original(*args, **kwargs)

        with patch.object(autoformalize_server, "_submit_formal_commit", side_effect=check_lock):
            self.assertEqual(autoformalize_server.emit_formalization_candidate(
                strategy, "Ada", "existence", sha,
            )["status"], "submitted")

    def test_destructive_prepare_holds_state_merge_and_author_locks(self):
        self.complete("sharpness")
        original = worktree.force_sync_from_main

        def check_locks(*args, **kwargs):
            for name in ("autoformalize-state.lock", "merge.lock", "autoformalize-finalize-Ada.lock"):
                with (self.forum / name).open("a+") as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return original(*args, **kwargs)

        with patch.object(worktree, "force_sync_from_main", side_effect=check_locks):
            self.assertTrue(autoformalize_server.prepare_formal_worktree("Ada", "sharpness", "next")["ok"])


if __name__ == "__main__":
    unittest.main()
