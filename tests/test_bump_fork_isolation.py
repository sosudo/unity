"""Bump's modules, files and advertised tools stay workflow-owned; no providers or Lean."""

import ast
import asyncio
import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity import formalize_input, formalize_jobs, formalize_state
from unity import bump_input, bump_jobs, bump_orchestrator, bump_spawn, bump_state
from unity.config import Paths
from unity.forum import formalize_server, bump_server


SOURCE = Path(__file__).resolve().parents[1] / "unity"


class BumpForkIsolationTests(unittest.TestCase):
    def test_workflow_modules_do_not_import_other_structured_workflows(self):
        files = [*SOURCE.glob("bump_*.py"), SOURCE / "commands/bump.py",
                 SOURCE / "forum/bump_server.py"]
        self.assertGreater(len(files), 20)
        forbidden = ("formalize", "autoformalize", "solve", "prove")
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

    def test_owned_functions_are_not_aliases_to_formalize(self):
        for suffix, function in (("state", "load_state"), ("runtime", "run_formalizing_runtime"),
                                 ("spawn", "spawn"), ("contract", "build_sources"),
                                 ("jobs", "run"), ("representation", "current_representation_review"),
                                 ("report", "completion_report")):
            with self.subTest(module=suffix):
                owned = importlib.import_module("unity.bump_" + suffix)
                original = importlib.import_module("unity.formalize_" + suffix)
                self.assertIsNot(getattr(owned, function), getattr(original, function))
                self.assertEqual(getattr(owned, function).__module__, "unity.bump_" + suffix)

    def test_forum_paths_and_state_files_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths.from_unity_dir(Path(directory) / ".unity")
            own = bump_input.bump_paths(paths)
            original = formalize_input.formalize_paths(paths)
            self.assertEqual(own.forum, paths.forum / "bump")
            self.assertEqual(original.forum, paths.forum / "formalize")
            self.assertNotEqual(bump_state.state_path(own.forum), formalize_state.state_path(original.forum))
            self.assertEqual(own.project_root, paths.project_root)
            self.assertEqual(own.artifacts, paths.artifacts)
            with bump_state.transaction(own.forum) as state:
                state["run_id"] = "bump-only"
            self.assertEqual(bump_state.load_state(own.forum)["run_id"], "bump-only")
            self.assertFalse(formalize_state.state_path(original.forum).exists())
            self.assertEqual(formalize_state.load_state(original.forum)["run_id"], "")

    def test_registered_job_directories_and_cancellation_tokens_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(bump_jobs._jobs_dir(root), root / ".unity/jobs/bump")
            self.assertEqual(formalize_jobs._jobs_dir(root), root / ".unity/jobs/formalize")
            self.assertIsNot(bump_jobs._cancellation, formalize_jobs._cancellation)

    def test_phase_tools_are_bump_owned_without_cross_workflow_tools(self):
        for profile in bump_server.PROFILES:
            with self.subTest(profile=profile):
                server = bump_server.build_server(profile)
                names = {tool.name for tool in asyncio.run(server.list_tools())}
                self.assertTrue({"bump_task", "bump_brief"}.issubset(names))
                self.assertFalse(any(name.startswith(("formalize_", "autoformalize_", "solve_", "prove_")) for name in names))
                self.assertNotIn("submit_solution_candidate", names)
                self.assertNotIn("prepare_formal_worktree", names)
                self.assertEqual("submit_formalization_verdict" in names, profile == "critic")
                self.assertEqual("finalize_formalization" in names, profile == "formalizing")
                for tool in bump_server.PROFILE_TOOLS[profile]:
                    self.assertEqual(tool.__module__, "unity.forum.bump_server")

    def test_bump_server_configuration_does_not_mutate_formalize(self):
        before = (formalize_server.FORUM_DIR, formalize_server.PROJECT_ROOT,
                  formalize_server.PROFILE)
        own_before = (bump_server.FORUM_DIR, bump_server.PROJECT_ROOT, bump_server.PROFILE)
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bump_server.configure(root / ".unity/forum/bump", root, "critic")
                self.assertEqual(bump_server.PROFILE, "critic")
                self.assertEqual(before, (formalize_server.FORUM_DIR, formalize_server.PROJECT_ROOT,
                                          formalize_server.PROFILE))
        finally:
            bump_server.FORUM_DIR, bump_server.PROJECT_ROOT, bump_server.PROFILE = own_before

    def test_mcp_configuration_uses_own_server_and_runtime_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            paths = bump_input.bump_paths(Paths.from_unity_dir(Path(directory) / ".unity"))
            config = bump_orchestrator.build_bump_mcp(paths, "formalizing")
            self.assertEqual(set(config), {"lean-lsp", "unity-forum"})
            args = config["unity-forum"]["args"]
            self.assertIn("unity.forum.bump_server", args)
            self.assertIn(str(paths.forum), args)
            self.assertNotIn("unity.forum.formalize_server", args)
            runtime = {"UNITY_BUMP_TASK_ID": "own-task", "UNITY_FORMALIZE_TASK_ID": "other-task",
                       "API_KEY": "test-secret"}
            rebound = bump_spawn.bump_mcp_with_runtime_env(config, runtime)
            env = rebound["lean-lsp"]["env"]
            self.assertEqual(env["UNITY_BUMP_TASK_ID"], "own-task")
            self.assertNotIn("UNITY_FORMALIZE_TASK_ID", env)
            self.assertNotIn("API_KEY", env)
            self.assertEqual(config["lean-lsp"]["env"], {})

    def test_prompts_and_catalogs_do_not_advertise_other_workflows_tools(self):
        prompts = [*SOURCE.glob("prompts/BUMP_*_TOOLS.md"),
                   *SOURCE.glob("prompts/bump/*.md")]
        self.assertGreater(len(prompts), 10)
        for path in prompts:
            with self.subTest(prompt=path.name):
                text = path.read_text()
                for name in ("formalize_task(", "formalize_brief(", "autoformalize_task(", "autoformalize_brief(", "solve_task(",
                             "solve_brief(", "submit_solution_candidate(", "reopen_solving("):
                    self.assertNotIn(name, text)


if __name__ == "__main__":
    unittest.main()
