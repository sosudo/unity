"""Autoformalize-only, toolchain-keyed native Lake workspace inspection.

Lake's cached Lean configuration loader needs native helpers and an executable
with interpreter support. Running the companion file through ``lean --run`` is
not sufficient for arbitrary ``lakefile.lean`` configurations.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import artifacts, autoformalize_jobs, autoformalize_native


def _run(root: Path, command: list[str]):
    result = autoformalize_jobs.run(
        root, command, cwd=root, owner="Unity", task_id="workspace",
        serialize_build=True,
    )
    if result.returncode:
        output = "\n".join(part.rstrip() for part in (result.stdout, result.stderr) if part)
        raise ValueError("Lake workspace inspection failed: " + artifacts.preview_text(output, 3000))
    return result


def _executable(root: Path, *, timings: dict | None = None) -> Path:
    return autoformalize_native.executable(
        root, Path(__file__).with_suffix(".lean"), name="workspace",
        link_args=("-lLake",), timings=timings,
    )


def discover(root: Path, files: list[str], *, executable: Path | None = None) -> dict:
    """Map checked project-relative Lean paths using the actual Lake configuration."""
    root = Path(root).resolve()
    executable = executable if executable is not None else _executable(root)
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
