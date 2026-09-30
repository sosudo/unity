"""Formalize's modules, files and advertised tools stay workflow-owned; no providers or Lean."""

import ast
import asyncio
import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity import autoformalize_input, autoformalize_jobs, autoformalize_state
from unity import formalize_input, formalize_jobs, formalize_orchestrator, formalize_spawn, formalize_state
from unity.config import Paths
from unity.forum import autoformalize_server, formalize_server


SOURCE = Path(__file__).resolve().parents[1] / "unity"


class FormalizeForkIsolationTests(unittest.TestCase):
    def test_workflow_modules_do_not_import_other_structured_workflows(self):
        files = [*SOURCE.glob("formalize_*.py"), SOURCE / "commands/formalize.py",
                 SOURCE / "forum/formalize_server.py"]
        self.assertGreater(len(files), 20)
        forbidden = ("autoformalize", "solve", "prove")
        for path in files:
            with self.subTest(module=path.name):
                for node in ast.walk(ast.parse(path.read_text())):
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        names = [node.module or "", *(alias.name for alias in node.names)]
                    else:
                        continue
                    for name in names:
                        self.assertFalse(any(part.startswith(forbidden) for part in name.split(".")), name)

    def test_owned_functions_are_not_aliases_to_autoformalize(self):
        for suffix, function in (("state", "load_state"), ("runtime", "run_formalizing_runtime"),
                                 ("spawn", "spawn"), ("contract", "build_sources"),
                                 ("jobs", "run"), ("representation", "current_representation_review"),
                                 ("report", "completion_report")):
            with self.subTest(module=suffix):
                owned = importlib.import_module("unity.formalize_" + suffix)
                original = importlib.import_module("unity.autoformalize_" + suffix)
                self.assertIsNot(getattr(owned, function), getattr(original, function))
                self.assertEqual(getattr(owned, function).__module__, "unity.formalize_" + suffix)

    def test_forum_paths_and_state_files_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths.from_unity_dir(Path(directory) / ".unity")
            own = formalize_input.formalize_paths(paths)
            original = autoformalize_input.autoformalize_paths(paths)
            self.assertEqual(own.forum, paths.forum / "formalize")
            self.assertEqual(original.forum, paths.forum / "autoformalize")
            self.assertNotEqual(formalize_state.state_path(own.forum), autoformalize_state.state_path(original.forum))
            self.assertEqual(own.project_root, paths.project_root)
            self.assertEqual(own.artifacts, paths.artifacts)
            with formalize_state.transaction(own.forum) as state:
                state["run_id"] = "formalize-only"
            self.assertEqual(formalize_state.load_state(own.forum)["run_id"], "formalize-only")
            self.assertFalse(autoformalize_state.state_path(original.forum).exists())
            self.assertEqual(autoformalize_state.load_state(original.forum)["run_id"], "")

    def test_registered_job_directories_and_cancellation_tokens_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(formalize_jobs._jobs_dir(root), root / ".unity/jobs/formalize")
            self.assertEqual(autoformalize_jobs._jobs_dir(root), root / ".unity/jobs/autoformalize")
            self.assertIsNot(formalize_jobs._cancellation, autoformalize_jobs._cancellation)

    def test_phase_tools_are_formalize_owned_without_cross_workflow_tools(self):
        for profile in formalize_server.PROFILES:
            with self.subTest(profile=profile):
                server = formalize_server.build_server(profile)
                names = {tool.name for tool in asyncio.run(server.list_tools())}
                self.assertTrue({"formalize_task", "formalize_brief"}.issubset(names))
                self.assertFalse(any(name.startswith(("autoformalize_", "solve_", "prove_")) for name in names))
                self.assertNotIn("submit_solution_candidate", names)
                self.assertNotIn("prepare_formal_worktree", names)
                self.assertEqual("submit_formalization_verdict" in names, profile == "critic")
                self.assertEqual("finalize_formalization" in names, profile == "formalizing")
                for tool in formalize_server.PROFILE_TOOLS[profile]:
                    self.assertEqual(tool.__module__, "unity.forum.formalize_server")

    def test_formalize_server_configuration_does_not_mutate_autoformalize(self):
        before = (autoformalize_server.FORUM_DIR, autoformalize_server.PROJECT_ROOT,
                  autoformalize_server.PROFILE)
        own_before = (formalize_server.FORUM_DIR, formalize_server.PROJECT_ROOT, formalize_server.PROFILE)
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                formalize_server.configure(root / ".unity/forum/formalize", root, "critic")
                self.assertEqual(formalize_server.PROFILE, "critic")
                self.assertEqual(before, (autoformalize_server.FORUM_DIR, autoformalize_server.PROJECT_ROOT,
                                          autoformalize_server.PROFILE))
        finally:
            formalize_server.FORUM_DIR, formalize_server.PROJECT_ROOT, formalize_server.PROFILE = own_before

    def test_mcp_configuration_uses_own_server_and_runtime_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            paths = formalize_input.formalize_paths(Paths.from_unity_dir(Path(directory) / ".unity"))
            config = formalize_orchestrator.build_formalize_mcp(paths, "formalizing")
            self.assertEqual(set(config), {"lean-lsp", "unity-forum"})
            args = config["unity-forum"]["args"]
            self.assertIn("unity.forum.formalize_server", args)
            self.assertIn(str(paths.forum), args)
            self.assertNotIn("unity.forum.autoformalize_server", args)
            runtime = {"UNITY_FORMALIZE_TASK_ID": "own-task", "UNITY_AUTOFORMALIZE_TASK_ID": "other-task",
                       "API_KEY": "test-secret"}
            rebound = formalize_spawn.formalize_mcp_with_runtime_env(config, runtime)
            env = rebound["lean-lsp"]["env"]
            self.assertEqual(env["UNITY_FORMALIZE_TASK_ID"], "own-task")
            self.assertNotIn("UNITY_AUTOFORMALIZE_TASK_ID", env)
            self.assertNotIn("API_KEY", env)
            self.assertEqual(config["lean-lsp"]["env"], {})

    def test_prompts_and_catalogs_do_not_advertise_other_workflows_tools(self):
        prompts = [*SOURCE.glob("prompts/FORMALIZE_*_TOOLS.md"),
                   *SOURCE.glob("prompts/formalize/*.md")]
        self.assertGreater(len(prompts), 10)
        for path in prompts:
            with self.subTest(prompt=path.name):
                text = path.read_text()
                for name in ("autoformalize_task(", "autoformalize_brief(", "solve_task(",
                             "solve_brief(", "submit_solution_candidate(", "reopen_solving("):
                    self.assertNotIn(name, text)


if __name__ == "__main__":
    unittest.main()
