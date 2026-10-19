"""Offline workspace plumbing, plus opt-in installed-toolchain fixture tests.

Ordinary discovery uses fake native jobs. Set UNITY_OPTIMIZE_NATIVE_TESTS=1
to compile the helper using already installed Lean 4.19.0/4.34.1; never downloads.
"""
import json
import os
import platform
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from unity import optimize_native as native, optimize_workspace as workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="optimize-workspace-offline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "src" / "Example.lean").write_text("theorem t : True := by trivial\n")
        self.report = {"modules": {"src/Example.lean": "Example"}, "traces": {"Example": ".lake/build/lib/lean/Example.trace"},
                       "build_dir": ".lake/build", "source_roots": [{"kind": "library", "name": "Example", "path": "src"}],
                       "libraries": ["Example"], "module_owners": {"src/Example.lean": {"libraries": ["Example"], "executables": []}},
                       "default_modules": {"src/Example.lean": "Example"}, "unknown_default_targets": [], "unmatched": [], "issues": []}

    def test_actual_srcdir_mapping_and_lake_options_are_retained(self):
        with patch.object(workspace, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(self.report), "")) as run:
            report = workspace.discover(self.root, ["src/Example.lean"], executable=self.root / "fake-workspace", lake_args=["-Kmode=checked"])
        self.assertEqual(report["modules"], {"src/Example.lean": "Example"})
        self.assertEqual(run.call_args.args[1], ["lake", "-Kmode=checked", "env", str(self.root / "fake-workspace"),
                                               "--lake-option", "mode", "checked", "src/Example.lean"])

    def test_no_cwd_command_or_toolchain_override(self):
        for value in ("--dir=/tmp", "+other", "build", "-Kx=y\nsecret", "-K=bad"):
            with self.assertRaises(ValueError):
                workspace.checked_lake_args([value])
        with self.assertRaises(ValueError):
            workspace.checked_lake_args(["-Kx=one", "-Kx=two"])

    def test_unmatched_file_is_retained_not_fabricated_as_module(self):
        report = {**self.report, "modules": {}, "module_owners": {}, "unmatched": ["src/Example.lean"]}
        with patch.object(workspace, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(report), "")):
            actual = workspace.discover(self.root, ["src/Example.lean"], executable=self.root / "fake-workspace")
        self.assertEqual(actual["unmatched"], ["src/Example.lean"])
        self.assertEqual(actual["modules"], {})

    def test_escaping_path_and_symlink_rejected_before_tools(self):
        for filename in ("../src/Example.lean", str(self.root / "src/Example.lean")):
            with self.assertRaises(ValueError):
                workspace.discover(self.root, [filename], executable=self.root / "fake-workspace")
        (self.root / "Alias.lean").symlink_to(self.root / "src/Example.lean")
        with self.assertRaises(ValueError):
            workspace.discover(self.root, ["Alias.lean"], executable=self.root / "fake-workspace")

    def test_ambiguous_native_report_is_not_accepted(self):
        report = {**self.report, "issues": ["ambiguous canonical ownership"]}
        with patch.object(workspace, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(report), "")):
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                workspace.discover(self.root, ["src/Example.lean"], executable=self.root / "fake-workspace")

    def test_native_cache_is_optimize_only_and_keyed_to_helper(self):
        source = self.root / "helper.lean"
        source.write_text("-- offline helper fixture\n")
        calls = []
        def fake(root, command, *, name):
            calls.append(command)
            if "--version" in command:
                return subprocess.CompletedProcess(command, 0, "Lean fake test version\n", "")
            if "--print-prefix" in command:
                return subprocess.CompletedProcess(command, 0, str(self.root) + "\n", "")
            if "-c" in command:
                Path(command[command.index("-c") + 1]).write_text("/* offline generated fixture */")
            if "-o" in command:
                Path(command[command.index("-o") + 1]).write_bytes(b"offline executable fixture")
            return subprocess.CompletedProcess(command, 0, "", "")
        with patch.object(native, "_run", side_effect=fake):
            first = native.executable(self.root, source, name="workspace", link_args=("-lLake",))
            again = native.executable(self.root, source, name="workspace", link_args=("-lLake",))
            self.assertEqual(first, again)
            self.assertEqual(sum("leanc" in command for command in calls), 1)
            source.write_text("-- changed offline helper fixture\n")
            changed = native.executable(self.root, source, name="workspace", link_args=("-lLake",))
        self.assertIn("optimize-workspace", first.parts)
        self.assertNotEqual(first, changed)
        self.assertEqual(sum("leanc" in command for command in calls), 2)


