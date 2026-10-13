"""Read-only compiler Bump dashboard and native stop routing, entirely offline."""
from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from unity import bump_jobs, bump_state, bump_tasks
from unity.forum import bump_server, web


class CompilerBumpWebTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.unity = self.root / ".unity"
        (self.unity / "forum").mkdir(parents=True)
        (self.unity / "logs").mkdir()
        (self.unity / "agents.yaml").write_text(
            "agents:\n  - name: Alice\n    model: offline\n    backend: codex\n"
            "    primary: true\n  - name: Bob\n    model: offline\n    backend: codex\n")
        for key, value in (("ROOT_DIR", self.unity), ("FORUM_DIR", self.unity / "forum")):
            p = patch.object(web, key, value)
            p.start()
            self.addCleanup(p.stop)
        self.set_command("bump")
        run_id = "bump-123456abcdef"
        self.target = self.unity / "bump" / run_id / "target"
        self.target_unity = self.target / ".unity"
        self.forum = self.target_unity / "forum" / "bump"
        self.forum.mkdir(parents=True)
        identity = {"schema": bump_tasks.SCHEMA, "run_id": run_id,
                    "source_root": str(self.root), "project_root": str(self.root), "target_path": str(self.target)}
        (self.unity / "bump" / "active.json").write_text(json.dumps(identity))
        (self.target_unity / "bump-origin.json").write_text(json.dumps(identity))
        (self.target_unity / "state.json").write_text(json.dumps({"command": "bump", "phase": "repairing"}))
        bump_tasks.initialize(self.forum, self.target, run_id=run_id, base_sha="base", version="v4.34.1")
        (self.target / "A.lean").write_text("namespace A\ndef first := missing\ndef second := 1\n#exit\ndef draft := missing\n")
        (self.target / "Old.lean").write_text("def archived := missing\n")
        inventory = bump_tasks.scan_sources(self.target, ["A.lean", "Old.lean"])
        for chunk in inventory["chunks"].values():
            if chunk["path"] == "Old.lean":
                chunk.update(scope_excluded=True, status="excluded")
        state = bump_tasks.refresh(self.forum, inventory,
            [{"path": "A.lean", "line": 2, "severity": "error", "message": "unknown identifier"}],
            main_sha="base", config_revision=0)
        self.tid = next(iter(state["tasks"]))
        self.client = TestClient(web.app)

    def set_command(self, command):
        (self.unity / "state.json").write_text(json.dumps({"command": command, "phase": "repairing"}))

    def test_state_and_metrics_read_new_target_without_legacy_or_writes(self):
        before = (self.forum / bump_tasks.STATE_FILE).read_bytes()
        with patch.object(bump_state, "load_state", side_effect=AssertionError("legacy state")), \
             patch.object(bump_server, "read_metrics", side_effect=AssertionError("legacy metrics")), \
             patch.object(bump_tasks, "transaction", side_effect=AssertionError("dashboard mutation")):
            state = self.client.get("/api/bump-state").json()
            metrics = self.client.get("/api/bump-metrics").json()
        self.assertEqual(state["schema"], bump_tasks.SCHEMA)
        self.assertEqual(metrics["tasks"], {"pending": 1})
        self.assertEqual(metrics["chunks"]["excluded"], 1)
        self.assertEqual(metrics["chunks"]["skipped"], 1)
        self.assertEqual(metrics["acceptance"], "provisional")
        self.assertEqual((self.forum / bump_tasks.STATE_FILE).read_bytes(), before)

    def test_dag_has_all_declarations_and_non_declaration_prerequisites(self):
        with bump_tasks.transaction(self.forum) as state:
            state["tasks"]["config"] = {"task_id": "config", "kind": "configuration", "chunk_ids": [],
                "files": ["lakefile.toml"], "dependencies": [], "status": "complete", "attempts": 1}
            state["tasks"][self.tid]["dependencies"] = ["config"]
            clean = next(c for c in state["chunks"].values() if c["name"] == "A.second")
            clean["status"] = "clean"
        before = bump_tasks.load(self.forum)
        dag = self.client.get("/api/dag").json()
        self.assertEqual(dag["schema"], bump_tasks.SCHEMA)
        self.assertEqual(len(dag["chunks"]), len(before["chunks"]) + 1)
        node = next(c for c in dag["chunks"] if c["title"] == "A.first")
        self.assertIn("assignment:config", node["dependencies"])
        clean = next(c for c in dag["chunks"] if c["title"] == "A.second")
        self.assertEqual(clean["status"], "blue")
        self.assertEqual(clean["acceptance"], "provisional")
        excluded = next(c for c in dag["chunks"] if c["title"] == "archived")
        self.assertEqual(excluded["acceptance"], "outside_scope")
        self.assertNotIn("faithfulness", node)
        self.assertEqual(bump_tasks.load(self.forum), before)

    def test_review_counter_requires_same_main_config_and_verdict_snapshot(self):
        state = bump_tasks.load(self.forum)
        state["phase"] = "complete"
        state["review"] = {"snapshot": {"snapshot_id": "s", "sha256": "hash", "main_sha": "base",
            "config_revision": 0, "passed": True},
            "verdict": {"snapshot_id": "s", "snapshot_sha256": "hash", "approved": True, "author": "Bob"}}
        self.assertEqual(web._compiler_bump_metrics(state)["acceptance"], "recorded_complete")
        for mutation in (lambda s: s.update(main_sha="new"),
                         lambda s: s["config"].update(revision=1),
                         lambda s: s["review"]["verdict"].update(snapshot_sha256="other")):
            changed = deepcopy(state)
            mutation(changed)
            self.assertEqual(web._compiler_bump_metrics(changed)["acceptance"], "provisional")

    def test_writer_and_assistant_claims_use_current_assignments(self):
        bump_tasks.claim(self.forum, "Alice", self.tid)
        with bump_tasks.transaction(self.forum) as state:
            state["strategies"]["s"] = {"task_id": self.tid, "status": "active", "owner": "Alice",
                "description": "Try revised API", "assistants": ["Bob"]}
        agents = {a["name"]: a for a in web.get_workspace()["agents"]}
        self.assertEqual(agents["Alice"]["chunk"], self.tid)
        self.assertEqual(agents["Bob"]["activity"], "assisting: Try revised API")
        with bump_tasks.transaction(self.forum) as state:
            state["phase"] = "stopped"
        self.assertTrue(all(not a["chunk"] for a in web.get_workspace()["agents"]))

    def test_safe_stop_native_target_marker_only_and_preserves_prior_marker(self):
        with patch.object(web, "_current_run", return_value={"running": True, "command": "bump", "pid": 999}), \
             patch.object(bump_jobs, "terminate", side_effect=AssertionError("historical job kill")), \
             patch.object(web.os, "kill", side_effect=AssertionError("signal")), \
             patch.object(web.os, "killpg", side_effect=AssertionError("group signal")):
            response = self.client.post("/api/run/stop", json={"mode": "safe"})
            self.assertTrue(response.json()["stopping"])
            marker = self.target_unity / "stop-requested"
            before = (marker.read_bytes(), marker.stat().st_mtime_ns)
            self.client.post("/api/run/stop", json={"mode": "safe"})
            self.assertEqual((marker.read_bytes(), marker.stat().st_mtime_ns), before)
        self.assertFalse((self.unity / "stop-requested").exists())

    def test_preparing_origin_does_not_fall_through_to_legacy_stop(self):
        (self.forum / bump_tasks.STATE_FILE).unlink()
        with patch.object(web, "_current_run", return_value={"running": True, "command": "bump"}), \
             patch.object(bump_jobs, "terminate", side_effect=AssertionError("legacy stop")):
            self.assertTrue(web.api_run_stop({"mode": "safe"})["stopping"])
        self.assertTrue((self.target_unity / "stop-requested").exists())

    def test_force_stop_refuses_unverified_pid_without_any_signals(self):
        with patch.object(web, "_current_run", return_value={"running": True, "command": "bump", "pid": 999}), \
             patch.object(bump_jobs, "terminate", side_effect=AssertionError("historical job kill")), \
             patch.object(web.os, "kill", side_effect=AssertionError("signal")), \
             patch.object(web.os, "killpg", side_effect=AssertionError("group signal")):
            response = self.client.post("/api/run/stop", json={"mode": "force"})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["stopped"])

    def test_project_metadata_and_decl_links_use_inventory_without_dag_file(self):
        self.assertFalse((self.forum / "dag.json").exists())
        metadata = self.client.get("/api/project").json()
        self.assertTrue(metadata["has_dag"])
        self.assertEqual(set(metadata["chunks"]), set(bump_tasks.load(self.forum)["chunks"]))
        result = web._chunk_for_decl("A.first")
        self.assertEqual(result["source"], "lexical_inventory")
        self.assertIsNone(web._chunk_for_decl("first"))

    def test_other_command_routes_never_read_compiler_state(self):
        for command in ("formalize", "autoformalize", "solve", "prove"):
            with self.subTest(command=command):
                self.set_command(command)
                with patch.object(web, "_compiler_bump_state", side_effect=AssertionError("other pipeline")):
                    self.assertEqual(web.get_bump_state(), {})
                    self.assertEqual(web.get_bump_metrics(), {})
                    self.assertFalse(web.api_project()["has_dag"])

    def test_frontend_separates_compiler_from_legacy_formalization_rendering(self):
        self.assertIn("af.schema === 'compiler-migration-v2'", web.APP_HTML)
        self.assertIn("Bump compiler workflow", web.APP_HTML)
        self.assertIn("else if (af.run_id && (['formalize', 'autoformalize']", web.APP_HTML)
        self.assertIn("graphSchema === 'compiler-migration-v2'", web.DAG_HTML)
        self.assertIn("scheduling dependencies", web.DAG_HTML)

    def test_embedded_scripts_are_syntactically_valid(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is not installed; no runtime is downloaded")
        for document in (web.APP_HTML, web.DAG_HTML):
            for script in re.findall(r"<script>(.*?)</script>", document, re.DOTALL):
                result = subprocess.run([node, "--check", "-"], input=script, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
