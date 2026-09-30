"""Existing-project Formalize lifecycle with no provider, Lean or real Git calls."""

import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from asyncclick.testing import CliRunner

from unity import formalize_project, formalize_state
from unity.commands import formalize as command
from unity.config import Paths
from unity.formalize_input import formalize_paths, scope_bytes, snapshot_sources


class FormalizeCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.base = Paths.from_unity_dir(self.root / ".unity")
        self.paths = formalize_paths(self.base)
        (self.base.unity / "source").mkdir(parents=True)
        self.paper = self.base.unity / "source/paper.md"
        self.paper.write_text("Supplied theorem and proof.\n")
        self.base.unity_md.write_text("# Scope\nFill the selected gaps.\n")
        self.existing = self.root / "Existing.lean"
        self.existing.write_text("theorem target : True := by sorry\naxiom unrelated : True\n")
        self.head = "a" * 40
        self.roster = SimpleNamespace(primary=SimpleNamespace(name="Ada"))
        self.roster.agents = [self.roster.primary]
        self.mocks = {}
        replacements = {
            "load_paths": {"return_value": self.base},
            "load_roster": {"return_value": self.roster},
            "stop_requested": {"return_value": False},
            "build_formalize_mcp": {"return_value": {}},
            "load_prompt": {"return_value": "Offline Formalize prompt"},
            "recover_interrupted_formal_merges": {},
            "persist_report": {"return_value": {}},
            "mark_done": {},
            "_prepare_critic_snapshot": {},
            "_chunk_source": {"new_callable": AsyncMock, "side_effect": self.chunk},
            "run_formalizing_runtime": {"new_callable": AsyncMock, "side_effect": self.finish},
            "dispatch": {"new_callable": AsyncMock, "side_effect": AssertionError("provider dispatch forbidden")},
        }
        for name, options in replacements.items():
            self.add_patch(command, name, **options)
        for module, name, options in (
            (formalize_project, "capture_baseline", {"side_effect": self.capture}),
            (formalize_project, "_require_clean", {}),
            (formalize_project, "require_original_branch", {}),
            (formalize_project, "require_pinned_inputs", {}),
            (formalize_project, "validate_baseline", {"return_value": []}),
            (command.formalize_contract, "_project_context_issues", {"return_value": []}),
            (command.worktree, "main_commit", {"return_value": self.head}),
            (command.formalize_jobs, "terminate", {"return_value": 0}),
            (command.formalize_contract, "build_sources", {"return_value": {"returncode": 0, "output": "ok"}}),
        ):
            self.add_patch(module, name, **options)
        for target in ("unity.Architect.architect", "unity.lake.cache_get", "unity.lake.new_project",
                       "unity.lake.ensure_initial_commit", "subprocess.run", "subprocess.Popen"):
            patcher = patch(target, side_effect=AssertionError("unexpected external/bootstrap call: " + target))
            self.mocks[target] = patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict("os.environ", {"MAX_ATTEMPTS": "1", "RETROSPECTIVE": "false"})
        environment.start()
        self.addCleanup(environment.stop)

    def add_patch(self, module, name, **options):
        patcher = patch.object(module, name, **options)
        self.mocks[name] = patcher.start()
        self.addCleanup(patcher.stop)

    def baseline(self, target_scope="target"):
        records = {}
        for name, kind in (("target", "theorem"), ("unrelated", "axiom")):
            meaning = {"name": name, "module": "Existing", "kind": kind,
                       "type": ["const", "True"], "level_params": []}
            records[name] = {"name": name, "module": "Existing", "target_kind": kind,
                             "type": meaning["type"], "level_params": [], "declaration_meaning": meaning,
                             "is_internal_detail": False, "direct_dependencies": [],
                             "proof_body": ["const", "sorryAx"] if kind == "theorem" else None}
        return formalize_project._seal({
            "version": 1, "project_root": str(self.root), "branch": "main", "head": self.head,
            "files": {}, "tracked_files": ["Existing.lean"], "environment": {},
            "layout": {"modules": {"Existing.lean": "Existing"}, "build_dir": ".lake/build"},
            "declarations": records, "target_scope": target_scope.strip(),
            "scope": {"mode": "explicit", "bound": True, "existing_targets": ["target"]},
            "project_axioms": ["unrelated"],
            "project_sorries": ["target"], "project_used_axioms": ["sorryAx", "unrelated"],
        })

    def capture(self, root, target_scope="All"):
        self.assertEqual(root, self.root)
        return self.baseline(target_scope)

    async def chunk(self, _roster, paths, _limit):
        formalize_state.set_phase(paths.forum, "formalizing")

    async def finish(self, _roster, paths, _mcp, _prompt):
        formalize_state.set_phase(paths.forum, "complete")
        return formalize_state.load_state(paths.forum)

    def bind(self, *, target_scope="target", baseline=True):
        source = snapshot_sources(self.paths)
        state = formalize_state.initialize_source(
            self.paths.forum, hashlib.sha256(scope_bytes(self.paths)).hexdigest(), self.head,
            source, reset=True, project_baseline=self.baseline(target_scope) if baseline else None)
        formalize_state.set_phase(self.paths.forum, "formalizing")
        return state

    async def invoke(self, *args):
        return await CliRunner().invoke(command.command, list(args))

    async def test_fresh_targets_flow_into_preserved_project_baseline_and_own_runtime(self):
        original = self.existing.read_bytes()
        result = await self.invoke("--targets", "target, source Lemma 2")
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["capture_baseline"].assert_called_once_with(self.root, target_scope="target, source Lemma 2")
        state = formalize_state.load_state(self.paths.forum)
        self.assertEqual(state["project_baseline"]["target_scope"], "target, source Lemma 2")
        self.assertEqual(state["pipeline"], "formalize")
        self.assertEqual(self.existing.read_bytes(), original)
        self.mocks["_chunk_source"].assert_awaited_once()
        self.mocks["run_formalizing_runtime"].assert_awaited_once()
        self.mocks["persist_report"].assert_called_once_with(self.paths, accepted=True)
        self.mocks["build_sources"].assert_not_called()  # mocked baseline capture owns the initial build

    async def test_fresh_default_targets_remain_all(self):
        result = await self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["capture_baseline"].assert_called_once_with(self.root, target_scope="All")

    async def test_continue_without_targets_reuses_original_scope(self):
        original = self.bind(target_scope="target")
        result = await self.invoke("--continue")
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["capture_baseline"].assert_not_called()
        self.mocks["build_sources"].assert_called_once_with(self.root, full=True, task_id="resume-preflight")
        self.mocks["require_original_branch"].assert_called_once_with(self.root, original["project_baseline"])
        self.mocks["require_pinned_inputs"].assert_called_once_with(
            self.root, original["project_baseline"], allowed_new_paths=set())
        state = formalize_state.load_state(self.paths.forum)
        self.assertEqual(state["run_id"], original["run_id"])
        self.assertEqual(state["project_baseline"], original["project_baseline"])
        self.mocks["_chunk_source"].assert_not_awaited()

    async def test_continue_accepts_same_explicit_targets(self):
        self.bind()
        result = await self.invoke("--continue", "--targets", " target ")
        self.assertEqual(result.exit_code, 0, result.output)

    async def test_continue_rejects_changed_targets_before_any_execution(self):
        self.bind()
        saved = formalize_state.state_path(self.paths.forum).read_bytes()
        result = await self.invoke("--continue", "--targets", "unrelated")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("cannot change", result.output)
        self.assertEqual(formalize_state.state_path(self.paths.forum).read_bytes(), saved)
        self.mocks["load_roster"].assert_not_called()
        self.mocks["terminate"].assert_not_called()
        self.mocks["run_formalizing_runtime"].assert_not_awaited()

    async def test_history_refuses_fresh_run_without_overwriting_markers_or_evidence(self):
        self.bind()
        marker = self.base.unity / "state.json"
        marker.write_text(json.dumps({"command": "formalize", "phase": "formalizing"}))
        state_path = formalize_state.state_path(self.paths.forum)
        before = {state_path: state_path.read_bytes(), marker: marker.read_bytes(),
                  self.existing: self.existing.read_bytes(), self.paper: self.paper.read_bytes()}
        result = await self.invoke()
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("history already exists", result.output)
        for path, payload in before.items():
            self.assertTrue(path.exists(), str(path))
            self.assertEqual(path.read_bytes(), payload)
        self.mocks["capture_baseline"].assert_not_called()
        self.mocks["load_roster"].assert_not_called()

    async def test_legacy_run_marker_cannot_resume_without_new_state(self):
        marker = self.base.unity / "state.json"
        marker.write_text(json.dumps({"command": "formalize", "phase": "semiformalization"}))
        original = marker.read_bytes()
        result = await self.invoke("--continue")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("legacy", result.output)
        self.assertEqual(marker.read_bytes(), original)
        self.mocks["capture_baseline"].assert_not_called()

    async def test_corrupt_history_refuses_fresh_run_and_preserves_marker(self):
        self.paths.forum.mkdir(parents=True)
        state_path = formalize_state.state_path(self.paths.forum)
        marker = self.base.unity / "state.json"
        marker.write_text(json.dumps({"command": "formalize", "phase": "formalizing"}))
        marker_bytes = marker.read_bytes()
        for payload in (b'{"run_id": "unfinished', b'{"legacy": "retained evidence"}'):
            with self.subTest(payload=payload):
                state_path.write_bytes(payload)
                result = await self.invoke()
                self.assertNotEqual(result.exit_code, 0)
                self.assertIn("history was preserved", result.output)
                self.assertIn("separate project copy", result.output)
                self.assertEqual(state_path.read_bytes(), payload)
                self.assertEqual(marker.read_bytes(), marker_bytes)
        self.mocks["capture_baseline"].assert_not_called()
        self.mocks["build_sources"].assert_not_called()
        self.mocks["load_roster"].assert_not_called()

    async def test_saved_run_without_baseline_is_rejected_without_rewriting_state(self):
        self.bind(baseline=False)
        path = formalize_state.state_path(self.paths.forum)
        original = path.read_bytes()
        result = await self.invoke("--continue")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no valid existing-project baseline", result.output)
        self.assertEqual(path.read_bytes(), original)
        self.mocks["capture_baseline"].assert_not_called()
        self.mocks["run_formalizing_runtime"].assert_not_awaited()

    async def test_frozen_source_edits_reject_before_build_or_dispatch(self):
        self.bind()
        self.paper.write_text("Different theorem.\n")
        result = await self.invoke("--continue")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("sources or immutable UNITY.md scope changed", result.output)
        self.assertIn("separate project copy", result.output)
        self.mocks["build_sources"].assert_not_called()
        self.mocks["load_roster"].assert_not_called()

    async def test_mutable_state_notes_allow_resume_but_instructions_do_not(self):
        self.base.unity_md.write_text("# Scope\nKeep the theorem.\n## State\nOld progress.\n## Rules\nPreserve interfaces.\n")
        self.bind()
        self.base.unity_md.write_text("# Scope\nKeep the theorem.\n## State\nNew progress.\n## Rules\nPreserve interfaces.\n")
        result = await self.invoke("--continue")
        self.assertEqual(result.exit_code, 0, result.output)
        self.base.unity_md.write_text("# Scope\nChange the theorem.\n## State\nNew progress.\n## Rules\nPreserve interfaces.\n")
        result = await self.invoke("--continue")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("sources or immutable UNITY.md scope changed", result.output)
        self.assertIn("separate project copy", result.output)

    async def test_pinned_input_failure_blocks_build_and_preserves_saved_state(self):
        self.bind()
        state_path = formalize_state.state_path(self.paths.forum)
        before = state_path.read_bytes()
        marker = self.base.unity / "state.json"
        marker.write_text(json.dumps({"command": "formalize", "phase": "formalizing"}))
        marker_bytes = marker.read_bytes()
        stop_flag = self.base.unity / "stop-requested"
        stop_flag.write_text("keep prior stop on refusal")
        self.mocks["require_pinned_inputs"].side_effect = ValueError("pinned project inputs changed")
        result = await self.invoke("--continue")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("pinned project inputs changed", result.output)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(marker.read_bytes(), marker_bytes)
        self.assertEqual(stop_flag.read_text(), "keep prior stop on refusal")
        self.mocks["build_sources"].assert_not_called()
        self.mocks["_chunk_source"].assert_not_awaited()
        self.mocks["run_formalizing_runtime"].assert_not_awaited()

    async def test_fresh_preflight_stop_survives_and_no_worker_launches(self):
        stop_flag = self.base.unity / "stop-requested"
        stop_flag.write_text("stale request")
        self.mocks["stop_requested"].side_effect = lambda root: stop_flag.exists()

        def capture_then_stop(root, target_scope="All"):
            self.assertFalse(stop_flag.exists(), "stale stop must clear before the owned preflight")
            baseline = self.capture(root, target_scope)
            stop_flag.write_text("new stop during baseline capture")
            return baseline

        self.mocks["capture_baseline"].side_effect = capture_then_stop
        result = await self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("stopped safely", result.output)
        self.assertEqual(stop_flag.read_text(), "new stop during baseline capture")
        self.mocks["_chunk_source"].assert_not_awaited()
        self.mocks["run_formalizing_runtime"].assert_not_awaited()
        self.mocks["dispatch"].assert_not_awaited()
        self.mocks["mark_done"].assert_not_called()
        self.mocks["persist_report"].assert_called_once_with(self.paths, accepted=False)

    async def test_continue_preflight_stop_survives_and_no_worker_launches(self):
        self.bind()
        stop_flag = self.base.unity / "stop-requested"
        stop_flag.write_text("stale request")
        self.mocks["stop_requested"].side_effect = lambda root: stop_flag.exists()

        def build_then_stop(root, **kwargs):
            self.assertFalse(stop_flag.exists(), "stale stop must clear before the owned preflight")
            stop_flag.write_text("new stop during resume preflight")
            return {"returncode": 0, "output": "build completed"}

        self.mocks["build_sources"].side_effect = build_then_stop
        result = await self.invoke("--continue")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("stopped safely", result.output)
        self.assertEqual(stop_flag.read_text(), "new stop during resume preflight")
        self.mocks["_chunk_source"].assert_not_awaited()
        self.mocks["run_formalizing_runtime"].assert_not_awaited()
        self.mocks["dispatch"].assert_not_awaited()
        self.mocks["mark_done"].assert_not_called()
        self.mocks["persist_report"].assert_called_once_with(self.paths, accepted=False)

    async def test_retrospective_dispatch_uses_private_role_view_not_main(self):
        original = self.bind()
        view = self.root / "private-retrospective-view"
        view.mkdir()
        library = self.root / "test-library"
        library.mkdir()
        report_path = self.paths.forum / "retrospective.json"

        async def write_outcome(*args, **kwargs):
            self.assertEqual(args[4], view)
            self.assertNotEqual(args[4], self.root)
            report_path.write_text(json.dumps({"run_id": original["run_id"], "status": "no_changes",
                                               "reason": "No justified reusable lesson."}))
            return [None]

        self.mocks["dispatch"].side_effect = write_outcome
        with patch.object(command.worktree, "role_view", return_value=view) as role_view, \
             patch.object(command.library, "ensure_library", return_value=library), \
             patch.object(command, "configure_forum"):
            result = await command._run_retrospective(self.roster, self.paths)
        role_view.assert_called_once_with(self.root, "retrospective")
        self.assertEqual(result["status"], "no_changes")
        self.mocks["dispatch"].assert_awaited_once()

    async def test_failed_baseline_capture_never_runs_chunker_or_rewrites_lean(self):
        original = self.existing.read_bytes()
        self.mocks["capture_baseline"].side_effect = ValueError("existing project must build")
        result = await self.invoke()
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("existing project must build", result.output)
        self.assertEqual(self.existing.read_bytes(), original)
        self.mocks["_chunk_source"].assert_not_awaited()
        self.mocks["persist_report"].assert_called_once_with(self.paths, accepted=False)

    async def test_help_retains_targets_option(self):
        result = await self.invoke("--help")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("--targets", result.output)
        self.assertIn("--continue", result.output)
        self.mocks["load_paths"].assert_not_called()

    def test_prepare_only_builds_and_reaps_owned_jobs(self):
        command._prepare_formalize_environment(self.root)
        self.mocks["build_sources"].assert_called_once_with(self.root, full=True, task_id="resume-preflight")
        self.mocks["terminate"].assert_called_once_with(self.root)
        self.mocks["build_sources"].reset_mock()
        command._prepare_formalize_environment(self.root, validate_project=False)
        self.mocks["build_sources"].assert_not_called()

    def test_prepare_rejects_failed_owned_build(self):
        self.mocks["build_sources"].return_value = {"returncode": 1, "output": "specific build failure"}
        with self.assertRaisesRegex(ValueError, "existing project failed validation: specific build failure"):
            command._prepare_formalize_environment(self.root)


if __name__ == "__main__":
    unittest.main()
