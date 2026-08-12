import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from unity.config import Paths, active_forum_dir
from unity.forum import server as forum
from unity.forum import web
from unity.prove_runtime import RuntimeStore, discover_goals
from unity.prove_scheduler import ProveScheduler
from unity.roster import Agent, Roster
from unity import worktree


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True)
    return proc.stdout.strip()


def agent(name: str, strength: float) -> Agent:
    return Agent(name, f"model-{name}", "test", "codex", strength,
                 None, None, None, None, name == "A")


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        (self.root / "lakefile.toml").write_text('name = "fixture"\n')
        (self.root / "lean-toolchain").write_text("leanprover/lean4:v4.19.0\n")
        (self.root / "Fixture.lean").write_text(
            "theorem target (n : Nat) : n = n := by\n  sorry\n\n"
            "theorem untouched : True := by\n  trivial\n"
        )
        (self.root / ".unity" / "forum").mkdir(parents=True)
        (self.root / ".unity" / "logs").mkdir()
        (self.root / ".unity" / "agents.yaml").write_text("agents: []\n")
        (self.root / ".unity" / "UNITY.md").write_text("# Goal\n")
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.email", "unity@test.invalid")
        git(self.root, "config", "user.name", "Unity Test")
        git(self.root, "add", "lakefile.toml", "lean-toolchain", "Fixture.lean")
        git(self.root, "commit", "-m", "initial")
        self.goals = discover_goals(self.root)
        self.assertEqual(len(self.goals), 1)
        self.store = RuntimeStore.create(self.root / ".unity", self.root, self.goals,
                                         run_id="prove-test", review_quorum=1)
        self.fake_bin = Path(self.tmp.name) / "bin"
        self.fake_bin.mkdir()
        lake = self.fake_bin / "lake"
        lake.write_text("#!/bin/sh\nif [ -f FAIL_BUILD ]; then echo failure; exit 1; fi\necho build-ok\n")
        lake.chmod(0o755)
        self.path_env = str(self.fake_bin) + os.pathsep + os.environ.get("PATH", "")

    def tearDown(self):
        forum.FORUM_DIR = Path("forum")
        web.ROOT_DIR = Path(".")
        self.tmp.cleanup()

    def task_id(self):
        return next(iter(self.store.load()["tasks"]))

    def candidate_commit(self, proof="by\n  rfl", branch="candidate", extra=""):
        git(self.root, "switch", "-c", branch)
        (self.root / "Fixture.lean").write_text(
            f"theorem target (n : Nat) : n = n := {proof}\n{extra}\n"
            "theorem untouched : True := by\n  trivial\n"
        )
        git(self.root, "add", "Fixture.lean")
        git(self.root, "commit", "-m", branch)
        sha = git(self.root, "rev-parse", "HEAD")
        git(self.root, "switch", "main")
        return sha

    def submit_valid(self, author="A", branch="candidate", extra="",
                     parent_candidate=None, parent_objection=None):
        sha = self.candidate_commit(branch=branch, extra=extra)
        cand = self.store.submit_candidate(self.goals[0]["id"], author, sha,
                                           parent_candidate=parent_candidate,
                                           parent_objection=parent_objection)
        with patch.dict(os.environ, {"PATH": self.path_env}):
            record = self.store.verify_candidate(cand["id"], build_timeout=20)
        return cand, record


