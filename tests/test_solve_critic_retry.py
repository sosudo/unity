"""Exercise critic retries through the real solve loop and shared-state gate."""

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asyncclick.testing import CliRunner

from unity import solve_state
from unity.commands import solve as solve_command
from unity.config import Paths
from solve_port_fixtures import initialize_graph, machine_snapshot, semantic_evidence, verification_receipt


class SolveCriticRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = Paths.from_unity_dir(Path(self.temp.name) / ".unity")
        self.paths.forum.mkdir(parents=True)
        self.paths.unity_md.write_text("Prove True faithfully.\n")
        self.ada = SimpleNamespace(name="Ada", strength=2, is_primary=True)
        self.bert = SimpleNamespace(name="Bert", strength=1, is_primary=False)
        self.roster = SimpleNamespace(primary=self.ada, agents=[self.ada, self.bert])
        state = solve_state.initialize(
            self.paths.forum,
            hashlib.sha256(self.paths.unity_md.read_bytes()).hexdigest(),
            "a" * 40,
        )
        self.run_id = state["run_id"]
        paper = solve_state.submit_solution_candidate(
            self.paths.forum, "Ada", "artifact-paper", "b" * 64, "PROOF.tex"
        )["candidate"]
        solve_state.review_solution_candidate(
            self.paths.forum, paper["candidate_id"], "Bert", "approve", "Complete proof."
        )
        solve_state.accept_solution_candidate(self.paths.forum, paper["candidate_id"], "Unity")
        source_components = [f"paper:{paper['candidate_id']}"]
        self.contract_sha = "9" * 64
        self.source_sha = "6" * 64
        initialize_graph(
            self.paths.forum,
            [{"id": "main", "lean_decl": "Example.main", "dependencies": [],
              "source_components": source_components}],
            solution_candidate=paper["candidate_id"],
            solution_sha256=paper["sha256"],
            main_sha="a" * 40,
            requirements=[{"id": "main-result", "statement": "Prove True faithfully.",
                           "source_components": source_components, "tasks": ["main"]}],
            contract={"sha256": self.contract_sha, "targets": {"Example.main": {}},
                      "environment": {}},
        )
        self.contract_sha = solve_state.load_state(self.paths.forum)["formalization"]["contract"]["sha256"]
        self.merge_formal()
        solve_state.record_review_snapshot(
            self.paths.forum,
            self.machine_report(self.paths, solve_state.load_state(self.paths.forum)),
        )
        solve_state.begin_critic(self.paths.forum)
        self.calls = []
        # Mock only filesystem/build inspection; controller orchestration and all
        # snapshot, semantic-review, and final-acceptance state checks stay real.
        for name, effect in {
            "snapshot_is_current": self.snapshot_is_current,
            "verify_final_project": self.machine_report,
        }.items():
            mocked = patch.object(solve_command.solve_contract, name, side_effect=effect)
            setattr(self, name + "_mock", mocked.start())
            self.addCleanup(mocked.stop)

    def machine_report(self, paths, state):
        self.assertEqual(paths, self.paths)
        formal = state["formalization"]
        report = {
            "passed": True,
            "main_sha": formal["main_sha"],
            "source_sha256": self.source_sha,
            "environment": {},
            "solution_candidate": formal["solution_candidate"],
            "solution_sha256": formal["solution_sha256"],
            "formalization_revision": formal["revision"],
            "contract_sha256": formal["contract"]["sha256"],
            "accepted_candidates": {
                task_id: task["accepted_candidate"]
                for task_id, task in state["formal_tasks"].items()
            },
            "declarations": {
                task["lean_decl"]: task_id for task_id, task in state["formal_tasks"].items()
            },
            "issues": [],
        }
        report = machine_snapshot(state, report)
        report.pop("snapshot_id", None)
        report["snapshot_id"] = hashlib.sha256(
            json.dumps(report, sort_keys=True).encode()
        ).hexdigest()
        return report

    def snapshot_is_current(self, paths, state, snapshot, **_kwargs):
        return snapshot == self.machine_report(paths, state)

    def merge_formal(self):
        strategy = solve_state.register_strategy(
            self.paths.forum, "Ada", "Exact proof", target="main", family="exact"
        )["strategy"]
        solve_state.claim_strategy(self.paths.forum, strategy["strategy_id"], "Ada")
        formal = solve_state.submit_formal_candidate(
            self.paths.forum, strategy["strategy_id"], "Ada", "main",
            "c" * 40, "a" * 40, "d" * 64,
        )["candidate"]
        solve_state.begin_formal_merge(self.paths.forum, formal["candidate_id"])
        solve_state.finish_formal_merge(
            self.paths.forum, formal["candidate_id"], success=True, main_sha="e" * 40,
            verification=verification_receipt(solve_state.load_state(self.paths.forum)),
        )

    def submit(self, verdict, author):
        kwargs = {"reopen_tasks": ["main"]} if verdict == "lean_reopen" else {}
        state = solve_state.load_state(self.paths.forum)
        evidence = semantic_evidence(state)
        if verdict != "approved":
            for row in evidence["requirements"]:
                row["status"] = "fail"
        solve_state.submit_critic_verdict(
            self.paths.forum, author, verdict, "Checked the exact accepted solution.",
            review=evidence,
            **kwargs,
        )
        if verdict == "approved":
            pending = solve_state.load_state(self.paths.forum)
            self.assertEqual(pending["phase"], "critic")
            self.assertEqual(pending["formalization"]["status"], "approval_pending")

    async def invoke(self, outcomes, *, limit="7", formalizing=None, solving=None, review=None):
        outcomes = list(outcomes)

        async def dispatch(agents, roster, prompt, task, *_args, **kwargs):
            self.assertEqual(len(agents), 1)
            self.assertIn(agents[0], self.roster.agents)
            self.assertIs(roster, self.roster)
            self.assertEqual(kwargs["tools_prompt"], "SOLVE_CRITIC_TOOLS")
            self.assertEqual(kwargs["mcp_profile"], "solve")
            self.assertFalse(kwargs["icrl_enabled"])
            self.assertLess(len(self.calls), len(outcomes), "unexpected extra critic dispatch")
            self.calls.append({"agent": agents[0].name, "task": task, **kwargs})
            outcome = outcomes[len(self.calls) - 1]
            if outcome in {"approved", "lean_reopen", "reopen_solving"}:
                self.submit(outcome, agents[0].name)
            elif outcome == "stale_approved":
                self.submit("approved", agents[0].name)
                self.source_sha = "7" * 64
            elif outcome == "source_fix":
                solve_state.submit_solution_candidate(
                    self.paths.forum, agents[0].name, "artifact-correction", "f" * 64,
                    "PROOF.tex", replace_accepted=True, notes="Local source correction.",
                )
            elif outcome == "stop":
                (self.paths.unity / "stop-requested").touch()
            elif outcome == "returned_error":
                return [RuntimeError("agent backend ended without a verdict")]
            else:
                self.assertEqual(outcome, "missing")
            return [None]

        with ExitStack() as stack:
            for name, value in {
                "load_paths": self.paths,
                "load_roster": self.roster,
                "resume_point": "critic",
                "build_solve_formal_mcp": {},
                "load_prompt": "Critic system prompt",
            }.items():
                stack.enter_context(patch.object(solve_command, name, return_value=value))
            stack.enter_context(patch.object(solve_command, "_prepare_solve_environment"))
            stack.enter_context(patch.object(solve_command.worktree, "main_commit", return_value="e" * 40))
            stack.enter_context(patch.object(solve_command, "formal_dispatch", side_effect=dispatch))
            stack.enter_context(patch.object(solve_command, "require_source_matches"))
            stack.enter_context(patch.object(solve_command, "persist_report"))
            mark_done = stack.enter_context(patch.object(solve_command, "mark_done"))
            retrospective = stack.enter_context(patch.object(solve_command, "_run_retrospective"))
            formal_mock = stack.enter_context(patch.object(
                solve_command, "run_formalizing_runtime", new=AsyncMock(side_effect=formalizing)
            ))
            solving_mock = stack.enter_context(patch.object(
                solve_command, "run_solving_runtime", new=AsyncMock(side_effect=solving)
            ))
            stack.enter_context(patch.object(
                solve_command, "_review_current_solution", new=AsyncMock(side_effect=review)
            ))
            stack.enter_context(patch.dict(os.environ, {"MAX_ATTEMPTS": limit, "RETROSPECTIVE": "false"}))
            result = await CliRunner().invoke(solve_command.command, ["--continue"])
        retrospective.assert_not_called()
        return result, mark_done, formal_mock, solving_mock

    def assert_attempts(self, count, *, agents=None, attempts=None):
        self.assertEqual(len(self.calls), count)
        self.assertEqual([call["agent"] for call in self.calls], agents or ["Ada"] * count)
        self.assertEqual(
            [call["log_context"]["attempt"] for call in self.calls],
            attempts or list(range(1, count + 1)),
        )
        for call in self.calls:
            self.assertEqual(call["log_context"]["run_id"], self.run_id)
            self.assertEqual(call["log_context"]["phase"], "critic")

    async def test_missing_verdict_retries_then_approval_completes(self):
        result, done, formal, solving = await self.invoke(["missing", "approved"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assert_attempts(2)
        self.assertIn("review remains incomplete", result.output)
        self.assertIn("critic attempt 2", self.calls[1]["task"])
        self.assertIn("submit_formalization_verdict", self.calls[1]["task"])
        state = solve_state.load_state(self.paths.forum)
        self.assertEqual(state["phase"], "complete")
        self.assertEqual(state["formalization"]["status"], "accepted")
        self.assertEqual(len(state["critic_verdicts"]), 1)
        self.assertEqual(
            state["formalization"]["accepted_verdict_id"],
            state["critic_verdicts"][0]["verdict_id"],
        )
        self.verify_final_project_mock.assert_not_called()
        done.assert_called_once_with(self.paths, "solve")
        formal.assert_not_awaited()
        solving.assert_not_awaited()

    async def test_primary_exhaustion_rotates_to_next_agent_with_a_fresh_budget(self):
        # The primary need not be first in the configured roster.
        self.roster.agents = [self.bert, self.ada]
        result, done, _, _ = await self.invoke(["missing"] * 7 + ["approved"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assert_attempts(8, agents=["Ada"] * 7 + ["Bert"], attempts=list(range(1, 8)) + [1])
        state = solve_state.load_state(self.paths.forum)
        self.assertEqual(state["critic_verdicts"][0]["author"], "Bert")
        done.assert_called_once_with(self.paths, "solve")

    async def test_every_agent_gets_seven_attempts_before_exhaustion(self):
        result, done, _, _ = await self.invoke(["missing"] * 14)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assert_attempts(
            14, agents=["Ada"] * 7 + ["Bert"] * 7, attempts=list(range(1, 8)) * 2,
        )
        self.assertIn("every configured agent exhausted its critic attempts", result.output)
        state = solve_state.load_state(self.paths.forum)
        self.assertEqual(state["phase"], "critic")
        self.assertEqual(state["formalization"]["status"], "review")
        self.assertEqual(state["critic_verdicts"], [])
        done.assert_not_called()

    async def test_one_attempt_limit_means_one_per_agent_in_roster_order(self):
        cy = SimpleNamespace(name="Cy", strength=3, is_primary=False)
        self.roster.agents = [self.bert, self.ada, cy]
        result, done, _, _ = await self.invoke(["missing"] * 3, limit="1")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assert_attempts(3, agents=["Ada", "Bert", "Cy"], attempts=[1, 1, 1])
        self.assertIn("every configured agent exhausted its critic attempts", result.output)
        done.assert_not_called()

    async def test_blank_attempt_limit_retries_until_structured_approval(self):
        result, done, _, _ = await self.invoke(["missing"] * 8 + ["approved"], limit="")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assert_attempts(9)
        done.assert_called_once_with(self.paths, "solve")

    async def test_approval_on_last_allowed_attempt_is_not_exhaustion(self):
        result, done, _, _ = await self.invoke(["missing"] * 3 + ["approved"], limit="2")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assert_attempts(4, agents=["Ada", "Ada", "Bert", "Bert"], attempts=[1, 2, 1, 2])
        done.assert_called_once_with(self.paths, "solve")

    async def test_first_approval_has_no_extra_dispatch(self):
        result, done, _, _ = await self.invoke(["approved"], limit="1")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assert_attempts(1)
        done.assert_called_once_with(self.paths, "solve")

    async def test_stale_approval_requires_a_new_snapshot_and_semantic_review(self):
        result, done, _, _ = await self.invoke(["stale_approved", "approved"], limit="2")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assert_attempts(2)
        self.assertIn("critic approval became stale", result.output)
        self.verify_final_project_mock.assert_called_once()
        state = solve_state.load_state(self.paths.forum)
        first, second = state["critic_verdicts"]
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(state["formalization"]["accepted_verdict_id"], second["verdict_id"])
        self.assertEqual(
            state["formalization"]["review_snapshot"]["snapshot_id"], second["snapshot_id"]
        )
        self.assertEqual(state["phase"], "complete")
        done.assert_called_once_with(self.paths, "solve")

    async def test_resumed_pending_approval_is_finalized_without_another_dispatch(self):
        self.submit("approved", "Ada")
        result, done, _, _ = await self.invoke([], limit="1")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assert_attempts(0)
        state = solve_state.load_state(self.paths.forum)
        self.assertEqual(state["phase"], "complete")
        self.assertEqual(len(state["critic_verdicts"]), 1)
        self.verify_final_project_mock.assert_not_called()
        done.assert_called_once_with(self.paths, "solve")

    async def test_returned_agent_exception_without_verdict_counts_as_an_attempt(self):
        result, done, _, _ = await self.invoke(["returned_error", "approved"], limit="2")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assert_attempts(2)
        done.assert_called_once_with(self.paths, "solve")

    async def test_stop_after_missing_verdict_exits_safely_without_retry(self):
        result, done, _, _ = await self.invoke(["stop"], limit="1")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assert_attempts(1)
        self.assertIn("solve stopped safely", result.output)
        self.assertEqual(solve_state.load_state(self.paths.forum)["phase"], "critic")
        done.assert_not_called()

    async def test_lean_reopen_returns_to_formalizing_without_another_critic_dispatch(self):
        async def formalizing(_roster, paths, *_args):
            state = solve_state.load_state(paths.forum)
            self.assertEqual(state["phase"], "formalizing")
            self.assertEqual(state["formal_tasks"]["main"]["status"], "pending")
            (paths.unity / "stop-requested").touch()
            return state

        result, done, formal, solving = await self.invoke(
            ["missing", "lean_reopen"], limit="1", formalizing=formalizing
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assert_attempts(2, agents=["Ada", "Bert"], attempts=[1, 1])
        formal.assert_awaited_once()
        solving.assert_not_awaited()
        done.assert_not_called()

    async def test_reopen_solving_returns_to_solving_without_another_critic_dispatch(self):
        async def solving(_roster, paths, *_args):
            state = solve_state.load_state(paths.forum)
            self.assertEqual(state["phase"], "solving")
            self.assertEqual(state["solution"]["status"], "open")
            (paths.unity / "stop-requested").touch()
            return state

        result, done, formal, solving_mock = await self.invoke(
            ["missing", "reopen_solving"], limit="1", solving=solving
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assert_attempts(2, agents=["Ada", "Bert"], attempts=[1, 1])
        solving_mock.assert_awaited_once()
        formal.assert_not_awaited()
        done.assert_not_called()

    async def test_critic_cancellation_propagates_without_accepting_gate(self):
        with patch.object(solve_command, "load_prompt", return_value="Critic"), \
             patch.object(solve_command, "build_solve_formal_mcp", return_value={}), \
             patch.object(solve_command, "formal_dispatch", side_effect=asyncio.CancelledError) as dispatch:
            with self.assertRaises(asyncio.CancelledError):
                await solve_command._run_critics(self.roster, self.paths, 7)
        dispatch.assert_awaited_once()
        state = solve_state.load_state(self.paths.forum)
        self.assertEqual(state["phase"], "critic")
        self.assertEqual(state["critic_verdicts"], [])

    async def test_stop_before_dispatch_does_not_start_any_critic(self):
        (self.paths.unity / "stop-requested").touch()
        with patch.object(solve_command, "formal_dispatch", new=AsyncMock()) as dispatch:
            await solve_command._run_critics(self.roster, self.paths, 7)
        dispatch.assert_not_awaited()
        self.assertEqual(solve_state.load_state(self.paths.forum)["phase"], "critic")

    async def test_source_correction_exits_rotation_for_solution_review(self):
        async def review(_roster, paths, _max_attempts):
            state = solve_state.load_state(paths.forum)
            self.assertEqual(state["phase"], "solution_review")
            self.assertEqual(state["solution"]["status"], "review")
            (paths.unity / "stop-requested").touch()

        result, done, formal, solving = await self.invoke(["source_fix"], limit="1", review=review)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assert_attempts(1)
        self.assertEqual(solve_state.load_state(self.paths.forum)["critic_verdicts"], [])
        formal.assert_not_awaited()
        solving.assert_not_awaited()
        done.assert_not_called()

    async def test_repaired_project_starts_a_new_primary_first_critic_budget(self):
        async def formalizing(_roster, paths, *_args):
            self.assertEqual(solve_state.load_state(paths.forum)["phase"], "formalizing")
            self.merge_formal()
            return solve_state.load_state(paths.forum)

        result, done, formal, solving = await self.invoke(
            ["missing", "lean_reopen", "missing", "approved"],
            limit="1", formalizing=formalizing,
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assert_attempts(4, agents=["Ada", "Bert", "Ada", "Bert"], attempts=[1] * 4)
        state = solve_state.load_state(self.paths.forum)
        self.assertEqual([item["verdict"] for item in state["critic_verdicts"]], ["lean_reopen", "approved"])
        self.assertNotEqual(
            state["critic_verdicts"][0]["snapshot_id"],
            state["critic_verdicts"][1]["snapshot_id"],
        )
        self.verify_final_project_mock.assert_called_once()
        formal.assert_awaited_once()
        solving.assert_not_awaited()
        done.assert_called_once_with(self.paths, "solve")

    async def test_empty_round_stops_without_budget_or_unchanged_critic_on_resume(self):
        self.submit("lean_reopen", "Ada")

        async def formalizing(_roster, paths, *_args):
            return solve_state.record_round_end(
                paths.forum, blocked_launches={"Ada/main": "encoding repair is exhausted"},
                activity={},
            )

        # An unchanged --continue is bounded too; it does not reset a repair's
        # budget or loop through five identical snapshots on either invocation.
        for _ in range(2):
            with patch.object(solve_command, "_prepare_critic_snapshot") as critic, \
                 patch.object(solve_command, "_save_incomplete_report") as incomplete:
                result, done, formal, _ = await self.invoke([], limit="1", formalizing=formalizing)
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("formalization blocked", result.output)
            self.assertIn("no proof attempt was charged", result.output)
            self.assertNotIn("exhausted MAX_ATTEMPTS", result.output)
            critic.assert_not_called()
            incomplete.assert_called_once_with(self.paths)
            formal.assert_awaited_once()
            done.assert_not_called()
            state = solve_state.load_state(self.paths.forum)
            self.assertEqual(state["phase"], "formalizing")
            self.assertEqual(state["formalization"]["last_round"]["outcome"], "blocked")
            self.assertEqual(len(state["critic_verdicts"]), 1)

    async def test_complete_zero_dispatch_round_still_gets_final_review(self):
        with solve_state.transaction(self.paths.forum) as state:
            state["phase"] = "formalizing"

        async def formalizing(_roster, paths, *_args):
            result = solve_state.record_round_end(paths.forum, blocked_launches={}, activity={})
            self.assertEqual(result["formalization"]["last_round"]["outcome"], "complete")
            self.assertFalse(solve_command._formal_round_attempted(result, None))
            return result

        result, done, formal, _ = await self.invoke(["approved"], limit="1", formalizing=formalizing)
        self.assertEqual(result.exit_code, 0, result.output)
        formal.assert_awaited_once()
        done.assert_called_once_with(self.paths, "solve")

    async def test_open_source_issue_cannot_hide_an_empty_blocked_round(self):
        self.submit("lean_reopen", "Ada")
        with solve_state.transaction(self.paths.forum) as state:
            source = solve_state.formal_source(state)
            state["source_issues"]["source-block"] = {
                "issue_id": "source-block", "status": "open", "task_ids": ["main"],
                "source_candidate": source["candidate_id"], "source_sha256": source["sha256"],
                "attempts": [], "repair_ids": [], "owner": None,
            }

        async def formalizing(_roster, paths, *_args):
            return solve_state.record_round_end(paths.forum, blocked_launches={}, activity={})

        with patch.object(solve_command, "_prepare_critic_snapshot") as critic:
            result, done, formal, _ = await self.invoke([], limit="1", formalizing=formalizing)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("formalization blocked", result.output)
        formal.assert_awaited_once()
        critic.assert_not_called()
        done.assert_not_called()

    async def test_round_accounting_records_actual_activity_not_task_completion(self):
        self.submit("lean_reopen", "Ada")
        for counter in ("worker_launches", "integrations", "review_launches", "source_repairs"):
            with self.subTest(counter=counter):
                state = solve_state.record_round_end(
                    self.paths.forum, blocked_launches={}, activity={counter: 1},
                )
                summary = state["formalization"]["last_round"]
                self.assertEqual(summary["outcome"], "attempted")
                self.assertTrue(solve_command._formal_round_attempted(state, None))
                self.assertFalse(solve_command._formal_round_attempted(state, summary["round_id"]))
                self.assertEqual(summary["pending_tasks"][0]["task_id"], "main")


if __name__ == "__main__":
    unittest.main()
