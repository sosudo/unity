"""Manifest repair Forum boundaries and preflight, without model or Lean calls."""

import asyncio
from contextlib import ExitStack, nullcontext
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from unity import autoformalize_state
from unity.forum import autoformalize_server as server
from unity.autoformalize_review import RepresentationRepairRequest
from test_autoformalize_manifest_repair import ManifestStateFixture


class ManifestServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.forum = self.root / ".unity" / "forum"
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(patch.object(server, "PROJECT_ROOT", self.root))
        self.stack.enter_context(patch.object(server, "FORUM_DIR", self.forum))
        self.stack.enter_context(patch.object(server, "PROFILE", "formalizing"))
        self.stack.enter_context(patch.dict(os.environ, {
            "UNITY_AGENT_NAME": "", "UNITY_AUTOFORMALIZE_TASK_ID": "",
            "UNITY_AUTOFORMALIZE_BRIEF_CHARS": "12000",
        }))
        self.stack.enter_context(patch.object(server, "_finalization_lock", side_effect=lambda _: nullcontext()))
        self.outputs = [{"declaration": "Example.goal", "file": "Example.lean"}]
        self.state = {
            "phase": "formalizing", "formalization": {"contract": {"version": 3}},
            "formal_tasks": {"main": {"task_id": "main", "status": "pending", "outputs": self.outputs}},
            "formal_candidates": {},
            "strategies": {"strategy": {"phase": "formalizing", "target": "main", "status": "claimed"}},
        }

    def test_repair_request_is_strict_and_schema_is_optional(self):
        request = {"task_id": "main", "kind": "output_manifest", "reason": "The existing witness is omitted."}
        self.assertEqual(RepresentationRepairRequest.model_validate(request).model_dump(), request)
        for invalid in ({**request, "task_id": 4}, {**request, "kind": "approved"},
                        {**request, "reason": ""}, {**request, "verified": True}):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                RepresentationRepairRequest.model_validate(invalid)
        tools = asyncio.run(server.build_server("critic").list_tools())
        schema = next(tool.parameters for tool in tools if tool.name == "submit_formalization_verdict")
        self.assertNotIn("representation_repairs", schema["required"])
        variants = schema["properties"]["representation_repairs"]["anyOf"]
        row = next(item for item in variants if item.get("type") == "array")["items"]
        self.assertEqual(set(row["required"]), {"task_id", "kind", "reason"})
        self.assertFalse(row["additionalProperties"])
        self.assertEqual(row["properties"]["kind"]["enum"], ["output_manifest", "representation"])

    def test_verdict_forwards_requests_separately_from_semantic_evidence(self):
        review = {"snapshot_id": "snapshot", "scope_rationale": "Inspect the accepted source.", "requirements": []}
        request = RepresentationRepairRequest(task_id="main", kind="representation", reason="Construct the missing witness.")
        with patch.object(autoformalize_state, "submit_critic_verdict", return_value={"status": "recorded"}) as submit, \
                patch.object(server, "_mirror"):
            result = server.submit_formalization_verdict(
                "Critic", "lean_reopen", "Missing witness", review, reopen_tasks=["main"],
                representation_repairs=[request],
            )
        self.assertEqual(result, {"status": "recorded"})
        self.assertEqual(submit.call_args.kwargs["representation_repairs"], [request.model_dump()])
        self.assertNotIn("representation_repairs", submit.call_args.kwargs["review"])

    def test_verdict_rejects_nonlist_repairs_before_forwarding(self):
        review = {"snapshot_id": "snapshot", "scope_rationale": "Review scope.", "requirements": []}
        with patch.object(autoformalize_state, "submit_critic_verdict") as submit:
            for invalid in ({}, (), ""):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "must be a list"):
                    server.submit_formalization_verdict("Critic", "lean_reopen", "Fix", review,
                                                       representation_repairs=invalid)
            submit.assert_not_called()

    def test_finalization_returns_preflight_block_before_any_git_or_worktree_access(self):
        for status in ("blocked", "conflict", "retry"):
            result = {"status": status, "reason": "Refresh the output manifest."}
            with self.subTest(status=status), \
                    patch.object(autoformalize_state, "preflight_formal_submission", return_value=result, create=True), \
                    patch.object(server, "_git") as git, \
                    patch.object(server.worktree, "agent_worktree") as tree, \
                    patch.object(server, "_submit_formal_commit") as submit:
                self.assertEqual(server.finalize_formalization("strategy", "Ada", "main", outputs=self.outputs), result)
                git.assert_not_called()
                tree.assert_not_called()
                submit.assert_not_called()

    def test_finalization_passes_exact_preflight_context_to_commit_submission(self):
        context = {"task_revision": 7, "outputs": deepcopy(self.outputs)}
        calls = []

        def preflight(*args, **kwargs):
            calls.append("preflight")
            return {"status": "ok", "context": context}

        def git(cwd, *args, **kwargs):
            calls.append(args[0])
            return SimpleNamespace(stdout="a" * 40 if args == ("rev-parse", "HEAD") else "", returncode=0)

        with patch.object(autoformalize_state, "preflight_formal_submission", side_effect=preflight, create=True), \
                patch.object(autoformalize_state, "load_state", return_value=self.state), \
                patch.object(autoformalize_state, "strategy_is_current", return_value=True), \
                patch.object(autoformalize_state, "participates", return_value=True), \
                patch.object(server.worktree, "agent_worktree", return_value=self.root), \
                patch.object(server, "_git", side_effect=git), \
                patch.object(server, "_submit_formal_commit", return_value={"status": "conflict"}) as submit:
            result = server.finalize_formalization("strategy", "Ada", "main", outputs=self.outputs)
        self.assertEqual(calls[0], "preflight")
        self.assertIs(submit.call_args.kwargs["submission_context"], context)
        self.assertEqual(result["status"], "conflict")
        self.assertFalse(result["committed"])

    def test_direct_commit_blocks_before_identity_reads_except_for_pending_identity_probe(self):
        with patch.object(autoformalize_state, "preflight_formal_submission", return_value={"status": "blocked"}, create=True), \
                patch.object(server.worktree, "verify_candidate_commit") as verify:
            self.assertEqual(server._submit_formal_commit("strategy", "Ada", "main", "a" * 40), {"status": "blocked"})
        verify.assert_not_called()

    def test_direct_commit_keeps_exact_pending_and_accepted_retries_idempotent(self):
        for outcome in ("submitted", "already_adopted"):
            existing = {"candidate_id": "candidate-existing", "task_id": "main"}
            expected = {"status": outcome, "candidate": existing, "idempotent": True}
            with self.subTest(outcome=outcome), \
                    patch.object(autoformalize_state, "preflight_formal_submission",
                                 return_value={"status": "conflict", "candidate": existing}, create=True), \
                    patch.object(autoformalize_state, "load_state", return_value=self.state), \
                    patch.object(server.worktree, "verify_candidate_commit", return_value="a" * 40), \
                    patch.object(server.worktree, "main_commit", return_value="b" * 40), \
                    patch.object(server, "_git", return_value=SimpleNamespace(stdout="", returncode=0)), \
                    patch.object(server.autoformalize_files, "immutable_git_paths", return_value={}), \
                    patch.object(server.autoformalize_contract, "policy_hash", return_value="policy"), \
                    patch.object(autoformalize_state, "submit_formal_candidate", return_value=expected) as submit, \
                    patch.object(server, "_mirror") as mirror:
                result = server._submit_formal_commit("strategy", "Ada", "main", "a" * 40)
            self.assertEqual(result, expected)
            self.assertIsNone(submit.call_args.kwargs["submission_context"])
            mirror.assert_not_called()

    def test_direct_commit_forwards_preflight_context_for_atomic_stale_check(self):
        context = {"task_revision": 3}
        with patch.object(autoformalize_state, "preflight_formal_submission", return_value={"status": "ok", "context": context}, create=True), \
                patch.object(autoformalize_state, "load_state", return_value=self.state), \
                patch.object(server.worktree, "verify_candidate_commit", return_value="a" * 40), \
                patch.object(server.worktree, "main_commit", return_value="b" * 40), \
                patch.object(server, "_git", return_value=SimpleNamespace(stdout="", returncode=0)), \
                patch.object(server.autoformalize_files, "immutable_git_paths", return_value={}), \
                patch.object(server.autoformalize_contract, "policy_hash", return_value="policy"), \
                patch.object(autoformalize_state, "submit_formal_candidate", return_value={"status": "retry"}) as submit:
            result = server._submit_formal_commit("strategy", "Ada", "main", "a" * 40)
        self.assertEqual(result, {"status": "retry"})
        self.assertIs(submit.call_args.kwargs["submission_context"], context)

    def test_metrics_use_persisted_repairs_not_bounded_event_history(self):
        state = {"run_id": "run", "events": [], "manifest_repairs": {
            "repair": {"repair_id": "repair", "task_id": "main", "kind": "output_manifest", "status": "cleared",
                       "created_at": 10, "resolved_at": 30,
                       "attempts": [{"author": "Ada", "status": "yielded", "started_at": 12, "finished_at": 14}]},
            "waiting": {"repair_id": "waiting", "task_id": "other", "kind": "representation", "status": "open",
                        "created_at": 31, "attempts": []},
            "exhausted": {"repair_id": "exhausted", "task_id": "last", "kind": "output_manifest",
                          "status": "exhausted", "created_at": 32, "exhausted_at": 45, "attempts": []},
        }}
        with patch.object(autoformalize_state, "load_state", return_value=state), \
                patch.object(autoformalize_state, "current_manifest_repairs", return_value=[state["manifest_repairs"]["waiting"]]):
            metrics = server.read_metrics(self.forum, self.root)["manifest_repairs"]
        self.assertEqual(metrics["total"], 3)
        self.assertEqual(metrics["by_status"], {"cleared": 1, "open": 1, "exhausted": 1})
        self.assertEqual(metrics["attempts_by_status"], {"yielded": 1})
        self.assertEqual(metrics["records"][0]["created_at"], 10)
        self.assertEqual(metrics["records"][0]["resolved_at"], 30)
        self.assertEqual(metrics["records"][0]["attempts"][0]["finished_at"], 14)
        self.assertEqual(metrics["records"][2]["exhausted_at"], 45)
        self.assertEqual([row["is_current"] for row in metrics["records"]], [False, True, False])
        self.assertEqual([row["diagnostic_latency_seconds"] for row in metrics["records"]], [20, None, 13])

    def test_task_and_brief_show_only_current_related_repairs(self):
        state = deepcopy(self.state)
        state["strategies"] = {}
        state["formalization"] = {"contract": {"version": 2}, "requirements": []}
        rows = [{"repair_id": "repair-main", "task_id": "main", "kind": "output_manifest",
                 "status": "open", "attempts": [], "blockers": [{"code": "output_not_found"}]},
                {"repair_id": "repair-other", "task_id": "other", "kind": "representation",
                 "status": "exhausted", "attempts": []}]
        with patch.object(autoformalize_state, "load_state", return_value=state), \
                patch.object(autoformalize_state, "current_manifest_repairs", return_value=rows, create=True), \
                patch.object(server, "_detail", side_effect=lambda value, _: value):
            detail = server.autoformalize_task("main")
            brief = server.autoformalize_brief("Ada", "main")
        self.assertEqual(detail["manifest_repairs"], rows[:1])
        self.assertIn("repair-main", brief)
        self.assertIn("not acceptance evidence", brief)
        self.assertNotIn("repair-other", brief)


