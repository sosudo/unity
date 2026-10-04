"""Task-specific critic guidance delivery; offline, without models or Lean builds."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unity import artifacts, bump_runtime as runtime
from unity.forum import bump_server as server


def feedback_entry(**changes):
    entry = {
        "requirement_id": "R-alpha", "task_ids": ["alpha"],
        "rationale": "The statement is correct but the source witness is absent.",
        "argument_rationale": "The proof calls the final theorem instead of counting the source objects.",
        "repair_steps": ["Define the finite chain type.", "Prove its incidence count."],
        "verdict_id": "verdict-previous", "snapshot_id": "snapshot-previous",
        "reviewed_main_sha": "a" * 40, "latest_review_status": "fail", "historical": True,
        "declarations": ["Alpha.count"], "checked_anchor_ids": ["source-counting"],
        "checked_prerequisite_ids": ["finite-chains"],
    }
    entry.update(changes)
    return entry


def submit_alpha_rejection(forum):
    """Use the real snapshot/critic state API; machine evidence is an offline fixture."""
    from test_bump_manifest_repair import machine_snapshot, semantic_evidence

    state = runtime.bump_state
    current = state.load_state(forum)
    state.record_review_snapshot(forum, machine_snapshot(current))
    state.begin_critic(forum)
    review = semantic_evidence(state.load_state(forum))
    requirement = next(row for row in current["formalization"]["requirements"] if "alpha" in row["tasks"])
    row = next(row for row in review["requirements"] if row["requirement_id"] == requirement["id"])
    row.update(status="fail", rationale="The statement is correct but the source witness is absent.",
               argument_rationale="The proof calls the final theorem instead of counting the source objects.",
               repair_steps=["Define the finite chain type.", "Prove its incidence count."])
    return state.submit_critic_verdict(
        forum, "Critic", "lean_reopen", "Implement the source counting argument.",
        review=review, reopen_tasks=["alpha"],
    )["verdict"]


class CriticRepairPromptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="critic-feedback-runtime-")
        self.addCleanup(temporary.cleanup)
        self.paths = SimpleNamespace(artifacts=Path(temporary.name) / "artifacts")
        self.state = {"run_id": "offline-run"}

    def test_no_relevant_feedback_adds_no_prompt_or_artifact(self):
        with patch.object(runtime.bump_state, "critic_feedback_for_task",
                          return_value={"direct": [], "upstream": []}) as lookup:
            self.assertEqual(runtime._critic_repair_prompt(self.paths, self.state, "alpha"), "")
        lookup.assert_called_once_with(self.state, "alpha")
        self.assertFalse(self.paths.artifacts.exists())

    def test_full_direct_feedback_and_provenance_are_not_brief_truncated(self):
        row = feedback_entry(rationale="x" * 500 + " FULL-RATIONALE-END",
                             argument_rationale="y" * 500 + " FULL-ARGUMENT-END")
        feedback = {"direct": [row], "upstream": []}
        before = deepcopy(feedback)
        prompt = runtime._critic_repair_prompt(self.paths, self.state, "alpha", feedback=feedback)
        for text in (row["rationale"], row["argument_rationale"], *row["repair_steps"],
                     row["verdict_id"], row["snapshot_id"], row["reviewed_main_sha"],
                     *row["declarations"], *row["checked_anchor_ids"], *row["checked_prerequisite_ids"],
                     "DIRECT FAILED REQUIREMENTS", "historical review evidence, not acceptance",
                     "do not wait for a new critic approval before submitting"):
            self.assertIn(text, prompt)
        self.assertNotIn("UPSTREAM FEEDBACK", prompt)
        self.assertEqual(feedback, before)

    def test_upstream_only_feedback_is_guidance_not_assignment_or_redo(self):
        prompt = runtime._critic_repair_prompt(
            self.paths, self.state, "beta", feedback={"direct": [], "upstream": [feedback_entry()]},
        )
        self.assertIn("UPSTREAM FEEDBACK — NOT A NEW ASSIGNMENT", prompt)
        self.assertIn("Do not redo your already-correct result", prompt)
        self.assertNotIn("DIRECT FAILED REQUIREMENTS", prompt)
        self.assertIn('"task_ids": [\n        "alpha"', prompt)

    def test_oversized_feedback_has_lossless_read_all_artifact(self):
        row = feedback_entry(rationale="long rationale α " * 2000,
                             argument_rationale="long argument β " * 2000,
                             repair_steps=["FIRST STEP", "FINAL CHECKLIST STEP"])
        feedback = {"direct": [row], "upstream": [feedback_entry(requirement_id="R-upstream")]}
        with patch.dict(os.environ, {"UNITY_ARTIFACT_THRESHOLD_BYTES": "1024",
                                     "UNITY_ARTIFACT_PREVIEW_BYTES": "256"}):
            prompt = runtime._critic_repair_prompt(self.paths, self.state, "alpha", feedback=feedback)
        self.assertIn("read the FULL feedback artifact", prompt)
        self.assertIn("starting at offset 0", prompt)
        self.assertIn("next_offset until null", prompt)
        self.assertIn("DIRECT FAILED REQUIREMENTS", prompt)
        self.assertIn("UPSTREAM FEEDBACK", prompt)
        artifact_id = re.search(r"artifact-[0-9a-f]{12}", prompt).group()
        payload = artifacts.artifact_bytes(self.paths.artifacts, artifact_id)
        self.assertEqual(json.loads(payload), {"task_id": "alpha", **feedback})
        info = artifacts.artifact_info(self.paths.artifacts, artifact_id)
        self.assertEqual(info["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(info["metadata"], {"run_id": "offline-run", "task_id": "alpha",
                                            "direct_count": 1, "upstream_count": 1})
        # The payload uses ASCII JSON escapes so arbitrary byte-page boundaries
        # preserve the exact Unicode evidence after agent-visible reconstruction.
        self.assertTrue(payload.isascii())
        offset, reconstructed = 0, []
        while True:
            page = artifacts.read_artifact(self.paths.artifacts, artifact_id, offset=offset, limit=701)
            reconstructed.append(page["content"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertEqual("".join(reconstructed).encode(), payload)
        self.assertEqual(json.loads("".join(reconstructed)), {"task_id": "alpha", **feedback})

    def test_direct_feedback_and_mechanical_failure_override_nudges_not_context(self):
        repair = {"repair_id": "manifest-1", "task_id": "alpha", "blockers": []}
        prompt = runtime._compose_formal_task_prompt(
            repair=repair, recovery="MECHANICAL BLOCKERS", critic_repair="SEMANTIC CHECKLIST",
            direct_critic_repair=True, resume="RESUME OLD PROOF", followup="FINALIZE IMMEDIATELY",
            normal="CURRENT TASK", representation="ADOPTED REPRESENTATION",
            work_context="PRESERVED STRATEGY / DIRTY FILES / SYNC WARNING",
        )
        self.assertLess(prompt.index("MANIFEST REPAIR"), prompt.index("MECHANICAL BLOCKERS"))
        self.assertLess(prompt.index("MECHANICAL BLOCKERS"), prompt.index("SEMANTIC CHECKLIST"))
        for text in ("CURRENT TASK", "ADOPTED REPRESENTATION", "PRESERVED STRATEGY", "SYNC WARNING"):
            self.assertIn(text, prompt)
        for text in ("RESUME OLD PROOF", "FINALIZE IMMEDIATELY"):
            self.assertNotIn(text, prompt)

    def test_semantic_failure_alone_suppresses_opportunistic_nudges(self):
        prompt = runtime._compose_formal_task_prompt(
            recovery="", critic_repair="SEMANTIC CHECKLIST", direct_critic_repair=True,
            resume="RESUME OLD PROOF", followup="FINALIZE IMMEDIATELY", normal="CURRENT TASK",
            representation="REPRESENTATION", work_context="PRIVATE WORK",
        )
        self.assertIn("CURRENT TASK", prompt)
        self.assertIn("SEMANTIC CHECKLIST", prompt)
        self.assertIn("PRIVATE WORK", prompt)
        self.assertNotIn("RESUME OLD PROOF", prompt)
        self.assertNotIn("FINALIZE IMMEDIATELY", prompt)

    def test_upstream_only_feedback_preserves_normal_nudge_precedence(self):
        prompt = runtime._compose_formal_task_prompt(
            recovery="", critic_repair="UPSTREAM ONLY", resume="RESUME", followup="SUBMISSION CHECK",
            normal="NORMAL", representation="REPRESENTATION",
        )
        for text in ("UPSTREAM ONLY", "RESUME", "SUBMISSION CHECK", "REPRESENTATION"):
            self.assertIn(text, prompt)
        self.assertNotIn("NORMAL", prompt)

    def test_unaffected_prompt_remains_identical(self):
        prompt = runtime._compose_formal_task_prompt(
            recovery="", resume="RESUME", followup="", normal="NORMAL", representation="REPRESENTATION",
        )
        self.assertEqual(prompt, "RESUME\nNORMAL\nREPRESENTATION")


class CriticFeedbackToolTests(unittest.TestCase):
    def test_task_payload_exposes_direct_upstream_and_unaffected_history_without_mutation(self):
        from test_bump_manifest_repair import ManifestStateFixture

        fixture = ManifestStateFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.accept("alpha")
        fixture.accept("beta")
        verdict = submit_alpha_rejection(fixture.forum)
        state = runtime.bump_state
        paths = SimpleNamespace(forum=fixture.forum, project_root=fixture.forum.parent.parent)
        runtime.configure_forum(paths, "formalizing")
        before = state.state_path(fixture.forum).read_bytes()
        # Keep the normal task tool result inline here; overflow is covered separately.
        with patch.dict(os.environ, {"UNITY_ARTIFACT_THRESHOLD_BYTES": "1000000"}):
            payloads = {task: json.loads(server.bump_task(task)) for task in ("alpha", "beta", "other")}
        for task, payload in payloads.items():
            self.assertEqual(payload["critic_feedback"], state.critic_feedback_for_task(fixture.current(), task))
        self.assertEqual(len(payloads["alpha"]["critic_feedback"]["direct"]), 1)
        self.assertEqual(payloads["alpha"]["critic_feedback"]["direct"][0]["verdict_id"], verdict["verdict_id"])
        self.assertFalse(payloads["beta"]["critic_feedback"]["direct"])
        self.assertEqual(len(payloads["beta"]["critic_feedback"]["upstream"]), 1)
        self.assertEqual(payloads["other"]["critic_feedback"], {"direct": [], "upstream": []})
        self.assertEqual(state.state_path(fixture.forum).read_bytes(), before)


class CriticFeedbackDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_bump_manifest_runtime import ManifestRuntimePersistedIntegrationTests

        self.fixture = ManifestRuntimePersistedIntegrationTests()
        # Register resource cleanup on the actively running async test, not the
        # embedded fixture whose IsolatedAsyncioTestCase runner was never started.
        self.fixture.addCleanup = self.addCleanup
        self.fixture.setUp()
        self.paths = self.fixture.paths
        self.accept("alpha")
        self.accept("beta")
        self.verdict = submit_alpha_rejection(self.paths.forum)

    def accept(self, task_id):
        """Fixture the compiler result; keep actual candidates, reviews and ownership."""
        ss = runtime.bump_state
        current = self.fixture.current()
        strategy = ss.register_strategy(self.paths.forum, "Author", "Prove " + task_id, target=task_id)["strategy"]
        ss.claim_strategy(self.paths.forum, strategy["strategy_id"], "Author")
        candidate = ss.submit_formal_candidate(
            self.paths.forum, strategy["strategy_id"], "Author", task_id, self.fixture.base,
            self.fixture.base, hashlib.sha256(b"").hexdigest(), stage="complete",
            outputs=[{"declaration": task_id, "file": task_id.title() + ".lean"}],
        )["candidate"]
        ss.begin_formal_merge(self.paths.forum, candidate["candidate_id"])
        proposed = deepcopy(current["formalization"]["contract"])
        proposed["bindings"][task_id] = candidate["outputs"]
        proposed["targets"][task_id] = {"fingerprint": ss.digest(task_id + " : True"),
                                       "meaning_dependencies": [task_id]}
        proposed["sha256"] = ss._contract_digest(proposed)
        ss.finish_formal_merge(
            self.paths.forum, candidate["candidate_id"], success=True, main_sha=self.fixture.base,
            proposed_contract=proposed, verification={"status": "passed", "contract_sha256": proposed["sha256"],
                "policy_sha256": runtime.bump_contract.policy_hash(),
                "verified_targets": {task_id: ss.digest(task_id + " : True")}},
        )
        runtime.bump_representation.claim_representation_review(self.paths.forum, task_id, "Reviewer")
        self.fixture.submit_review("Reviewer", task_id, "aligned")

    async def test_next_real_dispatch_contains_direct_feedback_and_upstream_only_guidance(self):
        calls = []

        async def provider(agent, system, prompt, *_args, **kwargs):
            context = kwargs["log_context"]
            calls.append((context["role"], context["task_id"], prompt))

        result = await self.fixture.run_persisted(provider)
        self.assertEqual({task for _, task, _ in calls}, {"alpha", "beta"})
        for role, task_id, prompt in calls:
            self.assertEqual(role, "bumpr")
            self.assertIn(self.verdict["verdict_id"], prompt)
            self.assertIn(self.verdict["snapshot_id"], prompt)
            self.assertIn(self.verdict["main_sha"], prompt)
            self.assertIn("Define the finite chain type.", prompt)
            self.assertIn("Prove its incidence count.", prompt)
            if task_id == "alpha":
                self.assertIn("DIRECT FAILED REQUIREMENTS", prompt)
                self.assertNotIn("UPSTREAM FEEDBACK", prompt)
            else:
                self.assertEqual(task_id, "beta")
                self.assertIn("UPSTREAM FEEDBACK — NOT A NEW ASSIGNMENT", prompt)
                self.assertNotIn("DIRECT FAILED REQUIREMENTS", prompt)

        self.assertEqual(result["phase"], "formalizing")
        self.assertNotEqual(result["formalization"]["status"], "accepted")
        self.assertEqual(self.fixture.source_file.read_bytes(), self.fixture.source_bytes)
        self.assertEqual(self.fixture.git("rev-parse", "HEAD"), self.fixture.base)

    async def test_direct_dispatch_preserves_claim_dirty_work_and_sync_warning(self):
        ss = runtime.bump_state
        real_prepare = runtime.bump_server.prepare_formal_worktree
        captures = []

        def prepare(author, **kwargs):
            result = real_prepare(author, **kwargs)
            if result.get("ok") and kwargs["next_task"] == "alpha":
                strategy = ss.register_strategy(self.paths.forum, author, "Repair the chain witness", target="alpha")["strategy"]
                ss.claim_strategy(self.paths.forum, strategy["strategy_id"], author)
                tree = runtime._formal_worktree(self.paths.project_root, author)
                (tree / "Unfinished.lean").write_text("-- preserved unfinished proof work\n")
                result["sync_warning"] = "SYNC WARNING: preserve the private branch conflict."
            return result

        async def provider(agent, system, prompt, *_args, **kwargs):
            if kwargs["log_context"]["task_id"] == "alpha":
                captures.append(prompt)

        with patch.object(runtime.bump_server, "prepare_formal_worktree", side_effect=prepare):
            await self.fixture.run_persisted(provider)
        self.assertTrue(captures)
        for prompt in captures:
            self.assertIn("Your currently claimed strategy is", prompt)
            self.assertIn("uncommitted or untracked files", prompt)
            self.assertIn("SYNC WARNING: preserve the private branch conflict.", prompt)
            self.assertNotIn("call `finalize_formalization` immediately", prompt)
            self.assertNotIn("Resume your currently claimed strategy", prompt)
            self.assertIn("DIRECT FAILED REQUIREMENTS", prompt)
        self.assertEqual(self.fixture.source_file.read_bytes(), self.fixture.source_bytes)
        self.assertEqual(self.fixture.git("rev-parse", "HEAD"), self.fixture.base)

    async def test_direct_feedback_preserves_other_authors_checked_prerequisite_repair(self):
        ss = runtime.bump_state
        strategy = ss.register_strategy(self.paths.forum, "DifferentAuthor", "Earlier attempt", target="alpha")["strategy"]
        ss.claim_strategy(self.paths.forum, strategy["strategy_id"], "DifferentAuthor")
        candidate = ss.submit_formal_candidate(
            self.paths.forum, strategy["strategy_id"], "DifferentAuthor", "alpha", self.fixture.base,
            self.fixture.base, hashlib.sha256(b"different attempt").hexdigest(), stage="complete",
            outputs=[{"declaration": "alpha", "file": "Alpha.lean"}],
        )["candidate"]
        ss.begin_formal_merge(self.paths.forum, candidate["candidate_id"])
        ss.finish_formal_merge(self.paths.forum, candidate["candidate_id"], success=False,
            error="Prerequisite evidence is missing.", blockers=[{
                "code": "prerequisite_unresolved", "prerequisite_id": "finite-chains", "task_ids": ["alpha"],
                "message": "Provide the exact source prerequisite witness.",
                "required_action": "Resolve finite-chains with its source-grounded evidence.",
            }])
        self.assertEqual(runtime._rejection_recovery_prompt(self.fixture.current(), "Ada", "alpha"), "")
        self.assertEqual(len(server.verification_blockers(self.fixture.current(), "alpha")), 1)
        captures = []

        async def provider(agent, system, prompt, *_args, **kwargs):
            if kwargs["log_context"]["task_id"] == "alpha":
                captures.append(prompt)

        await self.fixture.run_persisted(provider)
        self.assertTrue(captures)
        for prompt in captures:
            self.assertIn("DIRECT FAILED REQUIREMENTS", prompt)
            self.assertIn("SOURCE EVIDENCE REPAIR", prompt)
            self.assertIn("Resolve finite-chains with its source-grounded evidence.", prompt)
            self.assertNotIn("Submission check only", prompt)
            self.assertLess(prompt.index("SOURCE EVIDENCE REPAIR"), prompt.index("DIRECT FAILED REQUIREMENTS"))

    async def test_direct_feedback_preserves_real_replacement_lineage_handoff(self):
        ss = runtime.bump_state
        runtime.configure_forum(self.paths, "formalizing")
        runtime._formal_worktree(self.paths.project_root, "Ada")
        prepared = server.prepare_formal_worktree(
            "Ada", next_task="alpha", expected_revision=self.fixture.current()["formalization"]["revision"],
        )
        self.assertTrue(prepared["ok"])
        current = self.fixture.current()
        old_task = current["formal_tasks"]["alpha"]
        node_fields = {"title", "predicted_kind", "informal_statement", "informal_proof", "statement_dependencies",
                       "proof_dependencies", "source_components", "anchor_ids", "requirement_ids",
                       "proposed_formal_statement", "proposed_formal_strategy"}
        replacement = {key: deepcopy(value) for key, value in old_task.items() if key in node_fields}
        replacement["id"] = "replacement"
        ss.refine_chunks(self.paths.forum, "Ada", current["revision"], {
            "upserts": [replacement],
            "replacements": [{"old_ids": ["alpha"], "new_ids": ["replacement"], "reason": "Replace counting node."}],
        })
        self.assertTrue(ss.critic_feedback_for_task(self.fixture.current(), "replacement")["direct"])
        captures = []

        async def provider(agent, system, prompt, *_args, **kwargs):
            captures.append((agent.name, kwargs["log_context"]["task_id"], prompt))

        await self.fixture.run_persisted(provider)
        assigned = [prompt for author, task, prompt in captures if author == "Ada" and task == "replacement"]
        self.assertTrue(assigned)
        for prompt in assigned:
            self.assertIn("DIRECT FAILED REQUIREMENTS", prompt)
            self.assertIn("Your prior informal node was replaced", prompt)
            self.assertIn("successor and its lineage", prompt)
            self.assertIn("Your worktree was preserved; reuse relevant work", prompt)
            self.assertIn("Your current formalization target is task `replacement`", prompt)
        self.assertEqual(self.fixture.source_file.read_bytes(), self.fixture.source_bytes)
        self.assertEqual(self.fixture.git("rev-parse", "HEAD"), self.fixture.base)


if __name__ == "__main__":
    unittest.main()
