"""Manifest repair scheduling regressions; no models, Lean, or remote services."""

import asyncio
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from unity import solve_formal_runtime as runtime


def repair_record():
    return {"repair_id": "repair-1", "input_sha256": "input-1", "task_id": "root",
            "kind": "output_manifest_changed", "context": {}, "attempts": [], "status": "open",
            "blockers": [{"code": "output_manifest_changed", "task_ids": ["root"],
                          "message": "The adopted manifest differs.",
                          "required_action": "Explicitly reopen the representation."}]}


class ManifestRuntimeUnitTests(unittest.TestCase):
    def test_candidate_preflight_receives_exact_manifest(self):
        outputs = [{"declaration": "Witness", "file": "Witness.lean"}]
        candidate = {"task_id": "root", "stage": "complete", "outputs": outputs}
        with patch.object(runtime.solve_state, "submission_blockers", return_value=[]) as check:
            self.assertEqual(runtime._candidate_preflight({}, candidate), [])
        check.assert_called_once_with({}, "root", "complete", outputs=outputs)

    def test_integration_preflight_precedes_git_and_build(self):
        state = {"formalization": {"contract": {"version": 3}}}
        blocker = {"message": "The adopted manifest differs."}
        with patch.object(runtime.solve_state, "load_state", return_value=state), \
             patch.object(runtime, "require_source_matches"), \
             patch.object(runtime.solve_state, "candidate_is_current", return_value=True), \
             patch.object(runtime, "_candidate_preflight", return_value=[blocker]), \
             patch.object(runtime.worktree, "verify_candidate_commit") as git, \
             patch.object(runtime.solve_contract, "build_sources") as build:
            result = runtime._apply_formal_candidate(
                SimpleNamespace(project_root=Path("/unused"), forum=Path("/unused")), {}, {},
            )
        self.assertEqual(result["verification"]["mode"], "preflight")
        git.assert_not_called()
        build.assert_not_called()

    def test_repair_survives_rejection_while_opportunistic_nudge_is_suppressed(self):
        prompt = runtime._compose_formal_task_prompt(
            recovery="RECOVER REJECTED CANDIDATE", resume="resume", followup="Submission check only",
            normal="normal", representation="representation instructions", repair=repair_record(),
        )
        for text in ("MANIFEST REPAIR", "RECOVER REJECTED CANDIDATE", "normal",
                     "reopen_representations", "Preserve existing Lean", "representation instructions"):
            self.assertIn(text, prompt)
        self.assertNotIn("Submission check only", prompt)

    def test_nudge_without_rejection_keeps_dedicated_repair_instructions(self):
        prompt = runtime._compose_formal_task_prompt(
            recovery="", resume="resume", followup="Submission check only",
            normal="normal", representation="representation instructions", repair=repair_record(),
        )
        self.assertIn("MANIFEST REPAIR", prompt)
        self.assertIn("Submission check only", prompt)
        self.assertIn("resume", prompt)

    def test_scope_includes_transitive_dependents_not_unrelated_work(self):
        state = {"formal_tasks": {"root": {}, "dependent": {"dependencies": ["root"]},
                                  "last": {"dependencies": ["dependent"]}, "unrelated": {}}}
        self.assertEqual(runtime._manifest_repair_scope(state, repair_record()), {"root", "dependent", "last"})

    def test_pending_current_candidate_wins_over_repair(self):
        state = {"formal_tasks": {"root": {}, "dependent": {"dependencies": ["root"]}},
                 "formal_candidates": {"candidate": {"task_id": "dependent", "status": "submitted"}}}
        with patch.object(runtime.solve_state, "candidate_is_current", return_value=True):
            self.assertTrue(runtime._manifest_repair_pending_candidate(state, repair_record()))
        with patch.object(runtime.solve_state, "candidate_is_current", return_value=False):
            self.assertFalse(runtime._manifest_repair_pending_candidate(state, repair_record()))

    def test_overlapping_diagnostics_do_not_cancel_current_owner(self):
        first, second = repair_record(), repair_record()
        second.update(repair_id="repair-2", input_sha256="input-2")
        state = {"formal_tasks": {"root": {}, "dependent": {"dependencies": ["root"]}}}
        self.assertEqual(runtime._select_manifest_repairs(
            state, [first, second], {("repair-2", "input-2")},
        ), [second])
        first["status"] = "exhausted"
        self.assertEqual(runtime._select_manifest_repairs(state, [first, second], set()), [second])


class ManifestRuntimeSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = SimpleNamespace(project_root=self.root, forum=self.root, unity=self.root)
        self.state = {"phase": "formalizing", "run_id": "test", "events": [], "worker_tasks": {},
                      "formalization": {"main_sha": "main", "revision": 1}, "formal_candidates": {},
                      "formal_tasks": {"root": {"task_id": "root", "status": "pending", "revision": 1}},
                      "strategies": {}, "retired_tasks": {}, "manifest_repairs": {}}
        self.yielded = set()
        self.names = ["Ada", "Bert"]
        self.finishes = []

    def current_repairs(self, state, task_id=""):
        return [deepcopy(row) for row in state["manifest_repairs"].values()
                if row["status"] in {"open", "exhausted"} and (not task_id or row["task_id"] == task_id)]

    def begin(self, _forum, repair_id, author):
        repair = self.state["manifest_repairs"][repair_id]
        if any(row["author"] == author.casefold() for row in repair["attempts"]):
            return {"status": "attempted", "repair": deepcopy(repair)}
        if any(row["status"] == "started" for row in repair["attempts"]):
            return {"status": "conflict", "repair": deepcopy(repair)}
        repair["attempts"].append({"author": author.casefold(), "status": "started"})
        return {"status": "started", "repair": deepcopy(repair)}

    def finish(self, _forum, repair_id, author, outcome):
        self.finishes.append((author.casefold(), outcome))
        for row in self.state["manifest_repairs"][repair_id]["attempts"]:
            if row["author"] == author.casefold() and row["status"] == "started":
                row["status"] = outcome

    async def run_runtime(self, worker, *, prepare=None, real_eligibility=False):
        agents = [SimpleNamespace(name=name, backend="claude") for name in self.names]
        roster = SimpleNamespace(agents=agents)
        ss = runtime.solve_state
        fs = runtime.solve_server
        with ExitStack() as stack:
            def mock(obj, name, **kwargs):
                return stack.enter_context(patch.object(obj, name, create=True, **kwargs))
            for name in ("configure_forum", "require_source_matches"):
                mock(runtime, name)
            mock(runtime, "stop_requested", return_value=False)
            mock(runtime, "_formal_worktree", return_value=self.root)
            mock(runtime, "_git", return_value=SimpleNamespace(stdout="", returncode=0))
            mock(runtime, "_agent_runtime_env", return_value={})
            mock(runtime, "_formal_launch_retry_key", return_value="retry")
            mock(runtime, "_preamble", return_value="")
            mock(runtime, "load_prompt", return_value="")
            mock(runtime, "forum_brief", return_value="")
            mock(runtime, "_rejection_recovery_prompt", return_value="RECOVER REJECTED CANDIDATE")
            mock(runtime, "spawn", side_effect=worker)
            mock(runtime.library, "library_context", return_value="")
            mock(runtime.library, "library_subagents", return_value=[])
            for name in ("symlink_lake_cache", "link_runtime_state", "cleanup_worktree"):
                mock(runtime.worktree, name)
            mock(runtime.solve_jobs, "terminate")
            mock(runtime.solve_representation, "recover_representation_reviews")
            mock(runtime.solve_representation, "pending_representation_reviews", return_value=[])
            mock(ss, "load_state", side_effect=lambda *_: deepcopy(self.state))
            mock(ss, "current_manifest_repairs", side_effect=self.current_repairs)
            mock(ss, "begin_manifest_repair_attempt", side_effect=self.begin)
            mock(ss, "finish_manifest_repair_attempt", side_effect=self.finish)
            mock(ss, "mark_manifest_repair_exhausted", side_effect=lambda _, key:
                 self.state["manifest_repairs"][key].update(status="exhausted"))
            if not real_eligibility:
                mock(ss, "ready_formal_tasks", side_effect=lambda state:
                     [row for row in state["formal_tasks"].values() if row["status"] == "pending"])
                mock(ss, "task_available_to", side_effect=lambda state, name, task:
                     task in state["formal_tasks"] and state["formal_tasks"][task]["status"] == "pending"
                     and (name, task) not in self.yielded)
            mock(ss, "source_issues_blocking_task", return_value=[])
            mock(ss, "ready_source_issues", return_value=[])
            mock(ss, "open_source_issues", return_value=[])
            mock(ss, "pending_replan", return_value=None)
            if real_eligibility:
                mock(ss, "record_worker_yield", side_effect=lambda _, name, task, reason, *, snapshot:
                     ss._record_yield(self.state, name, task, reason, [], snapshot))
            else:
                mock(ss, "has_yielded", side_effect=lambda _, name, task: (name, task) in self.yielded)
                mock(ss, "snapshot_attempt", side_effect=lambda _, name, task: {"task_id": task})
                mock(ss, "record_worker_yield", side_effect=lambda _, name, task, *args, **kwargs:
                     self.yielded.add((name, task)))
            mock(ss, "record_round_end", side_effect=lambda *args, **kwargs: deepcopy(self.state))
            mock(ss, "all_formal_tasks_complete", return_value=False)
            mock(fs, "has_pending_formal_candidate", return_value=False)
            if not real_eligibility:
                mock(fs, "unresolved_formal_tasks", return_value=[])
            mock(fs, "verification_blockers", return_value=[])
            mock(fs, "prepare_formal_worktree", side_effect=prepare or (lambda *args, **kwargs: {"ok": True}))
            return await asyncio.wait_for(runtime.run_formalizing_runtime(roster, self.paths, {}, "formalize"), 4)

    async def test_one_author_per_input_then_exhaustion_without_generic_relaunch(self):
        self.state["manifest_repairs"]["repair-1"] = repair_record()
        launched = []
        active = 0
        maximum = 0

        async def worker(agent, _system, prompt, *_args, **_kwargs):
            nonlocal active, maximum
            self.assertIn("MANIFEST REPAIR", prompt)
            self.assertIn("RECOVER REJECTED CANDIDATE", prompt)
            launched.append(agent.name)
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1

        await self.run_runtime(worker)
        self.assertEqual(launched, ["Ada", "Bert"])
        self.assertEqual(maximum, 1)
        self.assertEqual(self.state["manifest_repairs"]["repair-1"]["status"], "exhausted")

    async def test_restart_consumes_interrupted_attempt_and_uses_next_author(self):
        repair = repair_record()
        repair["attempts"] = [{"author": "ada", "status": "started"}]
        self.state["manifest_repairs"]["repair-1"] = repair
        launched = []

        async def worker(agent, *_args, **_kwargs):
            launched.append(agent.name)

        await self.run_runtime(worker)
        self.assertEqual(launched, ["Bert"])
        self.assertIn(("ada", "interrupted"), self.finishes)

    async def test_real_claim_and_yield_gates_allow_next_repair_author(self):
        self.state["manifest_repairs"]["repair-1"] = repair_record()
        self.state["strategies"]["strategy"] = {
            "strategy_id": "strategy", "phase": "formalizing", "target": "root",
            "task_revision": 1, "status": "claimed", "owner": "Ada", "assistants": [],
            "description_key": "repair-approach",
        }
        launched = []

        async def worker(agent, *_args, **_kwargs):
            launched.append(agent.name)
            if agent.name == "Bert":
                self.assertEqual(self.state["strategies"]["strategy"]["status"], "registered")
                self.assertEqual(runtime.solve_server.unresolved_formal_tasks(self.state, "Ada"), [])

        await self.run_runtime(worker, real_eligibility=True)
        self.assertEqual(launched, ["Ada", "Bert"])
        self.assertEqual(self.state["manifest_repairs"]["repair-1"]["status"], "exhausted")

    async def test_preparation_race_does_not_launch_stale_repair(self):
        self.state["manifest_repairs"]["repair-1"] = repair_record()
        launched = []

        def prepare(*_args, **_kwargs):
            self.state["manifest_repairs"]["repair-1"]["status"] = "cleared"
            self.state["formal_tasks"]["root"]["status"] = "complete"
            return {"ok": True}

        async def worker(agent, *_args, **_kwargs):
            launched.append(agent.name)

        await self.run_runtime(worker, prepare=prepare)
        self.assertEqual(launched, [])
        self.assertEqual(self.state["manifest_repairs"]["repair-1"]["attempts"], [])

    async def test_new_repair_drains_dependents_preserving_unrelated_worker_and_source(self):
        self.names.append("Cleo")
        self.state["formal_tasks"].update({
            "dependent": {"task_id": "dependent", "status": "pending", "revision": 1, "dependencies": ["root"]},
            "other": {"task_id": "other", "status": "pending", "revision": 1},
        })
        private = self.root / "private.lean"
        private.write_text("retained private proof")
        active = set()
        cancelled = set()
        repaired = asyncio.Event()

        async def worker(agent, _system, prompt, *_args, **kwargs):
            target = kwargs["log_context"]["task_id"]
            if "MANIFEST REPAIR" in prompt:
                self.assertEqual(active, {"other"})
                self.assertEqual(private.read_text(), "retained private proof")
                self.state["manifest_repairs"]["repair-1"]["status"] = "cleared"
                for task in ("root", "dependent"):
                    self.state["formal_tasks"][task]["status"] = "complete"
                repaired.set()
                return
            active.add(target)
            if len(active) == 3:
                self.state["manifest_repairs"]["repair-1"] = repair_record()
            try:
                if target == "other":
                    await repaired.wait()
                    self.state["formal_tasks"][target]["status"] = "complete"
                else:
                    await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.add(target)
                raise
            finally:
                active.discard(target)

        await self.run_runtime(worker)
        self.assertEqual(cancelled, {"root", "dependent"})
        self.assertTrue(repaired.is_set())
        self.assertEqual(private.read_text(), "retained private proof")


if __name__ == "__main__":
    unittest.main()
