"""Real command/state accounting and critic gates; no providers or Lean builds."""

from contextlib import ExitStack
from dataclasses import replace
import hashlib
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import asyncclick as click
from asyncclick.testing import CliRunner

from unity import autoformalize_state as state
from unity.commands import autoformalize as command
from unity.config import Paths
from test_autoformalize_manifest_repair import ManifestStateFixture, machine_snapshot, semantic_evidence


class AutoformalizeCriticRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = ManifestStateFixture("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.forum = self.fixture.forum
        base = Paths.from_unity_dir(self.forum.parent / "cli")
        self.paths = replace(base, forum=self.forum)
        self.paths.unity.mkdir(parents=True, exist_ok=True)
        self.paths.unity_md.write_text("Formalize the supplied source faithfully.\n")
        self.fixture.accept("alpha")
        self.fixture.accept("beta")
        with state.transaction(self.forum) as current:
            current["problem_sha256"] = hashlib.sha256(self.paths.unity_md.read_bytes()).hexdigest()
        self.calls = []
        self.ada = SimpleNamespace(name="Ada", is_primary=True)
        self.bert = SimpleNamespace(name="Bert", is_primary=False)
        self.roster = SimpleNamespace(primary=self.ada, agents=[self.ada, self.bert])
        state.record_review_snapshot(self.forum, self.report())
        state.begin_critic(self.forum)

    def current(self):
        return state.load_state(self.forum)

    def report(self, *_args):
        return machine_snapshot(self.current())

    def submit(self, verdict, author="Ada"):
        evidence = semantic_evidence(self.current())
        if verdict != "approved":
            for row in evidence["requirements"]:
                row["status"] = "fail"
        return state.submit_critic_verdict(
            self.forum, author, verdict, "Checked the current supplied-source requirements.",
            review=evidence,
            **({"reopen_tasks": ["alpha"]} if verdict == "lean_reopen" else {}),
        )

    async def invoke(self, outcomes=(), *, limit="2", formalizing=None):
        outcomes = list(outcomes)

        async def dispatch(agents, _roster, *_args, **kwargs):
            self.assertEqual(len(agents), 1)
            self.assertEqual(kwargs["tools_prompt"], "AUTOFORMALIZE_CRITIC_TOOLS")
            self.assertEqual(kwargs["mcp_profile"], "autoformalize")
            self.assertLess(len(self.calls), len(outcomes), "unexpected model dispatch")
            outcome = outcomes[len(self.calls)]
            self.calls.append((agents[0].name, kwargs["log_context"]["attempt"]))
            if outcome in {"approved", "lean_reopen"}:
                self.submit(outcome, agents[0].name)
            else:
                self.assertEqual(outcome, "missing")
            return [None]

        with ExitStack() as stack:
            for name, value in {
                "load_paths": self.paths, "autoformalize_paths": self.paths,
                "load_roster": self.roster, "resume_point": "critic",
                "build_autoformalize_mcp": {}, "load_prompt": "critic prompt",
            }.items():
                stack.enter_context(patch.object(command, name, return_value=value))
            # Only external/filesystem preparation is substituted. Real state,
            # command accounting, verdict validation and acceptance still run.
            for name in ("_prepare_autoformalize_environment", "recover_interrupted_formal_merges",
                         "require_source_matches", "persist_report"):
                stack.enter_context(patch.object(command, name))
            stack.enter_context(patch.object(command.autoformalize_jobs, "terminate"))
            stack.enter_context(patch.object(command.worktree, "main_commit",
                                              return_value=self.current()["formalization"]["main_sha"]))
            stack.enter_context(patch.object(command.autoformalize_contract, "snapshot_is_current", return_value=True))
            stack.enter_context(patch.object(command.autoformalize_contract, "verify_final_project", side_effect=self.report))
            stack.enter_context(patch.object(command, "dispatch", side_effect=dispatch))
            formal = stack.enter_context(patch.object(command, "run_formalizing_runtime",
                                                       new=AsyncMock(side_effect=formalizing)))
            done = stack.enter_context(patch.object(command, "mark_done"))
            retrospective = stack.enter_context(patch.object(command, "_run_retrospective"))
            stack.enter_context(patch.dict(os.environ, {"MAX_ATTEMPTS": limit, "RETROSPECTIVE": "false"}))
            result = await CliRunner().invoke(command.command, ["--continue"])
        retrospective.assert_not_called()
        return result, done, formal

    async def test_missing_verdict_retries_then_approval_completes(self):
        result, done, formal = await self.invoke(["missing", "approved"])
        self.assertEqual(result.exit_code, 0, result.output + repr(result.exception))
        self.assertEqual(self.calls, [("Ada", 1), ("Ada", 2)])
        self.assertEqual(self.current()["phase"], "complete")
        done.assert_called_once_with(self.paths, "autoformalize")
        formal.assert_not_awaited()

    async def test_critic_exhaustion_rotates_with_independent_attempt_budget(self):
        result, _, _ = await self.invoke(["missing", "missing", "approved"])
        self.assertEqual(result.exit_code, 0, result.output + repr(result.exception))
        self.assertEqual(self.calls, [("Ada", 1), ("Ada", 2), ("Bert", 1)])

    async def test_every_critic_exhausts_without_acceptance(self):
        result, done, _ = await self.invoke(["missing"] * 4)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("every configured agent exhausted", result.output)
        self.assertEqual(self.calls, [("Ada", 1), ("Ada", 2), ("Bert", 1), ("Bert", 2)])
        self.assertEqual(self.current()["phase"], "critic")
        done.assert_not_called()

    async def test_empty_round_stops_without_budget_or_unchanged_critic_on_resume(self):
        self.submit("lean_reopen")

        async def formalizing(_roster, paths, *_args):
            return state.record_round_end(paths.forum,
                blocked_launches={"Ada/alpha": "encoding repair is exhausted"}, activity={})

        for _ in range(2):
            with patch.object(command, "_prepare_critic_snapshot") as critic, \
                 patch.object(command, "_save_incomplete_report") as incomplete:
                result, done, formal = await self.invoke(limit="1", formalizing=formalizing)
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("formalization blocked", result.output)
            self.assertIn("no proof attempt was charged", result.output)
            self.assertNotIn("exhausted MAX_ATTEMPTS", result.output)
            critic.assert_not_called()
            incomplete.assert_called_once_with(self.paths)
            formal.assert_awaited_once()
            done.assert_not_called()
            self.assertEqual(self.current()["formalization"]["last_round"]["outcome"], "blocked")
            self.assertEqual(len(self.current()["critic_verdicts"]), 1)

    async def test_complete_zero_dispatch_round_still_gets_final_review(self):
        with state.transaction(self.forum) as current:
            current["phase"] = "formalizing"

        async def formalizing(_roster, paths, *_args):
            result = state.record_round_end(paths.forum, blocked_launches={}, activity={})
            self.assertEqual(result["formalization"]["last_round"]["outcome"], "complete")
            self.assertFalse(command._formal_round_attempted(result, None))
            return result

        result, done, formal = await self.invoke(["approved"], limit="1", formalizing=formalizing)
        self.assertEqual(result.exit_code, 0, result.output + repr(result.exception))
        formal.assert_awaited_once()
        done.assert_called_once_with(self.paths, "autoformalize")

    async def test_open_source_issue_cannot_hide_empty_blocked_round(self):
        self.submit("lean_reopen")
        with state.transaction(self.forum) as current:
            source = state.formal_source(current)
            current["source_issues"]["source-block"] = {
                "issue_id": "source-block", "status": "open", "task_ids": ["alpha"],
                "source_candidate": source["candidate_id"], "source_sha256": source["sha256"],
                "attempts": [], "repair_ids": [], "owner": None,
            }

        async def formalizing(_roster, paths, *_args):
            return state.record_round_end(paths.forum, blocked_launches={}, activity={})

        with patch.object(command, "_prepare_critic_snapshot") as critic:
            result, done, formal = await self.invoke(limit="1", formalizing=formalizing)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("formalization blocked", result.output)
        formal.assert_awaited_once()
        critic.assert_not_called()
        done.assert_not_called()

    async def test_round_accounting_uses_actual_dispatch_and_preserves_legacy_records(self):
        self.submit("lean_reopen")
        for counter in ("worker_launches", "integrations", "review_launches", "source_repairs"):
            with self.subTest(counter=counter):
                current = state.record_round_end(self.forum, blocked_launches={}, activity={counter: 1})
                summary = current["formalization"]["last_round"]
                self.assertEqual(summary["outcome"], "attempted")
                self.assertTrue(command._formal_round_attempted(current, None))
                self.assertFalse(command._formal_round_attempted(current, summary["round_id"]))
        legacy = state.record_round_end(self.forum, blocked_launches={})
        self.assertTrue(command._formal_round_attempted(legacy, None))
        self.assertFalse(command._formal_round_attempted({"formalization": {}}, None))

    async def test_source_repair_author_is_excluded_from_critic_rotation(self):
        with state.transaction(self.forum) as current:
            current["formalization"]["spec"]["arguments"][0]["repair_ids"] = ["repair-1"]
            current["source_repairs"]["repair-1"] = {"author": "Ada"}
        selected = []

        async def critic(_roster, _paths, *, critic, attempt):
            selected.append((critic.name, attempt))
            with state.transaction(self.forum) as current:
                current["phase"] = "formalizing"

        with patch.object(command, "_run_critic", side_effect=critic):
            await command._run_critics(self.roster, self.paths, 2)
        self.assertEqual(selected, [("Bert", 1)])

    async def test_no_independent_critic_fails_before_dispatch(self):
        with state.transaction(self.forum) as current:
            current["formalization"]["spec"]["arguments"][0]["repair_ids"] = ["repair-1", "repair-2"]
            current["source_repairs"].update({"repair-1": {"author": "Ada"}, "repair-2": {"author": "Bert"}})
        with patch.object(command, "_run_critic", new_callable=AsyncMock) as critic:
            with self.assertRaisesRegex(click.ClickException, "No independent critic"):
                await command._run_critics(self.roster, self.paths, 2)
        critic.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
