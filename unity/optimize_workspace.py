"""Optimize-only, toolchain-keyed native Lake workspace inspection.

Lake's cached Lean configuration loader needs native helpers and an executable
with interpreter support. Running the companion file through ``lean --run`` is
not sufficient for arbitrary ``lakefile.lean`` configurations.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import artifacts, optimize_jobs, optimize_native


def checked_lake_args(values=()) -> list[str]:
    """Only explicit package options, never command/cwd/toolchain overrides."""
    result = list(values)
    if any(not isinstance(value, str) or not re.fullmatch(r"-K[A-Za-z_][A-Za-z0-9_.-]*=[^\x00\r\n]*", value)
           for value in result):
        raise ValueError("Optimize Lake arguments must be explicit -Kkey=value options")
    keys = [value.partition("=")[0] for value in result]
    if len(set(keys)) != len(keys):
        raise ValueError("Optimize Lake configuration options must have unique keys")
    return result


def _checked_files(root: Path, files: list[str]) -> list[str]:
    """Reject redirection and ambiguous aliases before starting a native job."""
    checked = []
    for name in files:
        if not isinstance(name, str) or not name or "\x00" in name:
            raise ValueError("invalid project source path")
        path = Path(name)
        if (path.is_absolute() or ".." in path.parts or path.suffix != ".lean"
                or path.as_posix() != name):
            raise ValueError(f"invalid project source path: {name}")
        source = root / path
        if (any(part.is_symlink() for part in (source, *source.parents))
                or not source.is_file() or source.resolve() != source):
            raise ValueError(f"project source is missing or noncanonical: {name}")
        if name not in checked:
            checked.append(name)
    return checked


def _report(result, *, action: str) -> dict:
    try:
        report = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise ValueError(f"Lake workspace {action} did not return JSON") from exc
    if not isinstance(report, dict) or not isinstance(report.get("issues"), list):
        raise ValueError(f"Lake workspace {action} returned an incomplete report")
    if report["issues"]:
        raise ValueError(f"Lake workspace {action} failed: " + "; ".join(map(str, report["issues"])))
    return report


def _run(root: Path, command: list[str]):
    result = optimize_jobs.run(
        root, command, cwd=root, owner="Unity", task_id="workspace",
        serialize_build=True,
    )
    if result.returncode:
        output = "\n".join(part.rstrip() for part in (result.stdout, result.stderr) if part)
        raise ValueError("Lake workspace inspection failed: " + artifacts.preview_text(output, 3000))
    return result


def _executable(root: Path, *, timings: dict | None = None, lake_args=()) -> Path:
    return optimize_native.executable(
        root, Path(__file__).with_suffix(".lean"), name="workspace",
        link_args=("-lLake",), timings=timings, lake_args=lake_args,
    )


def discover(root: Path, files: list[str], *, executable: Path | None = None, lake_args=()) -> dict:
    """Map checked project-relative Lean paths using the actual Lake configuration."""
    root = Path(root).resolve()
    files = _checked_files(root, files) if files != ["--layout-only"] else files
    lake_args = checked_lake_args(lake_args)
    executable = executable if executable is not None else _executable(root, lake_args=lake_args)
    options = [part for arg in lake_args for part in ("--lake-option", *arg[2:].split("=", 1))]
    result = _run(root, ["lake", *lake_args, "env", str(executable), *options, *files])
    report = _report(result, action="inspector")
    if (not isinstance(report, dict) or not isinstance(report.get("modules"), dict)
            or not isinstance(report.get("traces"), dict)
            or not isinstance(report.get("build_dir"), str)
            or not isinstance(report.get("source_roots"), list)
            or not isinstance(report.get("module_owners"), dict)
            or not isinstance(report.get("libraries"), list)
            or not isinstance(report.get("unmatched"), list)
            or not isinstance(report.get("issues"), list)):
        raise ValueError("Lake workspace inspector returned an incomplete report")
    if (any(not isinstance(name, str) or not name for name in report["libraries"])
            or len(set(report["libraries"])) != len(report["libraries"])
            or set(report["module_owners"]) != set(report["modules"])):
        raise ValueError("Lake workspace inspector returned inconsistent ownership")
    for name, owners in report["module_owners"].items():
        if (not isinstance(owners, dict) or set(owners) != {"libraries", "executables"}
                or any(not isinstance(owners[key], list)
                       or any(not isinstance(value, str) or not value for value in owners[key])
                       or len(set(owners[key])) != len(owners[key])
                       for key in ("libraries", "executables"))
                or not (owners["libraries"] or owners["executables"])
                or not set(owners["libraries"]).issubset(report["libraries"])):
            raise ValueError(f"Lake workspace inspector returned invalid ownership for {name}")
    # Keep optional default-target detail separate from selected-file ownership;
    # the configured default build still runs regardless of this metadata.
    if "default_modules" in report or "unknown_default_targets" in report:
        defaults = report.get("default_modules")
        unknown = report.get("unknown_default_targets")
        if (not isinstance(defaults, dict)
                or any(not isinstance(p, str) or not isinstance(m, str) or not m
                       for p, m in defaults.items())
                or not isinstance(unknown, list)
                or any(not isinstance(t, str) or not t for t in unknown)):
            raise ValueError("Lake workspace inspector returned invalid default-target coverage")
    return report