class RuntimeStateTests(ProjectCase):
    def test_atomic_claim_conflict_and_explicit_redundancy(self):
        task_id = self.task_id()
        barrier = threading.Barrier(3)
        results = []
        def claim(name):
            barrier.wait()
            results.append(self.store.claim_task(task_id, name))
        threads = [threading.Thread(target=claim, args=(name,)) for name in ("A", "B")]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(r["ok"] for r in results), 1)
        conflict = next(r for r in results if not r["ok"])
        self.assertEqual(conflict["reason"], "claim_conflict")
        redundant = self.store.claim_task(task_id, "C", independent=True)
        self.assertTrue(redundant["ok"])
        self.assertNotEqual(redundant["task"]["id"], task_id)
        telemetry = self.store.load()["telemetry"]
        self.assertGreaterEqual(telemetry["duplicate_claims_prevented"], 2)

    def test_stale_lease_can_be_reclaimed(self):
        task_id = self.task_id()
        self.assertTrue(self.store.claim_task(task_id, "A")["ok"])
        self.store._mutate(lambda s: s["tasks"][task_id].update(lease_expires_at=time.time() - 1))
        reclaimed = self.store.claim_task(task_id, "B")
        self.assertTrue(reclaimed["ok"])
        self.assertEqual(reclaimed["task"]["owner"], "B")
        self.assertIn("stale", [h["status"] for h in reclaimed["task"]["claim_history"]])

    def test_findings_are_shared_and_supersede_same_key(self):
        goal_id, task_id = self.goals[0]["id"], self.task_id()
        first = self.store.add_finding(goal_id, task_id, "A", "library_theorem",
                                       "Nat.eq_self", "May close the goal", "high", "#check")
        second = self.store.add_finding(goal_id, task_id, "B", "compiled_pattern",
                                        "Nat.eq_self", "rfl compiled", "verified", "build-ok")
        state = self.store.load()
        self.assertEqual(state["findings"][first["id"]]["status"], "superseded")
        self.assertEqual(state["findings"][second["id"]]["status"], "live")

    def test_large_artifacts_are_content_addressed_and_deduplicated(self):
        task_id = self.task_id()
        first = self.store.put_artifact(task_id, "A", "build-output", "line\n" * 5000)
        second = self.store.put_artifact(task_id, "B", "build-output", "line\n" * 5000)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["deduplicated"])
        fetched = self.store.get_artifact(first["id"], 100)
        self.assertTrue(fetched["clipped"])
        self.assertEqual(len(fetched["content"]), 100)

    def test_obstacles_and_questions_create_and_supersede_actionable_work(self):
        forum.FORUM_DIR = self.store.run_dir / "forum"
        goal_id = self.goals[0]["id"]
        obstacle = forum.forum_obstacle(goal_id, "A", "unknown equality lemma", ["simp"], "search API")
        self.assertTrue(obstacle["task"]["ok"])
        question = forum.forum_question("A", "Which equality lemma?", to="B", chunk=goal_id)
        task_id = question["task"]["task"]["id"]
        answer = forum.forum_answer(question["post_id"], "B", "Use rfl")
        self.assertEqual(answer["superseded_task"], task_id)
        self.assertEqual(self.store.load()["tasks"][task_id]["status"], "superseded")

    def test_mechanical_goal_dependencies_gate_initial_tasks_without_a_dag(self):
        (self.root / "Dependent.lean").write_text(
            "theorem first : True := by\n  sorry\n\n"
            "theorem second : True := by\n  have h := first\n  sorry\n"
        )
        git(self.root, "add", "Dependent.lean")
        git(self.root, "commit", "-m", "dependent targets")
        goals = discover_goals(self.root)
        by_name = {g["declaration"]: g for g in goals}
        self.assertIn(by_name["first"]["id"], by_name["second"]["dependencies"])
        self.assertFalse((self.root / ".unity" / "dag.json").exists())

    def test_one_goal_swarms_three_workers_without_prescribing_strategies(self):
        roster = Roster([agent("A", 3), agent("B", 2), agent("C", 1)])
        paths = Paths.from_unity_dir(self.root / ".unity")
        scheduler = ProveScheduler(roster, paths, self.store, "prompt")
        scheduler._start = AsyncMock(return_value=True)
        import asyncio
        asyncio.run(scheduler._allocate())
        self.assertEqual(scheduler._start.await_count, 3)
        assigned = [call.args[1] for call in scheduler._start.await_args_list]
        self.assertEqual(len({task["id"] for task in assigned}), 3)
        self.assertTrue(all(task["coordination_slot"] for task in assigned))
        self.assertTrue(all(task["strategy_key"].startswith("self-organized-attempt-")
                            for task in assigned))

    def test_agents_atomically_self_organize_distinct_task_plans(self):
        roster = Roster([agent("A", 3), agent("B", 2)])
        paths = Paths.from_unity_dir(self.root / ".unity")
        scheduler = ProveScheduler(roster, paths, self.store, "prompt")
        scheduler._ensure_swarm_work()
        task_ids = list(self.store.load()["tasks"])
        self.assertEqual(len(task_ids), 2)
        self.assertTrue(self.store.claim_task(task_ids[0], "A")["ok"])
        self.assertTrue(self.store.claim_task(task_ids[1], "B")["ok"])

        first = self.store.plan_task(task_ids[0], "A", "library_search",
                                     "search-specific-lemma", "Search the relevant namespace")
        collision = self.store.plan_task(task_ids[1], "B", "library_search",
                                         "search-specific-lemma", "Try the same search")
        alternate = self.store.plan_task(task_ids[1], "B", "decomposition",
                                         "derive-bridge-lemma", "Find a useful intermediate lemma")
        self.assertTrue(first["ok"])
        self.assertFalse(collision["ok"])
        self.assertEqual(collision["reason"], "strategy_conflict")
        self.assertEqual(collision["conflict"]["owner"], "A")
        self.assertTrue(alternate["ok"])
        state = self.store.load()
        self.assertFalse(state["tasks"][task_ids[0]]["coordination_slot"])
        self.assertFalse(state["tasks"][task_ids[1]]["coordination_slot"])

    def test_shared_state_visible_from_isolated_worktree(self):
        wt = worktree.create_worktree("visibility-test", self.root)
        try:
            worktree.link_shared_unity(wt, self.root / ".unity")
            linked = (wt / ".unity" / "current-run.json").resolve()
            self.assertEqual(json.loads(linked.read_text())["run_id"], "prove-test")
            paths = Paths.from_unity_dir((wt / ".unity").resolve())
            self.assertEqual(active_forum_dir(paths).resolve(), (self.store.run_dir / "forum").resolve())
        finally:
            worktree.cleanup_worktree("visibility-test", wt, self.root)


