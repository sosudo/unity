import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unity.forum import web


class SolveWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.unity = self.root / ".unity"
        self.forum = self.unity / "forum"
        self.forum.mkdir(parents=True)
        (self.unity / "logs").mkdir()
        (self.unity / "agents.yaml").write_text(
            "agents:\n"
            "  - name: Ada\n"
            "    model: test\n"
            "    backend: codex\n"
            "    primary: true\n"
            "  - name: Bert\n"
            "    model: test\n"
            "    backend: codex\n"
        )
        self.old_root, self.old_forum = web.ROOT_DIR, web.FORUM_DIR
        web.ROOT_DIR, web.FORUM_DIR = self.unity, self.forum

    def tearDown(self):
        web.ROOT_DIR, web.FORUM_DIR = self.old_root, self.old_forum
        self.temp.cleanup()

    def test_solve_state_endpoint_loads_run_scoped_state(self):
        expected = {
            "revision": 7,
            "stage": "formalizing",
            "gates": {"solution": {"status": "accepted", "gate_revision": 2}},
        }
        with patch.object(web, "_load_solve_state", return_value=expected) as load:
            self.assertIs(web.get_solve_state(), expected)
        load.assert_called_once_with(self.forum)

    def test_solve_state_endpoint_is_safe_before_initialization(self):
        state = web.get_solve_state()
        self.assertEqual(state["phase"], "solving")
        self.assertEqual(state["revision"], 0)
        self.assertEqual(state["solution"]["status"], "open")
        self.assertEqual(state["formalization"]["status"], "waiting")

    def test_corrupt_stale_solve_state_cannot_break_prove_web_overview(self):
        (self.unity / "state.json").write_text(json.dumps({
            "command": "prove", "phase": "proving",
        }))
        solve_state_path = self.forum / "solve-state.json"
        corrupt = b'{"run_id": "stale-solve"'
        solve_state_path.write_bytes(corrupt)

        state = web.get_solve_state()

        self.assertEqual(state["phase"], "solving")
        self.assertEqual(state["solution"]["status"], "open")
        self.assertEqual(state["revision"], 0)
        self.assertEqual(solve_state_path.read_bytes(), corrupt)
        # Prove workspace loading must not consult the unavailable solve state.
        agents = web.get_workspace()["agents"]
        self.assertEqual({agent["name"] for agent in agents}, {"Ada", "Bert"})

    def test_solve_claims_are_visible_in_agent_status_only_during_solve(self):
        solve_state = {
            "strategies": {
                "strategy-1": {
                    "strategy_id": "strategy-1",
                    "target": "subgoal-1",
                    "description": "derive the extremal reduction",
                    "owner": "Bert",
                    "status": "claimed",
                    "assistants": [],
                },
            },
            "formal_tasks": {},
        }
        (self.unity / "state.json").write_text(json.dumps({
            "command": "solve", "phase": "solving",
        }))
        with patch.object(web, "_load_solve_state", return_value=solve_state):
            agents = {item["name"]: item for item in web.get_workspace()["agents"]}
        self.assertEqual(agents["Bert"]["chunk"], "subgoal-1")
        self.assertEqual(agents["Bert"]["activity"], "derive the extremal reduction")

        (self.unity / "state.json").write_text(json.dumps({
            "command": "prove", "phase": "proving",
        }))
        with patch.object(
            web, "_load_solve_state", side_effect=AssertionError("solve state must stay isolated")
        ):
            agents = {item["name"]: item for item in web.get_workspace()["agents"]}
        self.assertEqual(agents["Bert"]["activity"], "")
        self.assertEqual(agents["Bert"]["chunk"], "")

    def test_overview_has_solve_only_state_and_preserves_prove_endpoint(self):
        self.assertIn("J('/api/solve-state')", web.APP_HTML)
        self.assertIn("J('/api/prove-state')", web.APP_HTML)
        self.assertIn("r.command === 'solve'", web.APP_HTML)
        self.assertIn("<b>informal solution</b>", web.APP_HTML)
        self.assertIn("<b>Lean formalization</b>", web.APP_HTML)
        self.assertIn("<h2>formal tasks</h2>", web.APP_HTML)

    def test_icrl_is_hidden_only_for_authoritative_workspaces(self):
        for command in ("prove", "solve"):
            with self.subTest(command=command):
                (self.unity / "state.json").write_text(json.dumps({"command": command}))
                self.assertFalse(web._icrl_visible())
        (self.unity / "state.json").write_text(json.dumps({"command": "bump"}))
        # Bump owns a structured Forum too; Solve's behavior is unchanged.
        self.assertFalse(web._icrl_visible())


if __name__ == "__main__":
    unittest.main()
