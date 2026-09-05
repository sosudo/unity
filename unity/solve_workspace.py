"""Solve-only, toolchain-keyed native Lake workspace inspection.

Lake's cached Lean configuration loader needs native helpers and an executable
with interpreter support. Running the companion file through ``lean --run`` is
not sufficient for arbitrary ``lakefile.lean`` configurations.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tempfile
from pathlib import Path

from . import artifacts, solve_jobs


def _run(root: Path, command: list[str]):
    result = solve_jobs.run(
        root, command, cwd=root, owner="Unity", task_id="workspace",
        serialize_build=True,
    )
    if result.returncode:
        output = "\n".join(part.rstrip() for part in (result.stdout, result.stderr) if part)
        raise ValueError("Lake workspace inspection failed: " + artifacts.preview_text(output, 3000))
    return result


def _toolchain_identity(root: Path) -> dict:
    version = _run(root, ["lake", "env", "lean", "--version"]).stdout.strip()
    prefix = _run(root, ["lake", "env", "lean", "--print-prefix"]).stdout.strip()
    if not version or not prefix or not Path(prefix).is_dir():
        raise ValueError("could not identify the Lean toolchain for workspace inspection")
    return {"version": version, "sysroot": str(Path(prefix).resolve()),
            "platform": platform.system(), "machine": platform.machine()}


def _executable(root: Path) -> Path:
    source = Path(__file__).with_suffix(".lean")
    source_bytes = source.read_bytes()
    toolchain = _toolchain_identity(root)
    key = hashlib.sha256(
        source_bytes + b"\0" + json.dumps(toolchain, sort_keys=True).encode()
    ).hexdigest()
    cache = root / ".unity" / "bin" / "solve-workspace"
    destination = cache / key
    executable = destination / ("workspace.exe" if os.name == "nt" else "workspace")
    if destination.is_symlink() or executable.is_symlink():
        raise ValueError("workspace inspector cache must not contain symlinks")
    if executable.is_file():
        return executable
    cache.mkdir(parents=True, exist_ok=True)
    # Each compiler owns a unique directory. Publish only a completed executable,
    # so simultaneous controller processes can never observe a partial cache hit.
    staging = Path(tempfile.mkdtemp(prefix="building-", dir=cache))
    try:
        generated_c = staging / "workspace.c"
        built = staging / executable.name
        _run(root, ["lake", "env", "lean", "-R", str(source.parent),
                    "-c", str(generated_c), str(source)])
        interpreter_flags = (
            ["-Wl,--whole-archive", "-lleanmanifest", "-Wl,--no-whole-archive"]
            if os.name == "nt" else ["-rdynamic"]
        )
        _run(root, ["lake", "env", "leanc", "-o", str(built), str(generated_c),
                    "-lLake", *interpreter_flags])
        if not built.is_file():
            raise ValueError("workspace inspector compilation produced no executable")
        if source.read_bytes() != source_bytes or _toolchain_identity(root) != toolchain:
            raise ValueError("workspace inspector source or toolchain changed during compilation")
        destination.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink() or executable.is_symlink():
            raise ValueError("workspace inspector cache changed during compilation")
        os.replace(built, executable)
    finally:
        # This is exclusively our mkdtemp directory, never project source/cache.
        shutil.rmtree(staging)
    return executable


def discover(root: Path, files: list[str]) -> dict:
    """Map checked project-relative Lean paths using the actual Lake configuration."""
    root = Path(root).resolve()
    executable = _executable(root)
    result = _run(root, ["lake", "env", str(executable), *files])
    try:
        report = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise ValueError("Lake workspace inspector did not return JSON") from exc
    if (not isinstance(report, dict) or not isinstance(report.get("modules"), dict)
            or not isinstance(report.get("traces"), dict)
            or not isinstance(report.get("build_dir"), str)
            or not isinstance(report.get("source_roots"), list)
            or not isinstance(report.get("unmatched"), list)
            or not isinstance(report.get("issues"), list)):
        raise ValueError("Lake workspace inspector returned an incomplete report")
    if report["issues"]:
        raise ValueError("Lake workspace inspection failed: " + "; ".join(map(str, report["issues"])))
    return report
