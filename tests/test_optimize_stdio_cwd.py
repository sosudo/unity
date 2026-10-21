"""Offline subprocess tests for the Optimize-only MCP cwd adapter."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from unity.optimize_stdio_cwd import main


REPOSITORY = Path(__file__).resolve().parents[1]


class OptimizeStdioCwdTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="optimize-stdio-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source with spaces"
        self.source.mkdir()
        self.runner = self.root / "runner"
        self.runner.mkdir()
        # The real installed module is launched from an unrelated directory.
        # No SDK, model, compiler, network service, or evaluation is involved.
        self.env = {**os.environ, "PYTHONPATH": str(REPOSITORY), "PWD": str(self.runner),
                    "OPTIMIZE_STDIO_FIXTURE": "value with spaces; $(not-a-command)"}

    def command(self, *command, cwd=None):
        return [sys.executable, "-m", "unity.optimize_stdio_cwd", "--cwd",
                str(self.source if cwd is None else cwd), "--", *command]

    def run_adapter(self, *command, cwd=None, input=None):
        return subprocess.run(self.command(*command, cwd=cwd), cwd=self.runner,
                              env=self.env, input=input, text=True, capture_output=True, timeout=10)

    def test_exec_preserves_argv_environment_stdio_and_process_id(self):
        script = (
            "import json, os, sys; "
            "print(json.dumps(dict(argv=sys.argv[1:], cwd=os.getcwd(), pwd=os.environ['PWD'], "
            "fixture=os.environ['OPTIMIZE_STDIO_FIXTURE'], stdin=sys.stdin.read(), pid=os.getpid()))); "
            "print('fixture stderr', file=sys.stderr)"
        )
        arguments = ["one two", "$(touch NEVER)", "; echo NEVER", "a'b\"c", "λ", "--flag"]
        with subprocess.Popen(self.command(sys.executable, "-c", script, *arguments),
                              cwd=self.runner, env=self.env, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            output, errors = process.communicate("fixture input\n", timeout=10)
        self.assertEqual(process.returncode, 0, errors)
        data = json.loads(output)
        self.assertEqual(data, {
            "argv": arguments, "cwd": str(self.source), "pwd": str(self.source),
            "fixture": self.env["OPTIMIZE_STDIO_FIXTURE"], "stdin": "fixture input\n",
            "pid": process.pid,
        })
        self.assertEqual(errors, "fixture stderr\n")
        self.assertEqual(list(self.source.iterdir()), [])
        self.assertEqual(list(self.runner.iterdir()), [])

    def test_child_exit_code_is_unchanged(self):
        result = self.run_adapter(sys.executable, "-c", "raise SystemExit(37)")
        self.assertEqual(result.returncode, 37)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    @unittest.skipUnless(os.name == "posix", "POSIX exec signal status")
    def test_child_signal_status_is_unchanged(self):
        result = self.run_adapter(sys.executable, "-c",
                                  "import os, signal; os.kill(os.getpid(), signal.SIGTERM)")
        self.assertEqual(result.returncode, -signal.SIGTERM)

    def test_exec_uses_exact_command_and_preserves_environment_except_pwd(self):
        supplied_env = {"PATH": "/fixture/bin", "FIXTURE": "untouched", "PWD": "/old"}
        with patch.dict(os.environ, supplied_env, clear=True), patch("os.chdir") as chdir, \
                patch("os.execvpe") as execute:
            main(["--cwd", str(self.source), "--", "server-command", "--arg", "a b;$x"])
        chdir.assert_called_once_with(self.source)
        execute.assert_called_once_with("server-command", ["server-command", "--arg", "a b;$x"],
                                        {**supplied_env, "PWD": str(self.source)})

    def test_relative_missing_file_and_symlink_cwd_are_rejected_before_exec(self):
        file = self.root / "not-a-directory"
        file.write_text("fixture")
        alias = self.root / "source-alias"
        alias.symlink_to(self.source, target_is_directory=True)
        for invalid in ("relative", self.root / "missing", file, alias,
                        str(self.source / ".." / self.source.name)):
            with self.subTest(cwd=str(invalid)):
                result = self.run_adapter(sys.executable, "-c", "print('MUST NOT RUN')", cwd=invalid)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("existing canonical absolute directory", result.stderr)

    def test_missing_and_empty_command_are_rejected_before_chdir(self):
        for suffix in ([], ["--"], ["--", ""]):
            with self.subTest(suffix=suffix), patch("os.chdir") as chdir, \
                    patch("os.execvpe") as execute, patch("sys.stderr"):
                with self.assertRaises(SystemExit) as failure:
                    main(["--cwd", str(self.source), *suffix])
                self.assertEqual(failure.exception.code, 2)
                chdir.assert_not_called()
                execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