class CandidateTests(ProjectCase):
    def test_candidate_identity_is_immutable_after_later_edits(self):
        sha = self.candidate_commit(branch="immutable")
        cand = self.store.submit_candidate(self.goals[0]["id"], "A", sha)
        old_hash = cand["source_hash"]
        (self.root / "Fixture.lean").write_text("-- unrelated later edit\n" + (self.root / "Fixture.lean").read_text())
        stored = self.store.load()["candidates"][cand["id"]]
        self.assertEqual(stored["commit_sha"], sha)
        self.assertEqual(stored["source_hash"], old_hash)
        self.assertEqual(git(self.root, "rev-parse", stored["git_ref"]), sha)
        self.assertIn("theorem target", (self.store.run_dir / stored["artifact"]).read_text())

    def test_model_reported_build_ok_does_not_create_trusted_candidate(self):
        forum.FORUM_DIR = self.store.run_dir / "forum"
        forum.forum_result(self.goals[0]["id"], "A", "done", build_ok=True)
        state = self.store.load()
        self.assertEqual(state["candidates"], {})
        self.assertEqual(state["goals"][self.goals[0]["id"]]["status"], "open")

    def test_deterministic_verifier_passes_and_cancels_competing_work(self):
        competing = self.store.create_task(self.goals[0]["id"], "library_search",
                                           "Find an equality lemma", "B", strategy_key="eq-search")
        cand, record = self.submit_valid()
        self.assertTrue(record["passed"], record)
        state = self.store.load()
        self.assertEqual(state["candidates"][cand["id"]]["status"], "reviewable")
        self.assertEqual(state["tasks"][competing["task"]["id"]]["status"], "dominated")
        reviews = [t for t in state["tasks"].values() if t["kind"] == "review"]
        self.assertEqual(len(reviews), 1)

    def test_verifier_rejects_signature_change_and_build_failure(self):
        bad_sig_sha = self.candidate_commit(proof="by\n  rfl", branch="bad-sig")
        # Rewrite candidate commit with a weakened signature.
        git(self.root, "switch", "bad-sig")
        (self.root / "Fixture.lean").write_text("theorem target (n : Nat) : True := by\n  trivial\n")
        git(self.root, "add", "Fixture.lean")
        git(self.root, "commit", "-m", "weaken")
        bad_sig_sha = git(self.root, "rev-parse", "HEAD")
        git(self.root, "switch", "main")
        cand = self.store.submit_candidate(self.goals[0]["id"], "A", bad_sig_sha)
        with patch.dict(os.environ, {"PATH": self.path_env}):
            rec = self.store.verify_candidate(cand["id"], build_timeout=20)
        self.assertFalse(rec["passed"])
        self.assertFalse(rec["checks"]["signature_preserved"])

        sha = self.candidate_commit(branch="build-fail")
        git(self.root, "switch", "build-fail")
        (self.root / "FAIL_BUILD").write_text("x")
        git(self.root, "add", "FAIL_BUILD")
        git(self.root, "commit", "-m", "fail build")
        sha = git(self.root, "rev-parse", "HEAD")
        git(self.root, "switch", "main")
        cand2 = self.store.submit_candidate(self.goals[0]["id"], "B", sha)
        with patch.dict(os.environ, {"PATH": self.path_env}):
            rec2 = self.store.verify_candidate(cand2["id"], build_timeout=20)
        self.assertFalse(rec2["passed"])
        self.assertFalse(rec2["checks"]["lake_build"])

    def test_objection_blocks_and_creates_fix_then_corrected_candidate_accepts(self):
        cand, record = self.submit_valid(branch="revision-one")
        self.assertTrue(record["passed"])
        objection = self.store.object(cand["id"], "B", "Missing explanatory import evidence", "review")
        state = self.store.load()
        self.assertEqual(state["candidates"][cand["id"]]["status"], "blocked")
        self.assertEqual(state["tasks"][objection["task"]["id"]]["parent_objection"],
                         objection["objection"]["id"])

        corrected, record2 = self.submit_valid(author="A", branch="revision-two",
                                               extra="-- corrected revision",
                                               parent_candidate=cand["id"],
                                               parent_objection=objection["objection"]["id"])
        self.assertTrue(record2["passed"])
        self.assertEqual(self.store.load()["candidates"][cand["id"]]["status"], "superseded")
        endorsed = self.store.endorse(corrected["id"], "B", "checked exact diff")
        self.assertEqual(endorsed["status"], "acceptable")
        remaining = self.store.create_task(self.goals[0]["id"], "api_search", "extra search",
                                           "runtime", strategy_key="extra", redundant=True)
        accepted = self.store.accept_candidate(corrected["id"])
        self.assertTrue(accepted["ok"], accepted)
        state = self.store.load()
        self.assertEqual(state["goals"][self.goals[0]["id"]]["status"], "closed")
        self.assertEqual(state["candidates"][corrected["id"]]["status"], "accepted")
        self.assertEqual(state["tasks"][remaining["task"]["id"]]["status"], "cancelled")

    def test_author_cannot_self_endorse(self):
        cand, record = self.submit_valid()
        self.assertTrue(record["passed"])
        result = self.store.endorse(cand["id"], "A")
        self.assertFalse(result["ok"])

    def test_brief_prioritizes_candidate_and_is_bounded(self):
        cand, record = self.submit_valid()
        self.assertTrue(record["passed"])
        for i in range(80):
            self.store.add_finding(self.goals[0]["id"], self.task_id(), "A", "note",
                                   f"finding-{i}", "x" * 1000)
        forum.FORUM_DIR = self.store.run_dir / "forum"
        brief = forum.build_brief("B", self.goals[0]["id"])
        self.assertLessEqual(len(brief.encode()), 8_100)
        self.assertLess(brief.index(cand["id"]), brief.index("4. Your assigned task"))
        self.assertIn("machine_verified=True", brief)

    def test_exploration_style_worker_can_submit_final_candidate_directly(self):
        # No exploration/chunk phase state exists: a library-search task may still
        # discover, commit, and submit the final candidate for the exact goal.
        search = self.store.create_task(self.goals[0]["id"], "library_search",
                                        "Search then prove", "runtime", strategy_key="rfl-search")
        self.store.claim_task(search["task"]["id"], "A")
        cand, record = self.submit_valid(branch="search-found-proof")
        self.assertTrue(record["passed"])
        self.assertEqual(self.store.load()["goals"][self.goals[0]["id"]]["status"], "review")

    def test_web_prove_resume_uses_runtime_not_forum_activity(self):
        web.ROOT_DIR = self.root / ".unity"
        self.assertTrue(web._prove_resume_available())
        self.assertFalse(web._forum_nonempty())
        dry = web.api_run_start({"command": "prove", "dry": True})
        self.assertIn("--continue", dry["argv"])

        self.store.mark_status("complete")
        self.assertFalse(web._prove_resume_available())
        fresh = web.api_run_start({"command": "prove", "dry": True})
        self.assertNotIn("--continue", fresh["argv"])

    def test_forum_web_exposes_authoritative_coordination(self):
        web.ROOT_DIR = self.root / ".unity"
        task_id = self.task_id()
        self.assertTrue(self.store.claim_task(task_id, "A")["ok"])
        self.assertTrue(self.store.plan_task(
            task_id, "A", "proof_attempt", "theorem-specific-plan",
            "Plan chosen after reading the Forum brief",
        )["ok"])
        state = web.get_proof_search()
        task = next(t for t in state["tasks"] if t["id"] == task_id)
        self.assertEqual(task["owner"], "A")
        self.assertEqual(task["strategy_key"], "theorem-specific-plan")
        self.assertIn("authoritative prove coordination", web.FORUM_HTML)


class LegacyCompatibilityTests(unittest.TestCase):
    def test_legacy_forum_without_runtime_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            forum.FORUM_DIR = Path(tmp) / "forum"
            claim = forum.forum_claim("old-chunk", "A", "legacy")
            result = forum.forum_result("old-chunk", "A", "partial", build_ok=False)
            self.assertEqual(claim["act"], "claim")
            self.assertEqual(result["act"], "result")
            self.assertIn("results", forum.forum_consensus("old-chunk"))


if __name__ == "__main__":
    unittest.main()
