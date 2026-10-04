"""Immutable Bump source/scope identity using temporary documents only."""

import hashlib
from pathlib import Path
import tempfile
import unittest

from unity import artifacts
from unity.config import Paths
from unity.bump_input import (
    bump_paths, require_source_matches, scope_bytes, snapshot_sources, source_matches,
)


class BumpInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = bump_paths(Paths.from_unity_dir(Path(temporary.name) / ".unity"))
        self.source = self.paths.unity / "source"
        self.source.mkdir(parents=True)
        self.paper = self.source / "paper.md"
        self.paper.write_text("Theorem: supplied result. Proof: supplied argument.\n")
        self.paths.unity_md.write_text("# Scope\nBump the selected result.\n")

    def bind(self):
        return {"input_source": snapshot_sources(self.paths),
                "problem_sha256": hashlib.sha256(scope_bytes(self.paths)).hexdigest()}

    def test_sources_are_byte_preserving_and_artifacts_are_exact(self):
        binary = self.source / "paper.pdf"
        binary.write_bytes(b"%PDF-1.7\n\xff\x00fixture")
        before = {path.name: path.read_bytes() for path in self.source.iterdir()}
        state = self.bind()
        for ref in state["input_source"]["source_refs"]:
            name = Path(ref["path"]).name
            self.assertEqual(artifacts.artifact_bytes(self.paths.artifacts, ref["artifact_id"]), before[name])
            self.assertEqual((self.source / name).read_bytes(), before[name])
        require_source_matches(self.paths, state)

    def test_only_exact_state_section_is_excluded(self):
        source = (b"# Scope\r\nKeep this.\r\n## State\r\nMutable progress.\r\n"
                  b"### Details\r\nAlso mutable.\r\n## Requirements\r\nKeep that.\r\n")
        self.paths.unity_md.write_bytes(source)
        self.assertEqual(scope_bytes(self.paths),
                         b"# Scope\r\nKeep this.\r\n## Requirements\r\nKeep that.\r\n")
        state = self.bind()
        self.paths.unity_md.write_bytes(source.replace(b"Mutable progress.", b"Different progress."))
        require_source_matches(self.paths, state)

    def test_nonexplicit_state_headings_remain_frozen(self):
        for heading in ("# State", "### State", "## state", "## State notes", "  ## State", "> ## State"):
            with self.subTest(heading=heading):
                content = (heading + "\nThis is an instruction.\n").encode()
                self.paths.unity_md.write_bytes(content)
                self.assertEqual(scope_bytes(self.paths), content)
                state = self.bind()
                self.paths.unity_md.write_bytes(content.replace(b"an instruction", b"changed instructions"))
                with self.assertRaisesRegex(ValueError, "changed;.*separate project copy"):
                    require_source_matches(self.paths, state)

    def test_state_heading_in_fenced_examples_is_frozen(self):
        for fence in ("```", "~~~", "````"):
            with self.subTest(fence=fence):
                content = f"# Scope\n{fence}markdown\n## State\nLiteral instructions.\n{fence}\n".encode()
                self.paths.unity_md.write_bytes(content)
                self.assertEqual(scope_bytes(self.paths), content)
                state = self.bind()
                self.paths.unity_md.write_bytes(content.replace(b"Literal", b"Changed"))
                with self.assertRaisesRegex(ValueError, "changed;.*separate project copy"):
                    require_source_matches(self.paths, state)

    def test_top_level_heading_ends_mutable_state_section(self):
        self.paths.unity_md.write_text("## State\nProgress.\n# Constraints\nDo not change declarations.\n")
        self.assertEqual(scope_bytes(self.paths), b"# Constraints\nDo not change declarations.\n")
        state = self.bind()
        self.paths.unity_md.write_text("## State\nOther progress.\n# Constraints\nChange declarations.\n")
        with self.assertRaisesRegex(ValueError, "changed;.*separate project copy"):
            require_source_matches(self.paths, state)

    def test_byte_edits_renames_additions_and_removal_reject_resume(self):
        state = self.bind()
        original = self.paper.read_bytes()
        self.paper.write_bytes(original + b"Changed argument.\n")
        with self.assertRaisesRegex(ValueError, "changed;.*separate project copy"):
            require_source_matches(self.paths, state)
        self.paper.write_bytes(original)
        renamed = self.source / "renamed.md"
        self.paper.rename(renamed)
        self.assertFalse(source_matches(self.paths, state))
        renamed.rename(self.paper)
        extra = self.source / "extra.md"
        extra.write_text("New source")
        self.assertFalse(source_matches(self.paths, state))
        extra.unlink()
        require_source_matches(self.paths, state)
        self.paper.unlink()
        self.assertFalse(source_matches(self.paths, state))

    def test_missing_scope_refuses_resume(self):
        state = self.bind()
        self.paths.unity_md.unlink()
        with self.assertRaisesRegex(ValueError, "original UNITY.md"):
            require_source_matches(self.paths, state)

    def test_symlink_sources_are_not_followed(self):
        outside = self.paths.project_root / "outside.md"
        outside.write_text("Not a supplied document")
        (self.source / "link.md").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            snapshot_sources(self.paths)


if __name__ == "__main__":
    unittest.main()