class ManifestServerRealGitTests(ManifestStateFixture):
    """Real source-bound preflight and Git bytes; no readiness/acceptance mocks."""

    def setUp(self):
        super().setUp()
        self.root = self.forum.parents[1]
        self.git(self.root, "init", "-b", "main")
        self.git(self.root, "config", "user.name", "Unity Test")
        self.git(self.root, "config", "user.email", "unity@example.test")
        (self.root / ".gitignore").write_text(".unity/\n.lake/\n*.scratch\n")
        (self.root / "Example").mkdir()
        (self.root / "Example/alpha.lean").write_text("theorem Example.alpha : True := by sorry\n")
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "-qm", "source-bound fixture")
        with autoformalize_state.transaction(self.forum) as current:
            current["formalization"]["main_sha"] = self.git(self.root, "rev-parse", "HEAD").strip()
        self.enterContext(patch.object(server, "PROJECT_ROOT", self.root))
        self.enterContext(patch.object(server, "FORUM_DIR", self.forum))
        self.enterContext(patch.object(server, "PROFILE", "formalizing"))
        self.enterContext(patch.dict(os.environ, {"UNITY_AGENT_NAME": ""}))
        self.tree = server.worktree.create_worktree("Ada", self.root)

    def git(self, root, *args):
        return subprocess.run(["git", *args], cwd=root, text=True,
                              capture_output=True, check=True).stdout

    def test_real_manifest_preflight_preserves_head_index_dirty_and_ignored_bytes(self):
        path = self.tree / "Example/alpha.lean"
        path.write_text("staged private proof\n")
        self.git(self.tree, "add", "Example/alpha.lean")
        path.write_text("newer unstaged private proof\n")
        (self.tree / "notes.scratch").write_text("ignored proof notes\n")
        (self.tree / "new.txt").write_text("untracked research\n")
        before_git = {name: self.git(self.tree, *args) for name, args in {
            "head": ("rev-parse", "HEAD"), "index": ("diff", "--cached", "--binary"),
            "status": ("status", "--porcelain"), "dirty": ("diff", "--binary"),
        }.items()}
        before_state = self.current()
        with patch.object(server, "_git") as git_calls, \
                patch.object(server, "_submit_formal_commit") as submit:
            result = server.finalize_formalization(
                self.strategy, "Ada", "alpha", outputs=self.changed,
            )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual([row["code"] for row in result["blockers"]], ["output_manifest_changed"])
        git_calls.assert_not_called()
        submit.assert_not_called()
        for name, args in {"head": ("rev-parse", "HEAD"), "index": ("diff", "--cached", "--binary"),
                           "status": ("status", "--porcelain"), "dirty": ("diff", "--binary")}.items():
            self.assertEqual(self.git(self.tree, *args), before_git[name])
        self.assertEqual(path.read_text(), "newer unstaged private proof\n")
        self.assertEqual((self.tree / "notes.scratch").read_text(), "ignored proof notes\n")
        self.assertEqual((self.tree / "new.txt").read_text(), "untracked research\n")
        self.assert_only_diagnostics_changed(before_state)

    def test_main_change_after_private_commit_returns_retry_without_candidate(self):
        (self.tree / "Example/alpha.lean").write_text("theorem Example.alpha : True := by trivial\n")
        before_candidates = self.current()["formal_candidates"]
        original = server._submit_formal_commit

        def move_main_then_submit(*args, **kwargs):
            (self.root / "unrelated.txt").write_text("accepted unrelated progress\n")
            self.git(self.root, "add", "unrelated.txt")
            self.git(self.root, "commit", "-qm", "advance accepted main")
            with autoformalize_state.transaction(self.forum) as current:
                current["formalization"]["main_sha"] = self.git(self.root, "rev-parse", "HEAD").strip()
            return original(*args, **kwargs)

        with patch.object(server, "_submit_formal_commit", side_effect=move_main_then_submit):
            result = server.finalize_formalization(self.strategy, "Ada", "alpha", outputs=self.outputs)
        self.assertEqual(result["status"], "retry")
        self.assertTrue(result["committed"])
        self.assertEqual(self.current()["formal_candidates"], before_candidates)
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD").strip(), result["commit_sha"])
        self.assertIn("by trivial", self.git(self.tree, "show", "HEAD:Example/alpha.lean"))


if __name__ == "__main__":
    unittest.main()
