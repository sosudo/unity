"""Real declaration-level compiler progress and final preservation, no models.

The same installed toolchain is used on both sides so this fixture needs no
downloads. Two deliberate target-only type errors model compiler failures after
an upgrade. Lifecycle transport/merge concurrency is covered separately; here
the controller-state updates are explicit fixture scaffolding around real Git,
Lake, compact inventory, compiler planning, candidate checks and final native
preservation checks.
"""
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from unity import bump_bootstrap as bootstrap
from unity import bump_contract as contract
from unity import bump_migration as migration
from unity import bump_preparation as preparation
from unity import bump_state as state
from unity.bump_input import bump_paths
from unity.config import Paths


class InstalledDeclarationMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        explicit = os.environ.get("UNITY_TEST_LEAN_TOOLCHAIN")
        elan = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan"))
        candidates = ([Path(explicit)] if explicit else [
            elan / "toolchains" / ("leanprover--lean4---v" + version)
            for version in ("4.34.1", "4.34.0", "4.33.1", "4.28.0")])
        cls.chain = next((path.resolve() for path in candidates
                          if all((path / "bin" / name).is_file() for name in ("lake", "lean", "leanc"))), None)
        if cls.chain is None:
            raise unittest.SkipTest("requires an already installed Lean; never downloads")
        cls.environment = {**os.environ, "PATH": str(cls.chain / "bin") + os.pathsep + os.environ.get("PATH", ""),
                           "LEAN_SYSROOT": str(cls.chain), "LEAN_PATH": "", "UNITY_AGENT_NAME": ""}
        cls.toolchain = "leanprover/lean4:v" + cls.chain.name.rsplit("---v", 1)[-1]
        cls.target_toolchain = cls.toolchain

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="unity-bump-declarations-")
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, self.environment, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.original = "def first : Nat := 1\n\ndef second : Nat := 2\n\ndef third : Nat := first + 1\n"
        custom_build_dir = getattr(self, "custom_build_dir", None)
        (self.source / ".gitignore").write_text(".lake/\n.unity/\n.worktrees/\n"
            + (custom_build_dir + "/\n" if custom_build_dir else ""))
        (self.source / "lean-toolchain").write_text(self.toolchain + "\n")
        root_module = "Root" if getattr(self, "import_only_root", False) else "Fixture"
        (self.source / "lakefile.toml").write_text('name = "declaration_fixture"\ndefaultTargets = ["' + root_module
            + '"]\n' + ('buildDir = "' + custom_build_dir + '"\n' if custom_build_dir else "")
            + '[[lean_lib]]\nname = "Fixture"\n' + ('[[lean_lib]]\nname = "Root"\n' if root_module == "Root" else ""))
        (self.source / "Fixture.lean").write_text(self.original)
        if root_module == "Root":
            (self.source / "Root.lean").write_text("import Fixture\n")
        self.command(self.source, ["lake", "update"])
        if custom_build_dir:
            # Populate the real native build output before bootstrap. The old
            # preparation map incorrectly classified these bytes as inputs.
            self.command(self.source, ["lake", "build"])
            self.assertTrue(list((self.source / custom_build_dir).rglob("*.olean")))
        for args in (("init", "-q", "--initial-branch=main"), ("config", "user.name", "Bump Native Test"),
                     ("config", "user.email", "fixture@example.invalid"), ("config", "commit.gpgsign", "false"),
                     ("add", "."), ("commit", "-qm", "immutable declaration fixture")):
            self.command(self.source, ["git", *args])
        (self.source / ".unity").mkdir()
        (self.source / ".unity" / "UNITY.md").write_text("Preserve the original Lean declarations.\n")
        (self.source / ".unity" / "agents.yaml").write_text("agents: []\n")
        self.paths = bootstrap.prepare(bump_paths(Paths.from_unity_dir(self.source / ".unity")),
                                       self.target_toolchain, {}, architect="off")
        self.target = self.paths.project_root
        self.assertEqual((self.source / "lean-toolchain").read_text().strip(), self.toolchain)
        self.assertEqual((self.target / "lean-toolchain").read_text().strip(), self.target_toolchain)

    def command(self, root, args, *, good=True):
        result = subprocess.run(args, cwd=root, env=self.environment, capture_output=True, text=True, timeout=240)
        if good:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def commit(self, message, *, path="Fixture.lean"):
        self.command(self.target, ["git", "add", "--", path])
        self.command(self.target, ["git", "commit", "-qm", message])
        return self.command(self.target, ["git", "rev-parse", "HEAD"]).stdout.strip()

    def checked_candidate(self, declaration, updated, *, path="Fixture.lean", task_id=None):
        current = state.load_state(self.paths.forum)
        task = (current["formal_tasks"][task_id] if task_id else
                next(row for row in current["formal_tasks"].values() if row["lean_decl"] == declaration))
        base = self.command(self.target, ["git", "rev-parse", "HEAD"]).stdout.strip()
        branch = self.command(self.target, ["git", "branch", "--show-current"]).stdout.strip()
        self.command(self.target, ["git", "switch", "-c", "candidate-" + (declaration or "command")])
        (self.target / path).write_text(updated)
        candidate_sha = self.commit("candidate " + (declaration or "command"), path=path)
        self.command(self.target, ["git", "switch", branch])
        (self.target / path).write_text(updated)
        modules = sorted(current["project_baseline"]["migration"]["selected_modules"])
        built = self.command(self.target, ["lake", "--rehash", "build", *["+" + module for module in modules]], good=False)
        build = {"returncode": built.returncode, "output": built.stdout + "\n" + built.stderr}
        candidate = {"task_id": task["task_id"], "base_main_sha": base, "commit_sha": candidate_sha,
                     "changed_paths": [path], "outputs": [{"declaration": declaration, "file": path}] if declaration else []}
        receipt = migration.verify_candidate(self.target, current["formalization"]["contract"], task, candidate,
            formal_tasks=list(current["formal_tasks"].values()), build=build, layout=current["project_baseline"]["layout"])
        self.assertEqual(receipt["status"], "passed", receipt["issues"])
        self.assertTrue(receipt["native_pending"])
        self.assertEqual(receipt["verified_targets"], {})
        integrated = self.commit("integrate " + (declaration or "command"), path=path)
        # This fixture directly records the validated integration. It does not
        # claim coverage of worker transport or the separate merge CAS tests.
        with state.transaction(self.paths.forum) as live:
            live["formalization"]["main_sha"] = integrated
            live["formalization"]["contract"] = deepcopy(receipt["proposed_contract"])
            row = live["formal_tasks"][task["task_id"]]
            row.update(status="complete", outputs=candidate["outputs"],
                       verification={"status": "provisional", "native_pending": True})
        refreshed = bootstrap.refresh(self.paths, build=build)
        return receipt, build, refreshed

    def test_two_declarations_same_file_partial_repair_then_full_native_gate(self):
        initial = state.load_state(self.paths.forum)
        self.assertEqual(initial["formal_tasks"], {})
        original_index = initial["project_baseline"]["migration"]["original_index"]
        self.assertEqual({row["display_name"] for row in original_index["occurrences"].values()},
                         {"first", "second", "third"})
        self.assertEqual(len(initial["formalization"]["requirements"]), 3)
        broken = self.original.replace(": Nat := 1", ": Nat := True").replace(": Nat := 2", ": Nat := False")
        (self.target / "Fixture.lean").write_text(broken)
        broken_sha = self.commit("simulate target compiler incompatibilities")
        with state.transaction(self.paths.forum) as live:
            live["formalization"]["main_sha"] = broken_sha
        planned = bootstrap.refresh(self.paths)
        self.assertEqual({row["lean_decl"] for row in planned["formal_tasks"].values()}, {"first", "second"})
        self.assertTrue(all(row["migration"]["kind"] == "declaration" for row in planned["formal_tasks"].values()))
        self.assertTrue(all(len(row["migration"]["original_ids"]) == 1 for row in planned["formal_tasks"].values()))
        first, partial_build, partial = self.checked_candidate("first", broken.replace(": Nat := True", ": Nat := 1"))
        self.assertNotEqual(partial_build["returncode"], 0, "sibling declaration must still fail")
        self.assertEqual(first["mode"], "diagnostic_repair")
        self.assertEqual({row["lean_decl"]: row["status"] for row in partial["formal_tasks"].values()},
                         {"first": "complete", "second": "pending"})
        rejected = migration.verify_final(self.paths, partial)
        self.assertFalse(rejected["passed"])
        self.assertFalse(rejected["migration_review"]["native_complete"])
        _, final_build, repaired = self.checked_candidate("second", self.original)
        self.assertEqual(final_build["returncode"], 0, final_build["output"])
        final = migration.verify_final(self.paths, repaired)
        self.assertTrue(final["passed"], final["issues"])
        self.assertTrue(final["migration_review"]["native_complete"])
        self.assertEqual(set(final["migration_review"]["verified_occurrences"]), set(original_index["occurrences"]))
        migration.validate_native_snapshot(repaired, final)
        self.assertNotEqual(repaired["formalization"]["status"], "accepted", "native checking is not independent critic acceptance")
        self.assertEqual((self.source / "Fixture.lean").read_text(), self.original)


