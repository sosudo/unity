import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from asyncclick.testing import CliRunner

from unity.commands import mcp as mcp_module
from unity.config import Paths
from unity.forum import server
from unity import prove_state


class McpArtifactTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.unity = self.root / ".unity"
        self.forum = self.unity / "forum"
        self.forum.mkdir(parents=True)
        self.paths = Paths.from_unity_dir(self.unity)
        old_forum = server.FORUM_DIR
        server.FORUM_DIR = self.forum
        server.forum_create_thread("global", "Global")
        server.forum_post("global", "Ada", "X" * 2000)
        server.FORUM_DIR = old_forum

    def tearDown(self):
        self.temp.cleanup()

    async def invoke(self, server_name="forum", tool="forum_read", args=None):
        runner = CliRunner()
        with patch("unity.config.load_paths", return_value=self.paths), \
             patch.dict("os.environ", {
                 "UNITY_ARTIFACT_THRESHOLD_BYTES": "100",
                 "UNITY_ARTIFACT_PREVIEW_BYTES": "60",
                 "UNITY_AGENT_NAME": "Ada",
             }):
            return await runner.invoke(
                mcp_module.command,
                [server_name, tool, json.dumps(args if args is not None else {"thread_id": "global"})],
                catch_exceptions=False,
            )

    async def test_large_mcp_output_is_compacted_for_active_structured_runs(self):
        (self.unity / "state.json").write_text(json.dumps({
            "command": "prove", "phase": "proving",
        }))
        prove_result = await self.invoke('unity-prove', 'prove_status', {})
        self.assertNotEqual(prove_result.exit_code, 0)
        self.assertIn('disabled', prove_result.output)

        (self.unity / "state.json").write_text(json.dumps({
            "command": "solve", "phase": "solving",
        }))
        solve_result = await self.invoke()
        self.assertEqual(solve_result.exit_code, 0)
        self.assertIn("Full output: artifact-", solve_result.output)
        self.assertEqual(
            len(list((self.paths.artifacts / "records").glob("artifact-*.json"))), 1
        )

        (self.unity / "state.json").write_text(json.dumps({
            "command": "optimize", "phase": "optimizing",
        }))
        other_result = await self.invoke()
        self.assertEqual(other_result.exit_code, 0)
        self.assertNotIn("Full output: artifact-", other_result.output)
        self.assertIn("X" * 200, other_result.output)
        self.assertEqual(
            len(list((self.paths.artifacts / "records").glob("artifact-*.json"))), 1
        )


if __name__ == "__main__":
    unittest.main()
