"""Solve MCP diagnostic routing with mocked servers and real artifact storage."""

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import asyncclick as click
from asyncclick.testing import CliRunner

from unity import artifacts
from unity.commands import mcp as mcp_module
from unity.config import Paths


class FakeStdioCall:
    def __init__(self, *, startup="", during="", shutdown="", error=None, startup_error=None):
        self.startup = startup
        self.during = during
        self.shutdown = shutdown
        self.error = error
        self.startup_error = startup_error
        self.result = SimpleNamespace(content=[SimpleNamespace(text="RESULT_FROM_TOOL")])
        self.events = []
        self.transport_options = None
        self.transport = None
        self.calls = []

    def make_transport(self, **options):
        self.transport_options = options
        self.transport = SimpleNamespace(**options)
        return self.transport

    def make_client(self, transport):
        if transport is not self.transport:
            raise AssertionError("client must use the configured stdio transport")
        return self

    def write_stderr(self, text):
        if self.transport.log_file.closed:
            raise AssertionError("temporary stderr file closed before server cleanup")
        # A subprocess writes to the underlying descriptor, not the Python buffer.
        if text:
            os.write(self.transport.log_file.fileno(), text.encode("utf-8"))

    async def __aenter__(self):
        self.events.append("enter")
        self.write_stderr(self.startup)
        if self.startup_error is not None:
            raise self.startup_error
        return self

    async def call_tool(self, tool, kwargs):
        self.calls.append((tool, kwargs))
        self.events.append("call")
        self.write_stderr(self.during)
        if self.error is not None:
            raise self.error
        return self.result

    async def __aexit__(self, exc_type, exc, traceback):
        self.events.append("exit")
        self.write_stderr(self.shutdown)
        return False

    @contextmanager
    def patched(self):
        with patch("fastmcp.client.transports.StdioTransport", side_effect=self.make_transport), \
             patch("fastmcp.Client", side_effect=self.make_client):
            yield self


class McpDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="unity-mcp-diagnostic-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = Paths.from_unity_dir(self.root / ".unity")
        self.paths.unity.mkdir()
        self.spec = {
            "command": "fake-stdio-server", "args": ["--test"],
            "cwd": "/fake/project", "env": {"PATH": "/fake/bin"},
        }
        self.environment = patch.dict(os.environ, {
            "UNITY_AGENT_NAME": "Ada",
            "UNITY_ARTIFACT_THRESHOLD_BYTES": "10000",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def records(self):
        return artifacts.list_artifacts(self.paths.artifacts)

    def diagnostic_record(self, note):
        records = self.records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertIn(record["artifact_id"], note)
        self.assertEqual(record["kind"], "mcp_diagnostics")
        self.assertEqual(record["producer"], "Ada")
        self.assertEqual(record["source"], "axle.check")
        return record, artifacts.artifact_bytes(self.paths.artifacts, record["artifact_id"]).decode("utf-8")

    async def invoke(self, fake, *, args=None):
        (self.paths.unity / "state.json").write_text(json.dumps({
            "command": "solve", "phase": "formalizing",
        }))
        with fake.patched(), \
             patch("unity.config.load_paths", return_value=self.paths), \
             patch("unity.solve_formal_orchestrator.build_solve_formal_mcp", return_value={"axle": self.spec}):
            return await CliRunner().invoke(
                mcp_module.command, ["axle", "check", *(args or ["{}"])],
            )

    async def test_helper_preserves_result_and_exact_stderr_through_shutdown(self):
        fake = FakeStdioCall(
            startup="WARNING server/discover not supported\n",
            during="a useful diagnostic: ∀\n",
            shutdown="server shutdown complete\n",
        )
        request = {"content": "example : True := by\n  trivial"}
        with fake.patched():
            result, note = await mcp_module._call_solve_stdio(
                self.paths, "axle", self.spec, "check", request,
            )
        self.assertIs(result, fake.result)
        self.assertEqual(fake.calls, [("check", request)])
        self.assertEqual(fake.events, ["enter", "call", "exit"])
        self.assertFalse(fake.transport_options["keep_alive"])
        self.assertEqual(fake.transport_options["command"], self.spec["command"])
        self.assertEqual(fake.transport_options["args"], self.spec["args"])
        self.assertEqual(fake.transport_options["env"], self.spec["env"])
        self.assertEqual(fake.transport_options["cwd"], self.spec["cwd"])
        self.assertTrue(fake.transport.log_file.closed)
        _, diagnostics = self.diagnostic_record(note)
        self.assertEqual(diagnostics, fake.startup + fake.during + fake.shutdown)

    async def test_cli_success_prints_tool_result_before_only_the_diagnostic_reference(self):
        fake = FakeStdioCall(startup="WARNING server/discover failed validation\n", shutdown="cleanup log\n")
        result = await self.invoke(fake)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(result.output.startswith("RESULT_FROM_TOOL\n"), result.output)
        self.assertIn("Server diagnostics: artifact-", result.output)
        self.assertNotIn("WARNING", result.output)
        self.assertNotIn("cleanup log", result.output)
        self.assertEqual(result.stderr, "")
        _, diagnostics = self.diagnostic_record(result.output)
        self.assertEqual(diagnostics, fake.startup + fake.shutdown)

    async def test_helper_no_stderr_produces_no_diagnostic_artifact(self):
        fake = FakeStdioCall()
        with fake.patched():
            result, note = await mcp_module._call_solve_stdio(self.paths, "axle", self.spec, "check", {})
        self.assertIs(result, fake.result)
        self.assertFalse(note)
        self.assertEqual(self.records(), [])
        self.assertTrue(fake.transport.log_file.closed)

    async def test_cli_no_stderr_has_only_original_result(self):
        result = await self.invoke(FakeStdioCall())
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output, "RESULT_FROM_TOOL\n")
        self.assertEqual(self.records(), [])

    async def test_tool_error_is_nonzero_and_retains_error_and_diagnostics(self):
        from fastmcp.exceptions import ToolError
        fake = FakeStdioCall(
            startup="startup warning\n", during="server error details\n",
            shutdown="cleanup log\n", error=ToolError("proof check failed"),
        )
        result = await self.invoke(fake)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("proof check failed", result.output)
        self.assertIn("axle.check failed", result.output)
        self.assertNotIn("RESULT_FROM_TOOL", result.output)
        _, diagnostics = self.diagnostic_record(result.output)
        self.assertIn(fake.startup + fake.during + fake.shutdown, diagnostics)
        self.assertIn("ToolError: proof check failed", diagnostics)
        self.assertEqual(fake.events, ["enter", "call", "exit"])
        self.assertTrue(fake.transport.log_file.closed)

    async def test_startup_failure_is_nonzero_and_retains_startup_diagnostics(self):
        fake = FakeStdioCall(startup="server cannot initialize\n", startup_error=OSError("startup failed"))
        result = await self.invoke(fake)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("startup failed", result.output)
        _, diagnostics = self.diagnostic_record(result.output)
        self.assertIn("server cannot initialize", diagnostics)
        self.assertIn("OSError: startup failed", diagnostics)
        self.assertEqual(fake.calls, [])
        self.assertTrue(fake.transport.log_file.closed)

    async def test_failure_without_server_stderr_still_retains_exception_artifact(self):
        fake = FakeStdioCall(startup_error=FileNotFoundError("missing executable"))
        with fake.patched(), self.assertRaises(click.ClickException) as raised:
            await mcp_module._call_solve_stdio(self.paths, "axle", self.spec, "check", {})
        _, diagnostics = self.diagnostic_record(str(raised.exception))
        self.assertIn("FileNotFoundError: missing executable", diagnostics)

    async def test_cancellation_propagates_after_context_and_tempfile_cleanup(self):
        cancellation = asyncio.CancelledError("cancelled by controller")
        fake = FakeStdioCall(startup="startup log\n", shutdown="cleanup log\n", error=cancellation)
        with fake.patched(), self.assertRaises(asyncio.CancelledError) as raised:
            await mcp_module._call_solve_stdio(self.paths, "axle", self.spec, "check", {})
        self.assertIs(raised.exception, cancellation)
        self.assertEqual(fake.events, ["enter", "call", "exit"])
        self.assertTrue(fake.transport.log_file.closed)

    async def test_diagnostic_storage_permission_error_preserves_successful_result(self):
        fake = FakeStdioCall(startup="server startup warning\n")
        with fake.patched(), patch.object(
            artifacts, "store_text", side_effect=PermissionError("artifact store is read-only"),
        ):
            result, note = await mcp_module._call_solve_stdio(
                self.paths, "axle", self.spec, "check", {},
            )
        self.assertIs(result, fake.result)
        self.assertIn("Server diagnostics could not be saved:", note)
        self.assertIn("artifact store is read-only", note)
        self.assertTrue(fake.transport.log_file.closed)
        self.assertEqual(self.records(), [])

    async def test_diagnostic_storage_permission_error_preserves_original_tool_failure(self):
        from fastmcp.exceptions import ToolError
        fake = FakeStdioCall(error=ToolError("original proof check failed"))
        with patch.object(
            artifacts, "store_text", side_effect=PermissionError("artifact store is read-only"),
        ):
            result = await self.invoke(fake)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("original proof check failed", result.output)
        self.assertIn("axle.check failed", result.output)
        self.assertIn("Server diagnostics could not be saved:", result.output)
        self.assertIn("artifact store is read-only", result.output)
        self.assertNotIn("RESULT_FROM_TOOL", result.output)
        self.assertTrue(fake.transport.log_file.closed)
        self.assertEqual(self.records(), [])

    async def test_prove_remote_and_completed_solve_keep_existing_client_path(self):
        cases = (
            ({"command": "prove", "phase": "proving"}, self.spec),
            ({"command": "solve", "phase": "formalizing"}, {"url": "https://example.test/mcp"}),
            ({"command": "solve", "phase": "done"}, self.spec),
        )
        for state, spec in cases:
            with self.subTest(state=state, spec=spec):
                (self.paths.unity / "state.json").write_text(json.dumps(state))
                client = AsyncMock()
                client.__aenter__.return_value = client
                client.call_tool.return_value = SimpleNamespace(content=[SimpleNamespace(text="existing path")])
                with patch("unity.config.load_paths", return_value=self.paths), \
                     patch("unity.orchestrator.build_mcp", return_value={"axle": spec}), \
                     patch("unity.solve_formal_orchestrator.build_solve_formal_mcp", return_value={"axle": spec}), \
                     patch.object(mcp_module, "_call_solve_stdio", new_callable=AsyncMock) as helper, \
                     patch("fastmcp.Client", return_value=client) as factory:
                    result = await CliRunner().invoke(mcp_module.command, ["axle", "check", "{}"])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(result.output, "existing path\n")
                factory.assert_called_once_with({"mcpServers": {"axle": spec}})
                helper.assert_not_awaited()
                client.call_tool.assert_awaited_once_with("check", {})
        self.assertEqual(self.records(), [])

    async def test_solve_forum_remains_in_process_and_bypasses_stdio_helper(self):
        from unity.forum import solve_server
        (self.paths.unity / "state.json").write_text(json.dumps({"command": "solve", "phase": "formalizing"}))
        for name in ("forum", "unity-forum"):
            with self.subTest(server=name):
                client = AsyncMock()
                client.__aenter__.return_value = client
                client.call_tool.return_value = SimpleNamespace(content=[SimpleNamespace(text="forum result")])
                server = object()
                with patch("unity.config.load_paths", return_value=self.paths), \
                     patch.object(solve_server, "configure") as configure, \
                     patch.object(solve_server, "build_server", return_value=server), \
                     patch.object(mcp_module, "_call_solve_stdio", new_callable=AsyncMock) as helper, \
                     patch("fastmcp.Client", return_value=client) as factory:
                    result = await CliRunner().invoke(mcp_module.command, [name, "solve_brief", "{}"])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(result.output, "forum result\n")
                helper.assert_not_awaited()
                factory.assert_called_once_with(server)
                configure.assert_called_once_with(self.paths.forum.resolve(), self.root.resolve(), "formalizing")
                client.call_tool.assert_awaited_once_with("solve_brief", {})
        self.assertEqual(self.records(), [])


if __name__ == "__main__":
    unittest.main()
