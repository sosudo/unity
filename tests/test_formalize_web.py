"""Formalize dashboard routing, using temporary files and mocked job control only."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from unity import formalize_jobs, formalize_state
from unity.forum import autoformalize_server, formalize_server, web


class FormalizeWebTests(unittest.TestCase):
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
        self.set_command("formalize")

    def set_command(self, command, phase="formalizing"):
        (self.unity / "state.json").write_text(json.dumps({"command": command, "phase": phase}))

    def test_formalize_state_route_uses_own_state(self):
        expected = {"run_id": "formalize-test", "revision": 3, "phase": "formalizing"}
        with patch.object(formalize_state, "load_state", return_value=expected) as load, \
             patch.object(web, "_load_solve_state", side_effect=AssertionError("wrong workflow")):
            response = self.client.get("/api/formalize-state")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        load.assert_called_once_with(self.forum / "formalize")

    def test_uninitialized_formalize_state_is_safe(self):
        response = self.client.get("/api/formalize-state")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pipeline"], "formalize")
        self.assertEqual(response.json()["revision"], 0)
        self.assertEqual(response.json()["formal_tasks"], {})

    def test_inactive_endpoints_do_not_touch_formalize_state_or_server(self):
        for command in ("autoformalize", "solve", "prove", "bump"):
            with self.subTest(command=command):
                self.set_command(command)
                with patch.object(formalize_state, "load_state", side_effect=AssertionError("inactive state")), \
                     patch.object(formalize_server, "read_metrics", side_effect=AssertionError("inactive metrics")):
                    self.assertEqual(self.client.get("/api/formalize-state").json(), {})
                    self.assertEqual(self.client.get("/api/formalize-metrics").json(), {})

    def test_metrics_are_read_without_server_reconfiguration(self):
        before = (formalize_server.FORUM_DIR, formalize_server.PROJECT_ROOT, formalize_server.PROFILE,
                  autoformalize_server.FORUM_DIR, autoformalize_server.PROJECT_ROOT,
                  autoformalize_server.PROFILE)
        with patch.object(formalize_server, "read_metrics", return_value={"worker_turns": 8}) as read, \
             patch.object(formalize_server, "configure", side_effect=AssertionError("global mutation")):
            self.assertEqual(self.client.get("/api/formalize-metrics").json(), {"worker_turns": 8})
        read.assert_called_once_with(self.forum / "formalize", self.root)
        after = (formalize_server.FORUM_DIR, formalize_server.PROJECT_ROOT, formalize_server.PROFILE,
                 autoformalize_server.FORUM_DIR, autoformalize_server.PROJECT_ROOT,
                 autoformalize_server.PROFILE)
        self.assertEqual(before, after)

    def test_discussions_and_icrl_stay_command_scoped(self):
        for command, directory in (("formalize", self.forum / "formalize"),
                                   ("autoformalize", self.forum / "autoformalize"),
                                   ("solve", self.forum), ("prove", self.forum)):
            with self.subTest(command=command):
                self.set_command(command)
                directory.mkdir(exist_ok=True)
                (directory / "scope.json").write_text(json.dumps({"title": command, "posts": []}))
                self.assertEqual(web._discussion_forum(), directory)
                self.assertEqual(web._load_thread("scope")["title"], command)
                self.assertFalse(web._icrl_visible())
        self.set_command("bump")
        self.assertTrue(web._icrl_visible())

    def test_formalize_dag_keeps_evidence_and_final_graph(self):
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
        self.set_command("formalize", "done")
        with patch.object(formalize_state, "load_state", return_value=state) as load, \
             patch.object(formalize_state, "assignment_view", return_value={"status": "unassigned"}), \
             patch.object(web, "_autoformalize_dag", side_effect=AssertionError("wrong DAG")):
            result = self.client.get("/api/dag").json()
        load.assert_called_once_with(self.forum / "formalize")
        self.assertEqual(result["graph_kind"], "formalize")
        self.assertEqual(len(result["chunks"]), 1)
        target = result["chunks"][0]
        self.assertEqual(target["status"], "green")
        self.assertEqual(target["dependencies"], ["definition", "lemma"])
        self.assertEqual(target["declarations"], ["Example.target"])
        self.assertEqual(target["requirement_ids"], ["R1"])
        self.assertEqual(state, before)

    def test_formalize_dag_colors_do_not_treat_build_as_acceptance(self):
        cases = [("complete", "verified", "unreviewed", "missing", "unassigned", "grey"),
                 ("blocked", "verified", "changes_requested", "adopted", "assigned", "red"),
                 ("candidate_pending", "pending", "unreviewed", "missing", "unassigned", "blue"),
                 ("open", "pending", "unreviewed", "missing", "assigned", "yellow")]
        for status, verification, faithfulness, representation, assignment, color in cases:
            with self.subTest(status=status):
                state = {"formal_tasks": {"t": {"task_id": "t", "status": status,
                         "verification": {"status": verification}, "faithfulness": {"status": faithfulness},
                         "representation": {"status": representation}}}}
                with patch.object(formalize_state, "load_state", return_value=state), \
                     patch.object(formalize_state, "assignment_view", return_value={"status": assignment}):
                    self.assertEqual(web._formalize_dag()["chunks"][0]["status"], color)

    def test_autoformalize_dag_route_remains_unchanged(self):
        self.set_command("autoformalize")
        expected = {"graph_kind": "autoformalize", "chunks": []}
        with patch.object(web, "_autoformalize_dag", return_value=expected), \
             patch.object(web, "_formalize_dag", side_effect=AssertionError("wrong DAG")):
            self.assertEqual(self.client.get("/api/dag").json(), expected)

    def test_live_claims_are_formalize_only_and_include_assistants(self):
        state = {"strategies": {"s": {"owner": "Ada", "status": "claimed", "target": "gap",
                                         "description": "Source argument", "assistants": ["Bert"]}}}
        with patch.object(formalize_state, "load_state", return_value=state) as load:
            agents = {a["name"]: a for a in web.get_workspace()["agents"]}
        load.assert_called_once_with(self.forum / "formalize")
        self.assertEqual(agents["Ada"]["chunk"], "gap")
        self.assertEqual(agents["Bert"]["activity"], "assisting: Source argument")
        for command, phase in (("prove", "proving"), ("formalize", "done")):
            self.set_command(command, phase)
            with patch.object(formalize_state, "load_state", side_effect=AssertionError("not active")):
                agents = web.get_workspace()["agents"]
                self.assertTrue(all(not agent["chunk"] for agent in agents))

    def test_safe_stop_reaps_only_formalize_registered_jobs(self):
        with patch.object(web, "_current_run", return_value={"running": True, "command": "formalize"}), \
             patch.object(formalize_jobs, "terminate", return_value=2) as terminate, \
             patch.object(web.os, "killpg", side_effect=AssertionError("safe stop must not kill run group")):
            result = self.client.post("/api/run/stop", json={"mode": "safe"}).json()
        self.assertTrue(result["stopping"])
        self.assertTrue((self.unity / "stop-requested").exists())
        terminate.assert_called_once_with(self.root)
        for command in ("autoformalize", "solve", "prove"):
            with self.subTest(command=command), \
                 patch.object(web, "_current_run", return_value={"running": True, "command": command}), \
                 patch.object(formalize_jobs, "terminate", side_effect=AssertionError("wrong job registry")):
                self.assertTrue(web.api_run_stop({"mode": "safe"})["stopping"])

    def test_idle_stop_never_touches_registered_jobs(self):
        with patch.object(web, "_current_run", return_value={"running": False, "command": "formalize"}), \
             patch.object(formalize_jobs, "terminate", side_effect=AssertionError("idle stop")):
            self.assertEqual(web.api_run_stop({"mode": "safe"}), {"ok": True, "stopped": False})
        self.assertFalse((self.unity / "stop-requested").exists())

    def test_targets_are_retained_by_dry_run(self):
        with patch.object(web, "_current_run", return_value={"running": False}):
            result = self.client.post("/api/run", json={"command": "formalize", "targets": "A.target\nB.target",
                                                        "continue": False, "dry": True}).json()
        self.assertEqual(result["argv"], ["formalize", "--targets", "A.target, B.target"])

    def test_project_metadata_uses_formalize_dag_not_stale_global_graph(self):
        (self.unity / "dag.json").write_text(json.dumps({"chunks": [{"id": "other-workflow"}]}))
        own = self.forum / "formalize"
        own.mkdir()
        (own / "dag.json").write_text(json.dumps({"chunks": [{"id": "own-node"}]}))
        response = self.client.get("/api/project").json()
        self.assertTrue(response["has_dag"])
        self.assertEqual(response["chunks"], ["own-node"])
        self.set_command("prove")
        response = self.client.get("/api/project").json()
        self.assertEqual(response["chunks"], ["other-workflow"])

    def test_declaration_link_uses_exact_formalize_output_binding(self):
        (self.unity / "dag.json").write_text(json.dumps({"chunks": [
            {"id": "stale", "declarations": ["Example.target"]}]}))
        own = self.forum / "formalize"
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

    def test_saved_formalize_history_needs_no_posts_to_default_to_continue(self):
        own = self.forum / "formalize"
        own.mkdir()
        (own / "formalize-state.json").write_text(json.dumps({"run_id": "saved-no-posts"}))
        self.assertTrue(web._forum_nonempty())
        response = self.client.get("/api/project").json()
        self.assertTrue(response["continue"])
        self.assertTrue(response["formalize_continue"])
        with patch.object(web, "_current_run", return_value={"running": False}):
            response = self.client.post("/api/run", json={"command": "formalize", "dry": True}).json()
        self.assertEqual(response["argv"], ["formalize", "--continue"])

    def test_formalize_continue_uses_requested_command_even_when_another_was_last(self):
        self.set_command("prove")
        own = self.forum / "formalize"
        own.mkdir()
        (own / "formalize-state.json").write_text('{"incomplete":')
        self.assertFalse(web._forum_nonempty())
        response = self.client.get("/api/project").json()
        self.assertFalse(response["continue"])
        self.assertTrue(response["formalize_continue"])
        with patch.object(web, "_current_run", return_value={"running": False}):
            response = self.client.post("/api/run", json={"command": "formalize", "dry": True}).json()
        self.assertEqual(response["argv"], ["formalize", "--continue"])

    def test_other_workflow_posts_do_not_trigger_first_formalize_continuation(self):
        self.set_command("prove")
        (self.forum / "old.json").write_text(json.dumps({"posts": [{"content": "old run"}]}))
        self.assertTrue(web._forum_nonempty())
        with patch.object(web, "_current_run", return_value={"running": False}):
            response = self.client.post("/api/run", json={"command": "formalize", "dry": True}).json()
        self.assertEqual(response["argv"], ["formalize"])

    def test_javascript_includes_own_endpoints_overview_and_graph_kind(self):
        self.assertIn("J('/api/formalize-state')", web.APP_HTML)
        self.assertIn("J('/api/formalize-metrics')", web.APP_HTML)
        self.assertIn("r.command === 'formalize'", web.APP_HTML)
        self.assertIn("['formalize', 'autoformalize'].includes(r.command)", web.APP_HTML)
        self.assertIn("cmd === 'formalize' ? PROJECT.formalize_continue", web.APP_HTML)
        self.assertIn("['formalize', 'autoformalize', 'solve_formalization'].includes(graphKind)", web.DAG_HTML)


if __name__ == "__main__":
    unittest.main()
