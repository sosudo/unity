"""Real Lean/Git lifecycle fixture; scripted critic, zero real model calls."""
from copy import deepcopy
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from tests import test_bump_project as project_fixture
from unity import bump_bootstrap as bootstrap, bump_contract as contract, bump_state as state
from unity import bump_runtime as runtime, bump_report, artifacts
from unity.commands import bump as command
from unity.config import Paths
from unity.forum import bump_server


class ParallelLifecycleNativeTests(unittest.TestCase):
    def test_independent_queued_merge_retirement_handoff_and_clean_critic_reopen(self):
        if not shutil.which("elan"):
            self.skipTest("Requires existing native toolchains; never downloads")
        available = subprocess.run(["elan", "toolchain", "list"], capture_output=True, text=True, timeout=20)
        if not {"leanprover/lean4:v4.28.0-rc1", "leanprover/lean4:v4.34.1"}.issubset(
                {row.split()[0] for row in available.stdout.splitlines() if row.strip()}):
            self.skipTest("Requires exact existing Lean4.28.0-rc1 and4.34.1")
        fixture = project_fixture.ProjectTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.write("lean-toolchain", "leanprover/lean4:v4.28.0-rc1\n")
        for name in ("A", "B", "C"):
            fixture.write(f"Fixture/{name}.lean", f"theorem fixture{name} : True := by trivial\n")
        fixture.write("Fixture.lean", "import Fixture.A\nimport Fixture.B\nimport Fixture.C\n")
        fixture.commit()
        paths = Paths.from_unity_dir(fixture.root / ".unity")
        paths.unity.mkdir()
        paths.unity_md.write_text("# Goal\nPreserve this tiny native fixture.\n")
        paths.agents_yaml.write_text("agents: []\n")
        paths.env.write_text("MAX_ATTEMPTS=5\nRETROSPECTIVE=false\n")
        original = bootstrap.bump_migration_project.snapshot(fixture.root)
        with patch.dict(os.environ, {"MAX_ATTEMPTS": "5"}):
            target = bootstrap.prepare(paths, "v4.34.1", {}, architect="off")
        initial_contract = deepcopy(state.load_state(target.forum)["formalization"]["contract"])

        def introduce_errors(names):
            for name in names:
                fixture.write(f"Fixture/{name}.lean", f"theorem fixture{name} : True := missingFixtureProof\n",
                              root=target.project_root)
            fixture.git("add", "Fixture", root=target.project_root)
            fixture.git("commit", "-qm", "test-only target failures", root=target.project_root)
            with state.transaction(target.forum) as mutable:
                mutable["formalization"]["main_sha"] = fixture.git("rev-parse", "HEAD", root=target.project_root)
                mutable["migration_refresh_required"] = True
            return bootstrap.check_ready_modules(target)

        old_server = (bump_server.FORUM_DIR, bump_server.PROJECT_ROOT, bump_server.PROFILE)
        old_discussion = (bump_server.discussion.FORUM_DIR, bump_server.discussion.PROJECT_ROOT,
                          bump_server.discussion.ICRL_ENABLED)
        for key, value in zip(("FORUM_DIR", "PROJECT_ROOT", "PROFILE"), old_server):
            self.addCleanup(setattr, bump_server, key, value)
        for key, value in zip(("FORUM_DIR", "PROJECT_ROOT", "ICRL_ENABLED"), old_discussion):
            self.addCleanup(setattr, bump_server.discussion, key, value)
        bump_server.configure(target.forum, target.project_root, "formalizing")

        def submit(worker, name, *, previous=""):
            key = "Fixture." + name
            tree = runtime._formal_worktree(target.project_root, worker)
            current = state.load_state(target.forum)
            handoff = bump_server.prepare_formal_worktree(worker, previous_task=previous, next_task=key,
                expected_revision=current["formalization"]["revision"])
            self.assertTrue(handoff["ok"], handoff)
            current = state.load_state(target.forum)
            chunk = state.next_migration_chunk(current, key)
            task = state.begin_migration_attempt(target.forum, key, worker,
                **({"subtask_id": chunk["id"]} if chunk else {}))
            if chunk:
                assignment = bump_server.record_migration_focus_assignment(worker, key, task["revision"])
                self.assertEqual(assignment["migration_subtask_id"], chunk["id"])
            strategy = state.register_strategy(target.forum, worker, "Preserve fixture theorem", target=key)["strategy"]
            state.claim_strategy(target.forum, strategy["strategy_id"], worker)
            fixture.write(f"Fixture/{name}.lean", f"theorem fixture{name} : True := by exact True.intro\n", root=tree)
            with patch.dict(os.environ, {"UNITY_AGENT_NAME": worker}):
                submitted = bump_server.finalize_formalization(strategy["strategy_id"], worker, key,
                    changed_paths=[f"Fixture/{name}.lean"], outputs=task["outputs"])
            self.assertEqual(submitted["status"], "submitted", submitted)
            return submitted["candidate"]

        def merge(candidate):
            started = state.begin_formal_merge(target.forum, candidate["candidate_id"])["candidate"]
            current = state.load_state(target.forum)
            result = runtime._integrate_and_record(target, started, current["formal_tasks"][candidate["task_id"]])
            self.assertTrue(result.get("ok"), result)
            return bootstrap.check_ready_modules(target)

        current = introduce_errors(["A", "B"])
        candidate_a = submit("ParallelWorkerA", "A")
        candidate_b = submit("ParallelWorkerB", "B")
        before_state = state.load_state(target.forum)
        before = before_state["formal_tasks"]["Fixture.B"]
        current = merge(candidate_a)
        after = current["formal_tasks"]["Fixture.B"]
        before_context = before_state["migration_plan"]["group_contexts"]["Fixture.B"]
        after_context = current["migration_plan"]["group_contexts"]["Fixture.B"]
        changed_context = {key: [before_context.get(key), after_context.get(key)]
            for key in set(before_context) | set(after_context) if before_context.get(key) != after_context.get(key)}
        if after["revision"] != before["revision"]:
            changed_context["before_build_log"] = artifacts.artifact_bytes(target.artifacts,
                before_state["migration_plan"]["diagnostic_artifact"]["artifact_id"]).decode()[:5000]
        self.assertEqual(after["revision"], before["revision"], changed_context)
        self.assertEqual(after["task_input_sha256"], before["task_input_sha256"])
        self.assertEqual(after["status"], "candidate_pending")
        self.assertTrue(state.candidate_is_current(current, current["formal_candidates"][candidate_b["candidate_id"]]))
        current = merge(candidate_b)
        self.assertTrue(state.all_formal_tasks_complete(current))
        self.assertEqual(current["formalization"]["contract"], initial_contract)

        # A's source is now retired from error routing, not lost as handoff work.
        current = introduce_errors(["C"])
        candidate_c = submit("ParallelWorkerA", "C", previous="Fixture.A")
        current = merge(candidate_c)
        self.assertTrue(state.all_formal_tasks_complete(current))

        def enter_review():
            current = state.load_state(target.forum)
            report = contract.verify_final_project(target, current)
            self.assertTrue(report["passed"], report.get("issues"))
            state.record_review_snapshot(target.forum, report)
            state.begin_critic(target.forum)
            current = state.load_state(target.forum)
            review = {"snapshot_id": report["snapshot_id"], "scope_rationale": "Scripted native lifecycle fixture.",
                "repair_reviews": [], "requirements": [{
                    "requirement_id": item["id"], "status": "pass", "checked_anchor_ids": item["anchor_ids"],
                    "checked_prerequisite_ids": [], "declarations": [key for owner in item["tasks"]
                        for key in contract.output_fingerprints(current["formalization"]["contract"], owner)],
                    "rationale": "Native original obligations checked.",
                    "argument_rationale": "Fixture only; no real semantic critic."}
                    for item in current["formalization"]["requirements"]]}
            return current, review

        current, review = enter_review()
        row = next(row for row in review["requirements"] if row["requirement_id"] == "preserve:Fixture.A")
        row.update(status="fail", repair_steps=["Explain and recheck the preserved fixture theorem."])
        state.submit_critic_verdict(target.forum, "ScriptedCritic", "lean_reopen", "Scripted repair request.",
                                   review=review, reopen_tasks=["Fixture.A"])
        current = state.load_state(target.forum)
        feedback = state.critic_feedback_for_task(current, "Fixture.A")
        self.assertTrue(feedback["direct"], feedback)
        self.assertIn("Explain and recheck", feedback["direct"][0]["repair_steps"][0])
        current = merge(submit("ParallelWorkerB", "A", previous="Fixture.B"))
        current, review = enter_review()
        state.submit_critic_verdict(target.forum, "FreshScriptedCritic", "approved", "Scripted final review.", review=review)
        self.assertTrue(command._accept_current_critic(target))
        completed = state.load_state(target.forum)
        self.assertEqual(bump_report.completion_report(completed)["status"], "accepted")
        self.assertEqual(bootstrap.bump_migration_project.snapshot(fixture.root), original)
        self.assertFalse(bump_report.completion_report(completed)["semantic_equivalence_proved"])
