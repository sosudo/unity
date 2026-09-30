import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asyncclick.testing import CliRunner

from unity.commands import mcp as mcp_module
from unity.config import Paths


class McpJsonArgsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = Paths.from_unity_dir(self.root / ".unity")
        self.payload = {
            "content": 'import Mathlib.Data.Int.Basic\n-- quote \' and " and \\\n'
                       'example (n : ℤ) : n = n := by\n  rfl\n',
            "context": {"name": "étude", "enabled": True, "other": None},
        }

    async def invoke(self, args, *, input=None):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.call_tool.return_value = SimpleNamespace(content=[])
        with patch("unity.config.load_paths", return_value=self.paths) as load_paths, \
             patch("unity.orchestrator.build_mcp", return_value={"axle": {}}) as build_mcp, \
             patch("unity.orchestrator.build_solve_mcp") as build_solve_mcp, \
             patch("fastmcp.Client", return_value=client) as factory:
            result = await CliRunner().invoke(mcp_module.command, ["axle", "check", *args], input=input)
        return result, client, load_paths, build_mcp, build_solve_mcp, factory

    async def assert_delivered(self, args, expected, *, input=None):
        result, client, load_paths, build_mcp, _, factory = await self.invoke(args, input=input)
        self.assertEqual(result.exit_code, 0, result.output)
        load_paths.assert_called_once()
        build_mcp.assert_called_once()
        factory.assert_called_once()
        client.call_tool.assert_awaited_once_with("check", expected)

    async def assert_rejected(self, args, message, *, input=None):
        result, client, *before_service = await self.invoke(args, input=input)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn(message, result.output)
        client.call_tool.assert_not_awaited()
        for mock in before_service:
            mock.assert_not_called()

    async def test_utf8_file_preserves_multiline_quotes_backslash_and_unicode(self):
        request = self.root / "request with spaces.json"
        request.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")
        await self.assert_delivered(["--args-file", str(request)], self.payload)

    async def test_stdin_preserves_multiline_quotes_backslash_and_unicode(self):
        await self.assert_delivered(
            ["--args-file", "-"], self.payload,
            input=json.dumps(self.payload, ensure_ascii=False),
        )

    async def test_positional_json_remains_supported(self):
        await self.assert_delivered([json.dumps(self.payload, ensure_ascii=False)], self.payload)

    async def test_omitted_args_remain_empty_object(self):
        await self.assert_delivered([], {})

    async def test_empty_positional_args_remain_empty_object(self):
        for value in ("", "  \n"):
            with self.subTest(value=value):
                await self.assert_delivered([value], {})

    async def test_empty_object_file_is_supported(self):
        request = self.root / "request.json"
        request.write_text("{}", encoding="utf-8")
        await self.assert_delivered(["--args-file", str(request)], {})

    async def test_positional_and_file_conflict_even_for_explicit_empty_object(self):
        request = self.root / "request.json"
        request.write_text("{}", encoding="utf-8")
        for value in ("{}", ""):
            with self.subTest(value=value):
                await self.assert_rejected(
                    [value, "--args-file", str(request)], "not both",
                )

    async def test_positional_and_stdin_conflict(self):
        await self.assert_rejected(["{}", "--args-file", "-"], "not both", input="{}")

    async def test_malformed_positional_json_does_not_initialize_services(self):
        await self.assert_rejected(['{"content":'], "args must be a JSON object")

    async def test_malformed_file_does_not_initialize_services(self):
        request = self.root / "request.json"
        request.write_text('{"content":', encoding="utf-8")
        await self.assert_rejected(["--args-file", str(request)], "args must be a JSON object")

    async def test_malformed_and_empty_stdin_do_not_initialize_services(self):
        for value in ('{"content":', "", "   "):
            with self.subTest(value=value):
                await self.assert_rejected(
                    ["--args-file", "-"], "args must be a JSON object", input=value,
                )

    async def test_nonobject_payload_rejected_in_all_interfaces(self):
        request = self.root / "request.json"
        for value in ([], "text", 3, True, None):
            encoded = json.dumps(value)
            request.write_text(encoded, encoding="utf-8")
            for args, input in (([encoded], None), (["--args-file", str(request)], None),
                                (["--args-file", "-"], encoded)):
                with self.subTest(value=value, args=args):
                    await self.assert_rejected(args, "args must be a JSON object", input=input)

    async def test_missing_file_does_not_initialize_services(self):
        await self.assert_rejected(
            ["--args-file", str(self.root / "missing.json")], "Invalid value for '--args-file'",
        )

    async def test_non_utf8_file_does_not_initialize_services(self):
        request = self.root / "request.json"
        request.write_bytes(b'{"content":"\xff"}')
        await self.assert_rejected(["--args-file", str(request)], "cannot read JSON args")


class FormalizingCommunicationPromptTests(unittest.TestCase):
    def setUp(self):
        prompts = Path(__file__).parents[1] / "unity" / "prompts"
        self.tools = " ".join((prompts / "SOLVE_FORMALIZING_TOOLS.md").read_text().split())
        self.phase = " ".join((prompts / "solve" / "FORMALIZING.md").read_text().split())

    def test_formalizer_has_safe_json_file_and_stdin_transport(self):
        self.assertIn("json.dump", self.tools)
        self.assertIn("json.dumps", self.tools)
        self.assertIn("exactly the documented tool fields", self.tools)
        self.assertIn("unity mcp axle check --args-file /path/to/request.json", self.tools)
        self.assertIn("unity mcp axle check --args-file - < /path/to/request.json", self.tools)
        self.assertIn("positional JSON or `--args-file`, not both", self.tools)
        self.assertIn("do not guess extra arguments or concatenate unescaped source into shell strings", self.tools)
        self.assertIn("--args-file", self.phase)
        self.assertIn("do not manually embed Lean in shell-quoted JSON", self.phase)

    def test_findings_are_early_reusable_targeted_and_not_activity_quotas(self):
        self.assertIn(
            "publish_finding(author, kind, title, content, confidence, target?, strategy_id?, evidence?, supersedes?, declarations?, files?)",
            self.tools,
        )
        for text in (self.tools, self.phase):
            with self.subTest(text=text[:40]):
                self.assertIn("before substantial follow-on work", text.lower())
                self.assertRegex(text.lower(), r"do not wait for (?:your )?(?:whole|entire) proof")
                self.assertRegex(text.lower(), r"reuse (?:existing|others') findings")
                self.assertIn("target", text)
                self.assertIn("evidence", text)
                self.assertRegex(text.lower(), r"routine reads and unchanged checks (?:do not need|need no) posts")


if __name__ == "__main__":
    unittest.main()
