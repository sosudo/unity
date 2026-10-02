"""Native Lake ownership and header-only imports; no agents or remote services."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unity import formalize_workspace as workspace


class WorkspaceReportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="unity-formalize-workspace-report-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        (self.root / "Main.lean").write_text("def value := 1\n")

    def response(self, report):
        return SimpleNamespace(stdout=json.dumps(report), stderr="", returncode=0)

    def test_import_reader_uses_only_header_mode_and_deduplicated_checked_paths(self):
        report = {"imports": {"Main.lean": ["Init", "Foo.«odd-name»"]}, "issues": []}
        with patch.object(workspace, "_run", return_value=self.response(report)) as run:
            self.assertEqual(workspace.read_imports(self.root, ["Main.lean", "Main.lean"],
                                                   executable=Path("/inspector")), report["imports"])
        run.assert_called_once_with(self.root, ["lake", "env", "/inspector", "--imports", "Main.lean"])

    def test_import_reader_rejects_partial_malformed_and_error_reports(self):
        for report in ({"imports": {}, "issues": []},
                       {"imports": {"Main.lean": "Init"}, "issues": []},
                       {"imports": {"Main.lean": ["Init", "Init"]}, "issues": []},
                       {"imports": {"Main.lean": [3]}, "issues": []},
                       {"imports": {"Main.lean": ["Init"]}, "issues": ["bad header"]}):
            with self.subTest(report=report), patch.object(workspace, "_run", return_value=self.response(report)):
                with self.assertRaises(ValueError):
                    workspace.read_imports(self.root, ["Main.lean"], executable=Path("/inspector"))

    def test_unsafe_missing_alias_and_symlink_paths_fail_before_native_job(self):
        (self.root / "Alias.lean").symlink_to(self.root / "Main.lean")
        for name in ("../Main.lean", str(self.root / "Main.lean"), "./Main.lean",
                     "a/../Main.lean", "Missing.lean", "Alias.lean", "--layout-only"):
            with self.subTest(name=name), patch.object(workspace, "_run") as run:
                with self.assertRaises(ValueError):
                    workspace.read_imports(self.root, [name], executable=Path("/inspector"))
                run.assert_not_called()

    def test_empty_import_request_needs_no_helper(self):
        with patch.object(workspace, "_executable") as prepare, patch.object(workspace, "_run") as run:
            self.assertEqual(workspace.read_imports(self.root, []), {})
        prepare.assert_not_called()
        run.assert_not_called()

    def test_discovery_requires_consistent_actual_owner_metadata(self):
        report = {"modules": {"Main.lean": "Main"}, "traces": {}, "build_dir": ".lake/build",
                  "source_roots": [], "unmatched": [], "issues": [], "libraries": ["Core"],
                  "module_owners": {"Main.lean": {"libraries": ["Core"], "executables": []}}}
        with patch.object(workspace, "_run", return_value=self.response(report)):
            self.assertEqual(workspace.discover(self.root, ["Main.lean"], executable=Path("/inspector")), report)
        for owners in ({}, {"Main.lean": {"libraries": ["Missing"], "executables": []}},
                       {"Main.lean": {"libraries": [], "executables": []}},
                       {"Main.lean": {"libraries": ["Core", "Core"], "executables": []}}):
            with self.subTest(owners=owners), patch.object(workspace, "_run", return_value=self.response(
                    {**report, "module_owners": owners})):
                with self.assertRaisesRegex(ValueError, "ownership"):
                    workspace.discover(self.root, ["Main.lean"], executable=Path("/inspector"))


class InstalledLeanScopeWorkspaceTests(unittest.TestCase):
    """Use only the already installed 4.34.1 toolchain, with bounded subprocesses."""

    @classmethod
    def setUpClass(cls):
        cls.chain = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan")) / "toolchains/leanprover--lean4---v4.34.1"
        if not all((cls.chain / "bin" / name).is_file() for name in ("lean", "leanc", "lake")):
            raise unittest.SkipTest("requires already installed Lean 4.34.1; never downloads")
        directory = tempfile.TemporaryDirectory(prefix="unity-formalize-scope-native-")
        cls.addClassCleanup(directory.cleanup)
        cls.directory = Path(directory.name).resolve()
        cls.environment = {**os.environ, "PATH": str(cls.chain / "bin") + os.pathsep + os.environ.get("PATH", ""),
                           "LEAN_SYSROOT": str(cls.chain), "LEAN_PATH": ""}
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
        self.write("lean-toolchain", "leanprover/lean4:v4.34.1\n")
        self.write("lakefile.toml", 'name = "scope_fixture"\n'
                   '[[lean_lib]]\nname = "Core"\nsrcDir = "src"\nroots = ["Example"]\n'
                   '[[lean_exe]]\nname = "brokenTool"\nsrcDir = "tools"\nroot = "Broken"\n')
        self.write("src/Example.lean", "import Example.Defs\ntheorem good : True := by trivial\n")
        self.write("src/Example/Defs.lean", "import Example.«odd-name»\ndef value := 1\n")
        self.write("src/Example/odd-name.lean", "def oddValue := 2\n")
        self.write("tools/Broken.lean", "import Example\nthis is deliberately not Lean\n")

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def run_helper(self, *args, good=True):
        command = [str(self.executable), *args]
        if not args or args[0] != "--imports":
            # Match production's installed-Lake environment, which identifies
            # Lake for the standalone native executable without any build.
            command = [str(self.chain / "bin/lake"), "env", *command]
        result = subprocess.run(command, cwd=self.root,
                                env=self.environment, capture_output=True, text=True, timeout=180)
        if good:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_native_owners_keep_valid_library_separate_from_broken_optional_executable(self):
        files = ["src/Example.lean", "src/Example/Defs.lean", "src/Example/odd-name.lean", "tools/Broken.lean"]
        report = self.run_helper(*files)
        self.assertEqual(report["libraries"], ["Core"])
        self.assertEqual(report["module_owners"]["src/Example/Defs.lean"],
                         {"libraries": ["Core"], "executables": []})
        self.assertEqual(report["module_owners"]["tools/Broken.lean"],
                         {"libraries": [], "executables": ["brokenTool"]})
        self.assertEqual(report["modules"]["src/Example/odd-name.lean"], "Example.«odd-name»")
        self.assertFalse((self.root / ".lake/build").exists(), "discovery must not build any module")
        built = subprocess.run([str(self.chain / "bin/lake"), "build", "Core"], cwd=self.root,
                               env=self.environment, capture_output=True, text=True, timeout=90)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.assertFalse((self.root / ".lake/build/lib/lean/Broken.olean").exists())
        broken = subprocess.run([str(self.chain / "bin/lake"), "build", "brokenTool"], cwd=self.root,
                                env=self.environment, capture_output=True, text=True, timeout=90)
        self.assertNotEqual(broken.returncode, 0, "fixture optional executable must genuinely fail")

    def test_native_headers_enable_transitive_library_closure_without_elaboration(self):
        files = ["src/Example.lean", "src/Example/Defs.lean", "src/Example/odd-name.lean", "tools/Broken.lean"]
        imports = self.run_helper("--imports", *files)["imports"]
        self.assertEqual(imports["src/Example.lean"], ["Init", "Example.Defs"])
        self.assertEqual(imports["src/Example/Defs.lean"], ["Init", "Example.«odd-name»"])
        self.assertEqual(imports["src/Example/odd-name.lean"], ["Init"])
        self.assertEqual(imports["tools/Broken.lean"], ["Init", "Example"])
        self.assertFalse((self.root / ".lake").exists(), "header-only mode must not load Lake config or build")

    def test_modern_module_prelude_meta_public_all_and_comments_use_native_parser(self):
        self.write("Modern.lean", '/- import Fake /- nested -/ -/\nmodule\nprelude\n'
                   'public import «hyphen-name».«component.with.dot»\n'
                   'meta import Foo\nimport all Foo\n'
                   'def ignoredBody := "import AlsoFake"\nthis is not elaborated\n')
        imports = self.run_helper("--imports", "Modern.lean")["imports"]
        self.assertEqual(imports, {"Modern.lean": ["«hyphen-name».«component.with.dot»", "Foo"]})
        self.write("Implicit.lean", "module\npublic import Foo\n")
        self.assertEqual(self.run_helper("--imports", "Implicit.lean")["imports"],
                         {"Implicit.lean": ["Init", "Foo"]})

    def test_invalid_header_fails_closed_before_reading_body(self):
        for text in ("import\n", "public import Foo\n", "module\npublic import all Foo\n"):
            with self.subTest(text=text):
                self.write("Invalid.lean", text)
                report = self.run_helper("--imports", "Invalid.lean", good=False)
                self.assertTrue(report["issues"])

    def test_nested_source_roots_with_different_names_for_same_file_are_rejected(self):
        self.write("lakefile.toml", 'name = "ambiguous_fixture"\n'
                   '[[lean_lib]]\nname = "Outer"\nsrcDir = "src"\nroots = ["Nested"]\n'
                   '[[lean_lib]]\nname = "Inner"\nsrcDir = "src/Nested"\nroots = ["Foo"]\n')
        self.write("src/Nested/Foo.lean", "def value := 1\n")
        report = self.run_helper("src/Nested/Foo.lean", good=False)
        self.assertTrue(any("ambiguous" in issue for issue in report["issues"]))

    def test_same_module_name_at_two_source_roots_rejected_even_for_canonical_winner(self):
        self.write("lakefile.toml", 'name = "conflicting_fixture"\n'
                   '[[lean_lib]]\nname = "First"\nsrcDir = "first"\nroots = ["Foo"]\n'
                   '[[lean_lib]]\nname = "Second"\nsrcDir = "second"\nroots = ["Foo"]\n')
        self.write("first/Foo.lean", "def value := 1\n")
        self.write("second/Foo.lean", "def value := 2\n")
        for path in ("first/Foo.lean", "second/Foo.lean"):
            with self.subTest(path=path):
                self.assertTrue(self.run_helper(path, good=False)["issues"])

    def test_identical_multiple_owners_are_reported_without_inventing_module_aliases(self):
        self.write("lakefile.toml", 'name = "shared_fixture"\n'
                   '[[lean_lib]]\nname = "Second"\nsrcDir = "src"\nroots = ["Example"]\n'
                   '[[lean_lib]]\nname = "First"\nsrcDir = "src"\nroots = ["Example"]\n')
        report = self.run_helper("src/Example.lean")
        self.assertEqual(report["libraries"], ["First", "Second"])
        self.assertEqual(report["modules"], {"src/Example.lean": "Example"})
        self.assertEqual(report["module_owners"]["src/Example.lean"],
                         {"libraries": ["First", "Second"], "executables": []})

    def test_native_header_reader_rejects_symlink_escape(self):
        (self.root / "Alias.lean").symlink_to(self.root / "src/Example.lean")
        self.assertTrue(self.run_helper("--imports", "Alias.lean", good=False)["issues"])

    def test_native_lean_configuration_uses_declared_target_names(self):
        (self.root / "lakefile.toml").unlink()
        self.write("lakefile.lean", 'import Lake\nopen Lake DSL\npackage scope_fixture\n'
                   'lean_lib ActualTarget where\n  srcDir := "src"\n  roots := #[`Example]\n'
                   'lean_exe OptionalTool where\n  srcDir := "tools"\n  root := `Broken\n')
        report = self.run_helper("src/Example.lean", "tools/Broken.lean")
        self.assertEqual(report["libraries"], ["ActualTarget"])
        self.assertEqual(report["module_owners"]["tools/Broken.lean"]["executables"], ["OptionalTool"])

    def test_helper_compiles_and_discovers_with_already_installed_lean_428(self):
        chain = self.chain.parent / "leanprover--lean4---v4.28.0"
        if not all((chain / "bin" / name).is_file() for name in ("lean", "leanc", "lake")):
            self.skipTest("requires already installed Lean 4.28.0; never downloads")
        environment = {**self.environment, "PATH": str(chain / "bin") + os.pathsep + os.environ.get("PATH", ""),
                       "LEAN_SYSROOT": str(chain)}
        source = Path(workspace.__file__).with_suffix(".lean")
        binary, generated = self.root / "workspace428", self.root / "workspace428.c"
        self.write("lean-toolchain", "leanprover/lean4:v4.28.0\n")
        commands = ([str(chain / "bin/lean"), "-R", str(source.parent), "-c", str(generated), str(source)],
                    [str(chain / "bin/leanc"), "-o", str(binary), str(generated), "-lLake", "-rdynamic"],
                    [str(chain / "bin/lake"), "env", str(binary), "src/Example.lean", "tools/Broken.lean"],
                    [str(binary), "--imports", "src/Example/Defs.lean"])
        reports = []
        for command in commands:
            result = subprocess.run(command, cwd=self.root, env=environment,
                                    capture_output=True, text=True, timeout=180)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            if command in commands[2:]:
                reports.append(json.loads(result.stdout.strip().splitlines()[-1]))
        self.assertEqual(reports[0]["module_owners"]["tools/Broken.lean"],
                         {"libraries": [], "executables": ["brokenTool"]})
        self.assertEqual(reports[1]["imports"], {"src/Example/Defs.lean": ["Init", "Example.«odd-name»"]})

    def test_real_baseline_library_scope_succeeds_and_all_scope_rejects_broken_executable(self):
        from unity import formalize_project as project

        self.write(".gitignore", ".lake/\n.unity/\n.worktrees/\n")
        # This dependency-free fixture creates its empty manifest before the
        # existing-project capture. No downloader or remote package is needed.
        subprocess.run([str(self.chain / "bin/lake"), "update"], cwd=self.root,
                       env=self.environment, capture_output=True, text=True, check=True, timeout=45)
        for args in (["init", "-q", "--initial-branch=main"],
                     ["config", "user.name", "Formalize Fixture"],
                     ["config", "user.email", "fixture@example.invalid"],
                     ["config", "commit.gpgsign", "false"], ["add", "."], ["commit", "-qm", "fixture"]):
            subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, check=True, timeout=30)
        original = {path.relative_to(self.root).as_posix(): path.read_bytes()
                    for path in self.root.rglob("*.lean") if ".lake" not in path.parts}
        commands = []

        def bounded_native_job(root, command, *, cwd, **kwargs):
            commands.append(command)
            return subprocess.run(command, cwd=cwd, env=self.environment,
                                  capture_output=True, text=True, timeout=180)

        # The process adapter is bounded test plumbing only: all Lake builds,
        # workspace discovery, header parsing and declaration inventory are real.
        with patch.dict(os.environ, self.environment), \
             patch.object(workspace, "_executable", return_value=self.executable), \
             patch.object(workspace.formalize_jobs, "run", side_effect=bounded_native_job):
            with self.assertRaisesRegex(ValueError, "existing project must build"):
                project.capture_baseline(self.root, "good", project_scope="all")
            commands.clear()
            baseline = project.capture_baseline(self.root, "good", project_scope="libraries")
        self.assertTrue(project.baseline_is_valid(baseline))
        self.assertEqual(baseline["verification_scope"]["selected_libraries"], ["Core"])
        self.assertEqual(set(baseline["declarations"]), {"good", "value", "oddValue"})
        self.assertEqual(set(baseline["layout"]["verification_modules"].values()),
                         {"Example", "Example.Defs", "Example.«odd-name»"})
        self.assertIn("tools/Broken.lean", baseline["files"])
        self.assertNotIn("tools/Broken.lean", baseline["layout"]["verification_modules"])
        self.assertTrue(all("+Broken" not in command and "brokenTool" not in command for command in commands))
        self.assertEqual(original, {name: (self.root / name).read_bytes() for name in original})
        clean = subprocess.run(["git", "status", "--porcelain"], cwd=self.root,
                               capture_output=True, text=True, check=True, timeout=30)
        self.assertEqual(clean.stdout, "")


if __name__ == "__main__":
    unittest.main()