class InstalledCustomLayoutMigrationTests(InstalledDeclarationMigrationTests):
    custom_build_dir = "generated/build"
    test_two_declarations_same_file_partial_repair_then_full_native_gate = None

    def test_native_custom_build_directory_is_preserved_through_bootstrap_and_final_gate(self):
        current = state.load_state(self.paths.forum)
        baseline = current["project_baseline"]
        preserved = baseline["migration"]
        self.assertEqual(preserved["scope"]["build_dir"], self.custom_build_dir)
        self.assertEqual(baseline["layout"]["build_dir"], self.custom_build_dir)
        self.assertEqual(preserved["selected_modules"], {"Fixture": "Fixture.lean"})
        self.assertEqual(current["formal_tasks"], {})
        self.assertFalse(any(path.startswith(self.custom_build_dir + "/") for path in preserved["original_files"]))
        self.assertEqual(preparation.source_files(self.source, build_dir=self.custom_build_dir),
                         preserved["original_files"])
        final = migration.verify_final(self.paths, current)
        self.assertTrue(final["passed"], final["issues"])
        self.assertTrue(final["migration_review"]["native_complete"])
        migration.validate_native_snapshot(current, final)
        self.assertEqual((self.source / "Fixture.lean").read_text(), self.original)


class InstalledVersionPairMigrationTests(InstalledDeclarationMigrationTests):
    """Exercise the actual requested old/new native versions, if installed."""
    @classmethod
    def setUpClass(cls):
        elan = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan"))
        old = elan / "toolchains" / "leanprover--lean4---v4.28.0-rc1"
        new = elan / "toolchains" / "leanprover--lean4---v4.34.1"
        if (not (elan / "bin/lake").is_file()
                or any(not (root / "bin" / binary).is_file()
                       for root in (old, new) for binary in ("lake", "lean", "leanc"))):
            raise unittest.SkipTest("requires already installed Lean 4.28.0-rc1 and 4.34.1; never downloads")
        cls.chain = new.resolve()
        cls.environment = {**os.environ, "PATH": str(elan / "bin") + os.pathsep + os.environ.get("PATH", ""),
                           "LEAN_PATH": "", "UNITY_AGENT_NAME": ""}
        cls.environment.pop("LEAN_SYSROOT", None)
        cls.environment.pop("ELAN_TOOLCHAIN", None)
        cls.toolchain = "leanprover/lean4:v4.28.0-rc1"
        cls.target_toolchain = "leanprover/lean4:v4.34.1"


