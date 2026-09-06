"""Solve-only native inspectors, cached by helper source and Lean toolchain.

This cache stores executable code, never inspection results. Each invocation
still imports the project's current built modules and reads its configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import tempfile
import time
from pathlib import Path

from . import artifacts, solve_jobs


def _run(root: Path, command: list[str], *, name: str):
    result = solve_jobs.run(
        root, command, cwd=root, owner="Unity", task_id=name,
        serialize_build=True,
    )
    if result.returncode:
        output = "\n".join(part.rstrip() for part in (result.stdout, result.stderr) if part)
        raise ValueError(
            f"Lean {name} inspector preparation failed: " + artifacts.preview_text(output, 3000)
        )
    return result


def _toolchain_identity(root: Path, *, name: str) -> dict:
    version = _run(root, ["lake", "env", "lean", "--version"], name=name).stdout.strip()
    prefix = _run(root, ["lake", "env", "lean", "--print-prefix"], name=name).stdout.strip()
    if not version or not prefix or not Path(prefix).is_dir():
        raise ValueError(f"could not identify the Lean toolchain for {name} inspection")
    return {"version": version, "sysroot": str(Path(prefix).resolve()),
            "platform": platform.system(), "machine": platform.machine()}


def executable(
    root: Path,
    source: Path,
    *,
    name: str,
    link_args: tuple[str, ...] = (),
    timings: dict | None = None,
) -> Path:
    """Prepare an interpreter-capable native helper; optionally report timings."""
    started = time.monotonic()
    if timings is not None:
        timings.update(cache_hit=False, compile_seconds=0.0)
    try:
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
            raise ValueError("invalid native inspector name")
        root = Path(root).resolve()
        source = Path(source).resolve()
        source_bytes = source.read_bytes()
        toolchain = _toolchain_identity(root, name=name)
        interpreter_flags = (
            ["-Wl,--whole-archive", "-lleanmanifest", "-Wl,--no-whole-archive"]
            if os.name == "nt" else ["-rdynamic"]
        )
        # Bump the recipe if the compiler invocation changes in ways not already
        # captured by its explicit arguments below.
        recipe = {"version": 1, "link_args": list(link_args),
                  "interpreter_flags": interpreter_flags, "lean_flags": ["-R", "-c"]}
        key = hashlib.sha256(
            source_bytes + b"\0" + json.dumps(
                {"toolchain": toolchain, "recipe": recipe}, sort_keys=True,
            ).encode()
        ).hexdigest()
        cache = root / ".unity" / "bin" / f"solve-{name}"
        destination = cache / key
        binary = destination / (f"{name}.exe" if os.name == "nt" else name)

        def check_cache_paths():
            # .unity itself may intentionally link to shared run state. The
            # directories and executable we publish within it must not redirect.
            if any(path.is_symlink() for path in (cache.parent, cache, destination, binary)):
                raise ValueError(f"{name} inspector cache must not contain symlinks")

        check_cache_paths()
        if binary.is_file():
            if source.read_bytes() != source_bytes:
                raise ValueError(f"{name} inspector source changed during preparation")
            if timings is not None:
                timings["cache_hit"] = True
            return binary
        cache.mkdir(parents=True, exist_ok=True)
        # Each compiler owns a unique directory. Publish only a completed
        # executable so concurrent controllers never observe a partial hit.
        staging = Path(tempfile.mkdtemp(prefix="building-", dir=cache))
        try:
            generated_c = staging / f"{name}.c"
            built = staging / binary.name
            compile_started = time.monotonic()
            try:
                _run(root, ["lake", "env", "lean", "-R", str(source.parent),
                            "-c", str(generated_c), str(source)], name=name)
                _run(root, ["lake", "env", "leanc", "-o", str(built), str(generated_c),
                            *link_args, *interpreter_flags], name=name)
            finally:
                if timings is not None:
                    timings["compile_seconds"] = time.monotonic() - compile_started
            if not built.is_file():
                raise ValueError(f"{name} inspector compilation produced no executable")
            if source.read_bytes() != source_bytes or _toolchain_identity(root, name=name) != toolchain:
                raise ValueError(f"{name} inspector source or toolchain changed during compilation")
            check_cache_paths()
            destination.mkdir(parents=True, exist_ok=True)
            check_cache_paths()
            os.replace(built, binary)
        finally:
            # Exclusively this compiler's mkdtemp directory, never project input.
            shutil.rmtree(staging)
        return binary
    finally:
        if timings is not None:
            timings["preparation_seconds"] = time.monotonic() - started
