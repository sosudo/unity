"""Optimize adapters and dashboard: entirely offline, no compiler/model calls."""
import json
import math
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from asyncclick.testing import CliRunner
from fastapi.testclient import TestClient
from unity.commands import optimize as command
from unity import optimize_tasks as tasks, optimize_workflow as workflow
from unity.config import Paths
from unity.forum import web


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, outcome, *args):
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths.from_unity_dir(Path(directory) / ".unity")
            roster = SimpleNamespace(agents=[SimpleNamespace(name="Primary")])
            roster.primary = roster.agents[0]
            run = AsyncMock(return_value={"status": outcome})
            with patch.object(command, "load_paths", return_value=paths), \
                 patch.object(command, "load_roster", return_value=roster), \
                 patch.object(workflow, "run", run), \
                 patch.dict(os.environ, {"MAX_ATTEMPTS": "5", "RETROSPECTIVE": "false"}):
                result = await CliRunner().invoke(command.optimize, ["length", *args])
            return result, run

    async def test_existing_interface_and_optional_retrospective(self):
        result, run = await self.invoke("accepted", "--targets", "A.foo", "--continue")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("checked improvement", result.output)
        self.assertEqual(run.await_args.kwargs, {"metric": "length", "targets": "A.foo",
                         "continue_": True, "max_attempts": 5, "retrospective": False})

    async def test_nonaccepted_results_are_never_called_complete(self):
        for outcome in ("stopped", "exhausted", "blocked", "no_improvement_found"):
            result, _ = await self.invoke(outcome)
            self.assertEqual(result.exit_code, 0)
            self.assertNotIn("optimize complete", result.output)
            self.assertIn(outcome, result.output)

    def test_round_budget_units_and_validation(self):
        for value in ("0", "-1", "1.5", "garbage"):
            with patch.dict(os.environ, {"MAX_ATTEMPTS": value}), self.assertRaises(Exception):
                command._attempt_limit()
        with patch.dict(os.environ, {"MAX_ATTEMPTS": ""}):
            self.assertEqual(command._attempt_limit(), math.inf)


class WebTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = workflow.paths_for(self.root)
        self.paths.forum.mkdir(parents=True)
        self.paths.logs.mkdir()
        tasks.initialize(self.paths.forum, self.root, run_id="fixture", base_sha="base", metric="length",
                         scope={"targets": "Universe"}, config={"primary": "P", "agents": ["P"]})
        with tasks.transaction(self.paths.forum) as state:
            state["chunks"] = {"a": {"name": "A.foo"}}
            state["tasks"] = {"a": {"task_id": "a", "chunk_ids": ["a"], "dependencies": [], "status": "no_improvement"}}
        for key, value in (("ROOT_DIR", self.paths.unity), ("FORUM_DIR", self.paths.unity / "forum")):
            patcher = patch.object(web, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(web.app)

    def test_readonly_state_dag_and_noop_status(self):
        (self.paths.unity / "state.json").write_text(json.dumps({"command": "optimize", "phase": "optimizing"}))
        before = (self.paths.forum / tasks.STATE_FILE).read_bytes()
        with patch.object(tasks, "transaction", side_effect=AssertionError("dashboard write")):
            state = self.client.get("/api/optimize-state").json()
            graph = self.client.get("/api/dag").json()
            measured = self.client.get("/api/optimize-metrics").json()
        self.assertEqual(state["schema"], tasks.SCHEMA)
        self.assertEqual(graph["graph_kind"], "optimize")
        self.assertEqual(graph["chunks"][0]["outcome"], "no_improvement")
        self.assertEqual(graph["acceptance"], "provisional")
        self.assertEqual(measured["tasks"], {"no_improvement": 1})
        self.assertEqual((self.paths.forum / tasks.STATE_FILE).read_bytes(), before)

    def test_other_pipeline_does_not_read_optimize(self):
        for pipeline in ("prove", "solve", "bump", "formalize", "create", "verify"):
            (self.paths.unity / "state.json").write_text(json.dumps({"command": pipeline}))
            with patch.object(tasks, "load", side_effect=AssertionError("cross-pipeline read")):
                self.assertEqual(self.client.get("/api/optimize-state").json(), {})
                self.assertEqual(self.client.get("/api/optimize-metrics").json(), {})


class PromptContracts(unittest.TestCase):
    def test_approved_roles_and_no_mutable_approval(self):
        prompts = Path(workflow.__file__).parent / "prompts"
        critic = (prompts / "optimize/CRITIC.md").read_text()
        tools = (prompts / "OPTIMIZE_TOOLS.md").read_text()
        self.assertIn("read-only", critic)
        self.assertIn("no independent/reserved reviewer", critic)
        self.assertIn("All agents optimize", tools)
        self.assertIn("No spot-fixes", critic)
        chunking = (prompts / "optimize/CHUNKING.md").read_text()
        self.assertIn("including clean declarations", chunking)
        self.assertIn("Baseline is immutable", chunking)

    def test_common_prompt_distinguishes_task_and_phase_turns(self):
        prompts = Path(workflow.__file__).parent / "prompts"
        common = (prompts / "OPTIMIZE_TOOLS.md").read_text()
        self.assertIn("Call optimize_task only when the controller supplies a real bound task ID", common)
        self.assertIn('task_id=""', common)
        self.assertIn("Only an actual optimization assignment submits a candidate", common)
        self.assertIn("When the assigned turn permits source edits", common)
        self.assertIn("optimize_inventory", common)
        self.assertIn("artifact_read", common)
        self.assertIn("next_offset", common)
        self.assertIn("--args-file", common)
        self.assertNotIn("Read optimize_status, optimize_brief and optimize_task", common)

    def test_preparation_prompts_request_phase_results_not_optimization_candidates(self):
        prompts = Path(workflow.__file__).parent / "prompts/optimize"
        for phase in ("ARCHITECT", "EXPLORATION", "CHUNKING"):
            with self.subTest(phase=phase):
                prompt = (prompts / f"{phase}.md").read_text()
                self.assertIn("report_phase_result", prompt)
                self.assertIn('task_id=""', prompt)
                self.assertIn("complete", prompt)
                self.assertIn("blocked", prompt)
                self.assertNotIn("submit_candidate", prompt)
        self.assertIn("skipped for a", (prompts / "ARCHITECT.md").read_text())
        for phase in ("PREPARATION", "OPTIMIZING", "CRITIC", "RETROSPECTIVE"):
            self.assertNotIn("report_phase_result", (prompts / f"{phase}.md").read_text())

    def test_chunk_prompt_explains_canonical_paging_and_explicit_scope_accounting(self):
        prompt = (Path(workflow.__file__).parent / "prompts/optimize/CHUNKING.md").read_text()
        for term in ("optimize_inventory", "next_offset", "canonical chunk IDs", "inventory_sha256",
                     "exclude_outside_paths", "exclusion_reason", "Universe forbids this selector",
                     "no omission implies exclusion", "full configured build scope is unchanged",
                     "--args-file", "does not substitute"):
            with self.subTest(term=term):
                self.assertIn(term, prompt)

    def test_optimizing_prompt_keeps_primary_selection_and_verifier_scoring_read_only(self):
        prompt = (Path(workflow.__file__).parent / "prompts/optimize/OPTIMIZING.md").read_text()
        self.assertIn("For a real bound optimization task", prompt)
        self.assertIn("primary candidate selection and verifier scoring turns", prompt)
        self.assertIn("read-only turns without a bound task", prompt)
        self.assertIn("Neither turn registers a strategy or submits a candidate", prompt)
        self.assertIn("without replacing the original baseline", prompt)
        self.assertIn("submit_score_report", prompt)
        self.assertIn("workers do not need to create measurement records before submission", prompt)

    def test_owned_runtime_does_not_import_bump_workflow_or_prove(self):
        import ast
        root = Path(workflow.__file__).parent
        for file in root.glob("optimize*.py"):
            tree = ast.parse(file.read_text())
            imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
            for node in imports:
                names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                self.assertFalse(any("bump" in name or "prove" in name for name in names), (file, names))
