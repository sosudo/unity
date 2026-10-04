"""Bump dashboard routing, using temporary files and mocked job control only."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from unity import bump_jobs, bump_state
from unity.forum import autoformalize_server, bump_server, web


class BumpWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.unity = self.root / ".unity"
        self.forum = self.unity / "forum"
        self.forum.mkdir(parents=True)
        (self.unity / "logs").mkdir()
        (self.unity / "agents.yaml").write_text(
            "agents:\n  - name: Ada\n    model: test\n    backend: codex\n"
            "    primary: true\n  - name: Bert\n    model: test\n    backend: codex\n"
        )
        for name, value in (("ROOT_DIR", self.unity), ("FORUM_DIR", self.forum)):
            patcher = patch.object(web, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(web.app)
        self.set_command("bump")

    def set_command(self, command, phase="formalizing"):
        (self.unity / "state.json").write_text(json.dumps({"command": command, "phase": phase}))

    def prepare_target(self):
        run_id = "bump-123456abcdef"
        target = self.root.resolve() / ".unity" / "bump" / run_id / "target"
        unity = target / ".unity"
        (unity / "forum" / "bump").mkdir(parents=True)
        (unity / "logs").mkdir()
        origin = {"version": 1, "run_id": run_id, "project_root": str(self.root.resolve()),
                  "source_root": str(self.root.resolve()), "target_path": str(target), "status": "ready"}
        (self.unity / "bump" / "active.json").write_text(json.dumps(origin))
        (unity / "bump-origin.json").write_text(json.dumps(origin))
        (unity / "state.json").write_text(json.dumps({"command": "bump", "phase": "formalizing"}))
        return unity

    def test_source_dashboard_follows_only_its_bound_target_forum(self):
        unity = self.prepare_target()
        own = unity / "forum" / "bump"
        (own / "discussion.json").write_text(json.dumps({"title": "target discussion", "posts": []}))
        (own / "dag.json").write_text(json.dumps({"chunks": [{"id": "repair-decl"}]}))
        with patch.object(bump_state, "load_state", return_value={"run_id": "target-runtime"}) as load:
            self.assertEqual(self.client.get("/api/bump-state").json()["run_id"], "target-runtime")
        load.assert_called_once_with(own)
        self.assertEqual(web._discussion_forum(), own)
        self.assertEqual(web._load_thread("discussion")["title"], "target discussion")
        self.assertEqual(self.client.get("/api/project").json()["chunks"], ["repair-decl"])
        with patch.object(bump_server, "read_metrics", return_value={}) as read:
            self.client.get("/api/bump-metrics")
        read.assert_called_once_with(own, unity.parent)

    def test_bound_target_artifacts_and_stop_jobs_use_target_root(self):
        unity = self.prepare_target()
        with patch.object(web.artifact_store, "list_artifacts", return_value=[]) as listing, \
             patch.object(web.artifact_store, "artifact_stats", return_value={}):
            self.client.get("/api/artifacts")
        listing.assert_called_once_with(unity / "artifacts", limit=200)
        with patch.object(web, "_current_run", return_value={"running": True, "command": "bump"}), \
             patch.object(bump_jobs, "terminate", return_value=0) as terminate:
            self.assertTrue(web.api_run_stop({"mode": "safe"})["stopping"])
        terminate.assert_called_once_with(unity.parent)
        self.assertTrue((unity / "stop-requested").exists())

    def test_pointer_mismatch_or_origin_mismatch_never_reads_foreign_state(self):
        unity = self.prepare_target()
        pointer = self.unity / "bump" / "active.json"
        original = json.loads(pointer.read_text())
        for key, value in (("project_root", "/elsewhere"), ("target_path", "/elsewhere/target"),
                           ("run_id", "../foreign")):
            with self.subTest(field=key):
                pointer.write_text(json.dumps({**original, key: value}))
                with patch.object(bump_state, "load_state", side_effect=AssertionError("foreign read")):
                    with self.assertRaises(ValueError):
                        web.get_bump_state()
        pointer.write_text(json.dumps(original))
        (unity / "bump-origin.json").write_text(json.dumps({**original, "run_id": "bump-aaaaaaaaaaaa"}))
        with self.assertRaises(ValueError):
            web.get_bump_state()

    def test_saved_bump_pointer_does_not_take_over_other_workflow(self):
        self.prepare_target()
        self.set_command("formalize")
        self.assertEqual(web._discussion_forum(), self.forum / "formalize")
        self.assertEqual(web._workspace_unity(), self.unity)
        self.assertEqual(web.get_bump_state(), {})
        self.assertTrue(web._bump_history_exists())

    def test_bump_state_route_uses_own_state(self):
        expected = {"run_id": "bump-test", "revision": 3, "phase": "formalizing"}
        with patch.object(bump_state, "load_state", return_value=expected) as load, \
             patch.object(web, "_load_solve_state", side_effect=AssertionError("wrong workflow")):
            response = self.client.get("/api/bump-state")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        load.assert_called_once_with(self.forum / "bump")

    def test_uninitialized_bump_state_is_safe(self):
        response = self.client.get("/api/bump-state")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pipeline"], "bump")
        self.assertEqual(response.json()["revision"], 0)
        self.assertEqual(response.json()["formal_tasks"], {})

    def test_inactive_endpoints_do_not_touch_bump_state_or_server(self):
        for command in ("autoformalize", "solve", "prove", "formalize"):
            with self.subTest(command=command):
                self.set_command(command)
                with patch.object(bump_state, "load_state", side_effect=AssertionError("inactive state")), \
                     patch.object(bump_server, "read_metrics", side_effect=AssertionError("inactive metrics")):
                    self.assertEqual(self.client.get("/api/bump-state").json(), {})
                    self.assertEqual(self.client.get("/api/bump-metrics").json(), {})

    def test_metrics_are_read_without_server_reconfiguration(self):
        before = (bump_server.FORUM_DIR, bump_server.PROJECT_ROOT, bump_server.PROFILE,
                  autoformalize_server.FORUM_DIR, autoformalize_server.PROJECT_ROOT,
                  autoformalize_server.PROFILE)
        with patch.object(bump_server, "read_metrics", return_value={"worker_turns": 8}) as read, \
             patch.object(bump_server, "configure", side_effect=AssertionError("global mutation")):
            self.assertEqual(self.client.get("/api/bump-metrics").json(), {"worker_turns": 8})
        read.assert_called_once_with(self.forum / "bump", self.root)
        after = (bump_server.FORUM_DIR, bump_server.PROJECT_ROOT, bump_server.PROFILE,
                 autoformalize_server.FORUM_DIR, autoformalize_server.PROJECT_ROOT,
                 autoformalize_server.PROFILE)
        self.assertEqual(before, after)

    def test_discussions_and_icrl_stay_command_scoped(self):
        for command, directory in (("bump", self.forum / "bump"),
                                   ("formalize", self.forum / "formalize"),
                                   ("autoformalize", self.forum / "autoformalize"),
                                   ("solve", self.forum), ("prove", self.forum)):
            with self.subTest(command=command):
                self.set_command(command)
                directory.mkdir(exist_ok=True)
                (directory / "scope.json").write_text(json.dumps({"title": command, "posts": []}))
                self.assertEqual(web._discussion_forum(), directory)
                self.assertEqual(web._load_thread("scope")["title"], command)
                self.assertFalse(web._icrl_visible())
        self.set_command("create")
        self.assertTrue(web._icrl_visible())

    def test_bump_dag_keeps_evidence_and_final_graph(self):
        tasks = {
            "target": {"task_id": "target", "revision": 2, "status": "complete",
                       "informal_statement": "Supplied theorem", "informal_proof": "Source argument",
                       "statement_dependencies": ["definition"], "proof_dependencies": ["lemma", "definition"],
                       "source_components": ["source-1"], "anchor_ids": ["A1"], "requirement_ids": ["R1"],
                       "outputs": [{"declaration": "Example.target", "file": "Example.lean"}],
                       "verification": {"status": "verified"}, "faithfulness": {"status": "approved"}},
            "retired": {"task_id": "retired", "status": "superseded"},
        }
        state = {"formal_tasks": tasks}
        before = deepcopy(state)
        self.set_command("bump", "done")
        with patch.object(bump_state, "load_state", return_value=state) as load, \
             patch.object(bump_state, "assignment_view", return_value={"status": "unassigned"}), \
             patch.object(web, "_autoformalize_dag", side_effect=AssertionError("wrong DAG")):
            result = self.client.get("/api/dag").json()
        load.assert_called_once_with(self.forum / "bump")
        self.assertEqual(result["graph_kind"], "bump")
        self.assertEqual(len(result["chunks"]), 1)
        target = result["chunks"][0]
        self.assertEqual(target["status"], "green")
        self.assertEqual(target["dependencies"], ["definition", "lemma"])
        self.assertEqual(target["declarations"], ["Example.target"])
        self.assertEqual(target["requirement_ids"], ["R1"])
        self.assertEqual(state, before)

    def test_bump_dag_colors_do_not_treat_build_as_acceptance(self):
        cases = [("complete", "verified", "unreviewed", "missing", "unassigned", "grey"),
                 ("blocked", "verified", "changes_requested", "adopted", "assigned", "red"),
                 ("candidate_pending", "pending", "unreviewed", "missing", "unassigned", "blue"),
                 ("open", "pending", "unreviewed", "missing", "assigned", "yellow")]
        for status, verification, faithfulness, representation, assignment, color in cases:
            with self.subTest(status=status):
                state = {"formal_tasks": {"t": {"task_id": "t", "status": status,
                         "verification": {"status": verification}, "faithfulness": {"status": faithfulness},
                         "representation": {"status": representation}}}}
                with patch.object(bump_state, "load_state", return_value=state), \
                     patch.object(bump_state, "assignment_view", return_value={"status": assignment}):
                    self.assertEqual(web._bump_dag()["chunks"][0]["status"], color)

    def test_autoformalize_dag_route_remains_unchanged(self):
        self.set_command("autoformalize")
        expected = {"graph_kind": "autoformalize", "chunks": []}
        with patch.object(web, "_autoformalize_dag", return_value=expected), \
             patch.object(web, "_bump_dag", side_effect=AssertionError("wrong DAG")):
            self.assertEqual(self.client.get("/api/dag").json(), expected)

    def test_live_claims_are_bump_only_and_include_assistants(self):
        state = {"strategies": {"s": {"owner": "Ada", "status": "claimed", "target": "gap",
                                         "description": "Source argument", "assistants": ["Bert"]}}}
        with patch.object(bump_state, "load_state", return_value=state) as load:
            agents = {a["name"]: a for a in web.get_workspace()["agents"]}
        load.assert_called_once_with(self.forum / "bump")
        self.assertEqual(agents["Ada"]["chunk"], "gap")
        self.assertEqual(agents["Bert"]["activity"], "assisting: Source argument")
        for command, phase in (("prove", "proving"), ("bump", "done")):
            self.set_command(command, phase)
            with patch.object(bump_state, "load_state", side_effect=AssertionError("not active")):
                agents = web.get_workspace()["agents"]
                self.assertTrue(all(not agent["chunk"] for agent in agents))

    def test_safe_stop_reaps_only_bump_registered_jobs(self):
        with patch.object(web, "_current_run", return_value={"running": True, "command": "bump"}), \
             patch.object(bump_jobs, "terminate", return_value=2) as terminate, \
             patch.object(web.os, "killpg", side_effect=AssertionError("safe stop must not kill run group")):
            result = self.client.post("/api/run/stop", json={"mode": "safe"}).json()
        self.assertTrue(result["stopping"])
        self.assertTrue((self.unity / "stop-requested").exists())
        terminate.assert_called_once_with(self.root)
        for command in ("autoformalize", "solve", "prove", "formalize"):
            with self.subTest(command=command), \
                 patch.object(web, "_current_run", return_value={"running": True, "command": command}), \
                 patch.object(bump_jobs, "terminate", side_effect=AssertionError("wrong job registry")):
                self.assertTrue(web.api_run_stop({"mode": "safe"})["stopping"])

    def test_idle_stop_never_touches_registered_jobs(self):
        with patch.object(web, "_current_run", return_value={"running": False, "command": "bump"}), \
             patch.object(bump_jobs, "terminate", side_effect=AssertionError("idle stop")):
            self.assertEqual(web.api_run_stop({"mode": "safe"}), {"ok": True, "stopped": False})
        self.assertFalse((self.unity / "stop-requested").exists())

    def test_target_version_is_retained_by_dry_run(self):
        with patch.object(web, "_current_run", return_value={"running": False}):
            result = self.client.post("/api/run", json={"command": "bump", "version": "v4.34.1",
                                                        "continue": False, "dry": True}).json()
        self.assertEqual(result["argv"], ["bump", "v4.34.1"])

    def test_project_metadata_uses_bump_dag_not_stale_global_graph(self):
        (self.unity / "dag.json").write_text(json.dumps({"chunks": [{"id": "other-workflow"}]}))
        own = self.forum / "bump"
        own.mkdir()
        (own / "dag.json").write_text(json.dumps({"chunks": [{"id": "own-node"}]}))
        response = self.client.get("/api/project").json()
        self.assertTrue(response["has_dag"])
        self.assertEqual(response["chunks"], ["own-node"])
        self.set_command("prove")
        response = self.client.get("/api/project").json()
        self.assertEqual(response["chunks"], ["other-workflow"])

    def test_declaration_link_uses_exact_bump_output_binding(self):
        (self.unity / "dag.json").write_text(json.dumps({"chunks": [
            {"id": "stale", "declarations": ["Example.target"]}]}))
        own = self.forum / "bump"
        own.mkdir()
        (own / "dag.json").write_text(json.dumps({"chunks": [
            {"id": "retired", "status": "superseded", "outputs": [{"declaration": "Example.target"}]},
            {"id": "own-node", "title": "Source statement", "status": "complete",
             "outputs": [{"declaration": "Example.target", "file": "Existing.lean"}]}]}))
        self.assertEqual(web._chunk_for_decl("Example.target"),
                         {"id": "own-node", "title": "Source statement", "status": "complete"})
        self.assertIsNone(web._chunk_for_decl("Other.target"))
        self.assertIsNone(web._chunk_for_decl("Source statement"))
        self.set_command("prove")
        self.assertEqual(web._chunk_for_decl("Example.target")["id"], "stale")

    def test_saved_bump_history_needs_no_posts_to_default_to_continue(self):
        own = self.forum / "bump"
        own.mkdir()
        (own / "bump-state.json").write_text(json.dumps({"run_id": "saved-no-posts"}))
        self.assertTrue(web._forum_nonempty())
        response = self.client.get("/api/project").json()
        self.assertTrue(response["continue"])
        self.assertTrue(response["bump_continue"])
        with patch.object(web, "_current_run", return_value={"running": False}):
            response = self.client.post("/api/run", json={"command": "bump", "dry": True}).json()
        self.assertEqual(response["argv"], ["bump", "--continue"])

    def test_bump_continue_uses_requested_command_even_when_another_was_last(self):
        self.set_command("prove")
        own = self.forum / "bump"
        own.mkdir()
        (own / "bump-state.json").write_text('{"incomplete":')
        self.assertFalse(web._forum_nonempty())
        response = self.client.get("/api/project").json()
        self.assertFalse(response["continue"])
        self.assertTrue(response["bump_continue"])
        with patch.object(web, "_current_run", return_value={"running": False}):
            response = self.client.post("/api/run", json={"command": "bump", "dry": True}).json()
        self.assertEqual(response["argv"], ["bump", "--continue"])

    def test_other_workflow_posts_do_not_trigger_first_bump_continuation(self):
        self.set_command("prove")
        (self.forum / "old.json").write_text(json.dumps({"posts": [{"content": "old run"}]}))
        self.assertTrue(web._forum_nonempty())
        with patch.object(web, "_current_run", return_value={"running": False}):
            response = self.client.post("/api/run", json={"command": "bump", "dry": True}).json()
        self.assertEqual(response["argv"], ["bump"])

    def test_javascript_includes_own_endpoints_overview_and_graph_kind(self):
        self.assertIn("J('/api/bump-state')", web.APP_HTML)
        self.assertIn("J('/api/bump-metrics')", web.APP_HTML)
        self.assertIn("r.command === 'bump'", web.APP_HTML)
        self.assertIn("|| r.command === 'bump'", web.APP_HTML)
        self.assertIn("cmd === 'bump' ? PROJECT.bump_continue", web.APP_HTML)
        self.assertIn("|| graphKind === 'bump'", web.DAG_HTML)
        self.assertIn("Declaration repair DAG", web.DAG_HTML)
        self.assertIn("declaration repair tasks", web.APP_HTML)


if __name__ == "__main__":
    unittest.main()