@unittest.skipUnless(os.environ.get("UNITY_OPTIMIZE_NATIVE_TESTS") == "1",
                     "opt-in installed Lean helper integration; no downloads")
class InstalledWorkspaceTests(unittest.TestCase):
    """Real helper compilation/config loading, not an Optimize evaluation."""

    version = "4.34.1"
    deployment_target = None

    @classmethod
    def setUpClass(cls):
        cls.chain = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan")) / ("toolchains/leanprover--lean4---v" + cls.version)
        if not all((cls.chain / "bin" / name).is_file() for name in ("lean", "leanc", "lake")):
            raise unittest.SkipTest("Lean " + cls.version + " is not installed; never downloads")
        directory = tempfile.TemporaryDirectory(prefix="optimize-installed-workspace-")
        cls.addClassCleanup(directory.cleanup)
        cls.directory = Path(directory.name).resolve()
        cls.environment = {**os.environ, "PATH": str(cls.chain / "bin") + os.pathsep + os.environ.get("PATH", ""),
                           "LEAN_SYSROOT": str(cls.chain), "LEAN_PATH": "",
                           "ELAN_TOOLCHAIN": "leanprover/lean4:v" + cls.version}
        if cls.deployment_target is not None and platform.system() == "Darwin":
            cls.environment["MACOSX_DEPLOYMENT_TARGET"] = cls.deployment_target
        source = Path(workspace.__file__).with_suffix(".lean")
        generated = cls.directory / "workspace.c"
        cls.executable = cls.directory / "workspace"
        for command in ([str(cls.chain / "bin/lean"), "-R", str(source.parent), "-c", str(generated), str(source)],
                        [str(cls.chain / "bin/leanc"), "-o", str(cls.executable), str(generated), "-lLake", "-rdynamic"]):
            result = subprocess.run(command, cwd=cls.directory, env=cls.environment,
                                    capture_output=True, text=True, timeout=180)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="project-", dir=self.directory)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.write("lean-toolchain", "leanprover/lean4:v" + self.version + "\n")
        self.write("src/Example.lean", "theorem first : True := by trivial\n")
        self.write("src/Example/Extra.lean", "def extra := 1\n")
        self.write("scratch/Draft.lean", "deliberately not Lean; inspection must not compile me\n")

    def write(self, name, content):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def inspect(self, paths):
        # Use the installed binary directly, not elan's auto-install wrapper.
        def run(root, command):
            self.assertEqual(command[0], "lake")
            result = subprocess.run([str(self.chain / "bin/lake"), *command[1:]], cwd=root,
                                    env=self.environment, capture_output=True, text=True, timeout=180)
            self.assertTrue(result.stdout.strip(), f"native helper exit {result.returncode}: {result.stderr}")
            return result
        with patch.object(workspace, "_run", side_effect=run):
            return workspace.discover(self.root, paths, executable=self.executable)

    def test_toml_srcdir_selected_modules_and_custom_build_directory(self):
        self.write("lakefile.toml", 'name = "optimize_fixture"\ndefaultTargets = ["ActualLibrary"]\n'
                   'buildDir = "generated"\n[[lean_lib]]\nname = "ActualLibrary"\n'
                   'srcDir = "src"\nroots = ["Example"]\n')
        report = self.inspect(["src/Example.lean", "src/Example/Extra.lean", "scratch/Draft.lean"])
        self.assertEqual(report["modules"], {"src/Example.lean": "Example", "src/Example/Extra.lean": "Example.Extra"})
        self.assertEqual(report["module_owners"]["src/Example.lean"]["libraries"], ["ActualLibrary"])
        self.assertEqual(report["build_dir"], "generated")
        self.assertEqual(report["unmatched"], ["scratch/Draft.lean"])
        self.assertIn({"kind": "library", "name": "ActualLibrary", "path": "src"}, report["source_roots"])

    def test_lean_dsl_configuration_native_loading_and_layout_only(self):
        self.write("lakefile.lean", 'import Lake\nopen Lake DSL\npackage optimize_fixture where\n'
                   '  buildDir := "generated"\n@[default_target]\nlean_lib ActualLibrary where\n'
                   '  srcDir := "src"\n  roots := #[`Example]\n')
        layout = self.inspect(["--layout-only"])
        self.assertEqual(layout["build_dir"], "generated")
        self.assertEqual(layout["modules"], {})
        report = self.inspect(["src/Example.lean", "scratch/Draft.lean"])
        self.assertEqual(report["modules"], {"src/Example.lean": "Example"})
        self.assertEqual(report["module_owners"]["src/Example.lean"], {"libraries": ["ActualLibrary"], "executables": []})
        self.assertEqual(report["unmatched"], ["scratch/Draft.lean"])

    def test_source_root_prefix_and_unconfigured_module_do_not_establish_ownership(self):
        self.write("lakefile.toml", 'name = "optimize_fixture"\n[[lean_lib]]\nname = "ActualLibrary"\n'
                   'srcDir = "src"\nroots = ["Example"]\n')
        self.write("srcElsewhere/Example.lean", "def outside := 1\n")
        self.write("src/Other.lean", "def notInRoots := 1\n")
        report = self.inspect(["src/Example.lean", "srcElsewhere/Example.lean", "src/Other.lean"])
        self.assertEqual(report["modules"], {"src/Example.lean": "Example"})
        self.assertEqual(set(report["unmatched"]), {"srcElsewhere/Example.lean", "src/Other.lean"})

    def test_canonical_module_collision_is_rejected_and_executable_root_is_mapped(self):
        self.write("lakefile.toml", 'name = "optimize_fixture"\n[[lean_exe]]\nname = "Tool"\n'
                   'srcDir = "tools"\nroot = "Main"\n')
        self.write("tools/Main.lean", "def main : IO Unit := pure ()\n")
        report = self.inspect(["tools/Main.lean"])
        self.assertEqual(report["modules"], {"tools/Main.lean": "Main"})
        self.assertEqual(report["module_owners"]["tools/Main.lean"]["executables"], ["Tool"])
        self.write("lakefile.toml", 'name = "optimize_fixture"\n[[lean_lib]]\nname = "First"\n'
                   'srcDir = "src"\nroots = ["Example"]\n[[lean_lib]]\nname = "Second"\n'
                   'srcDir = "other"\nroots = ["Example"]\n')
        self.write("other/Example.lean", "theorem second : True := by trivial\n")
        with self.assertRaisesRegex(ValueError, "conflicts|ambiguous"):
            self.inspect(["src/Example.lean"])


class InstalledWorkspace419Tests(InstalledWorkspaceTests):
    """4.19 API/runtime fixtures with an explicit test-only macOS target.

Its bundled leanc otherwise defaults to future deployment target 99.0. On
current macOS the resulting old-linker binary is rejected by dyld with
missing __DATA_CONST SG_READ_ONLY. This is not a production fallback: runtime
linker flags/environment remain unchanged, and that host limitation remains.
"""
    version = "4.19.0"
    deployment_target = "13.0"


if __name__ == "__main__":
    unittest.main()