class InstalledEmptyCommandMigrationTests(InstalledDeclarationMigrationTests):
    """A real import-only root receives a line task, never a module repair task."""
    import_only_root = True
    test_two_declarations_same_file_partial_repair_then_full_native_gate = None

    def test_empty_original_module_command_repair_keeps_empty_native_receipt(self):
        initial = state.load_state(self.paths.forum)
        index = initial["project_baseline"]["migration"]["original_index"]
        self.assertEqual(index["modules"]["Root"]["occurrence_ids"], [])
        commands = bootstrap.bump_planner.empty_module_commands(index)
        command_id = next(key for key, row in commands.items() if row["module"] == "Root")
        self.assertEqual(len(initial["formalization"]["requirements"]), 4)
        self.assertEqual(initial["formal_tasks"], {})
        (self.target / "Root.lean").write_text("import MissingModule\n")
        main_sha = self.commit("simulate target import incompatibility", path="Root.lean")
        with state.transaction(self.paths.forum) as live:
            live["formalization"]["main_sha"] = main_sha
        planned = bootstrap.refresh(self.paths)
        self.assertEqual(len(planned["formal_tasks"]), 1)
        task = next(iter(planned["formal_tasks"].values()))
        self.assertEqual(task["migration"]["kind"], "command")
        self.assertEqual(task["migration"]["original_ids"], [])
        self.assertEqual(task["migration"]["command_obligation"], command_id)
        self.assertEqual(task["migration"]["command_line"], 1)
        _, built, repaired = self.checked_candidate("", "import Fixture\n", path="Root.lean", task_id=task["task_id"])
        self.assertEqual(built["returncode"], 0, built["output"])
        final = migration.verify_final(self.paths, repaired)
        self.assertTrue(final["passed"], final["issues"])
        self.assertEqual(final["migration_review"]["empty_module_commands"], commands)
        self.assertEqual({(row["module"], row["side"]) for row in final["migration_review"]["native_reports"]},
                         {(module, side) for module in ("Root", "Fixture") for side in ("original", "target")})
        migration.validate_native_snapshot(repaired, final)
        self.assertEqual((self.source / "Root.lean").read_text(), "import Fixture\n")
