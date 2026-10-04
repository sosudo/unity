"""Target compiler diagnostics used only to discover declaration repair work."""
from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
import re

from . import artifacts, bump_jobs, bump_workspace
from .bump_inventory import digest

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_LOCATION = re.compile(r"^(?:(error|warning|info):\s*)?(.+?\.lean):(\d+):(\d+):\s*(?:(error|warning|info):\s*)?(.*)$")


def scheduling_imports(root: Path, index: dict) -> dict:
    """Current native headers, with explicit parser-only scheduling fallback.

    The copied native helper distinguishes parser-invalid headers from file,
    compiler and loader failures in its structured issues list. Only parser
    failures reuse original scheduling edges, and final acceptance never uses
    this record in place of a successful fresh build/native inspection.
    """
    paths = sorted(row["path"] for row in index["modules"].values())
    checked = bump_workspace._checked_files(root, paths)
    executable = bump_workspace._executable(root)
    result = bump_jobs.run(root, ["lake", "env", str(executable), "--imports", *checked], cwd=root,
                          owner="Unity", task_id="migration-imports", serialize_build=True)
    try:
        report = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise ValueError("native current-import inspection did not return structured evidence") from exc
    if not isinstance(report, dict):
        raise ValueError("native current-import inspection returned non-object evidence")
    imports, issues = report.get("imports"), report.get("issues")
    if not isinstance(imports, dict) or not isinstance(issues, list):
        raise ValueError("native current-import inspection returned incomplete evidence")
    invalid = set()
    for issue in issues:
        path = next((path for path in paths if isinstance(issue, str)
                     and issue.startswith("invalid Lean import header in " + path + ": ")), None)
        if path is None:
            raise ValueError("native current-import inspection failed outside header parsing")
        invalid.add(path)
    if (result.returncode not in {0, 1} or bool(result.returncode) != bool(issues)
            or set(imports) != set(paths) - invalid
            or any(not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names)
                   or len(set(names)) != len(names) for names in imports.values())):
        raise ValueError("native current-import inspection returned inconsistent evidence")
    return {"current_imports": {module: imports.get(row["path"], row["imports"])
                               for module, row in index["modules"].items()},
            "current_imports_complete": not invalid,
            "scheduling_header_fallbacks": sorted(invalid)}


def parse_diagnostics(output: str, root: Path, *, source_sha256: str, returncode: int) -> dict:
    rows = []
    for line in _ANSI.sub("", output).splitlines():
        match = _LOCATION.match(line)
        if match:
            prefix, filename, line_number, column, middle, message = match.groups()
            path = Path(filename)
            if path.is_absolute():
                try:
                    path = path.relative_to(root)
                except ValueError:
                    path = None
            if path is not None and ".." in path.parts:
                path = None
            severity = middle or prefix or "error"
            lower = message.casefold()
            kind = "import" if any(term in lower for term in ("unknown module", "object file", "unknown package")) else "declaration"
            row = {"path": path.as_posix() if path is not None else None, "line": int(line_number),
                   "column": int(column), "severity": severity, "kind": kind, "message": message}
            row["id"] = "diag-" + digest([source_sha256, row])
            rows.append(row)
        elif line.startswith(("error:", "fatal error:", "PANIC", "uncaught exception")):
            if line.strip() in {"error: build failed", "error: Lean exited with code 1"}:
                continue
            row = {"path": None, "line": None, "column": None, "severity": "error",
                   "kind": "environment", "message": line}
            row["id"] = "diag-" + digest([source_sha256, row])
            rows.append(row)
    if returncode and not any(row["severity"] == "error" for row in rows):
        rows.append({"id": "diag-" + digest([source_sha256, returncode, output]), "path": None,
                     "line": None, "column": None, "severity": "error", "kind": "environment",
                     "message": "Build failed without a located diagnostic; inspect the preserved compiler log."})
    return {"diagnostics": rows, "unmapped_error_count": sum(
        row["severity"] == "error" and row["path"] is None for row in rows)}


def original_line(original: str, current: str, line: int) -> int:
    """Map a current compiler location back through edits to the original input.

    Changed regions map into their original region; exact unchanged lines retain
    their precise identity. Native final validation never relies on this hint.
    """
    before, after = original.splitlines(), current.splitlines()
    at = max(0, line - 1)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes():
        if j1 <= at < j2:
            return (i1 + at - j1 + 1) if tag == "equal" else min(max(1, i1 + 1), max(1, i2))
    return max(1, len(before))


def occurrence_ids_at(index: dict, path: str, line: int, column: int = 0) -> list[str]:
    matches = []
    for key, row in index["occurrences"].items():
        span = row.get("range")
        if row["path"] != path or not span:
            continue
        if (span["start_line"] <= line <= span["end_line"]
                and (line != span["start_line"] or column >= span["start_column"])
                and (line != span["end_line"] or column < span["end_column"])):
            matches.append(((span["end_line"] - span["start_line"], span["end_column"] - span["start_column"]),
                            row.get("is_internal", False), key))
    if not matches:
        return []
    smallest = min((size, internal) for size, internal, _ in matches)
    return sorted(key for size, internal, key in matches if (size, internal) == smallest)


def diagnostics_from_output(root: Path, index: dict, output: str, returncode: int,
                            *, original_root: Path | None = None, build_dir: str | None = None) -> dict:
    from . import bump_preparation
    root = Path(root).resolve()
    files = bump_preparation.source_files(root, build_dir=build_dir)
    parsed = parse_diagnostics(output, root, source_sha256=digest(files), returncode=returncode)
    if original_root is None:
        try:
            saved = json.loads((root / ".unity/bump/migration.json").read_text())
            original_root = Path(saved["original_root"])
        except (OSError, ValueError, KeyError):
            original_root = None
    for row in parsed["diagnostics"]:
        path = row["path"]
        line = row["line"]
        if path and line:
            if original_root is not None and (Path(original_root) / path).is_file() and (root / path).is_file():
                line = original_line((Path(original_root) / path).read_text(), (root / path).read_text(), line)
            row["original_line"] = line
            row["occurrence_ids"] = occurrence_ids_at(index, path, line, row["column"])
        else:
            row["occurrence_ids"] = []
    errors = [row for row in parsed["diagnostics"] if row["severity"] == "error"]
    return {**parsed, "errors": errors, "unmapped": [row for row in errors if not row["path"]],
            "source_sha256": digest(files), "source_files": files,
            "passed": returncode == 0, "returncode": returncode,
            "blocked_modules": sorted({module for module, entry in index["modules"].items()
                                       if any(row["kind"] == "import" and row["path"] == entry["path"] for row in errors)})}


def collect_build_diagnostics(target_root: Path, index: dict, *, artifact_dir: Path,
                              original_root: Path | None = None, scope: dict | None = None) -> dict:
    from . import bump_preparation
    root = Path(target_root).resolve()
    build_dir = scope["build_dir"] if scope is not None else None
    files = bump_preparation.source_files(root, build_dir=build_dir)
    source_sha = digest(files)
    # Native default targets define the normal build. Explicit selected module
    # facets also cover local support files in that frozen import closure.
    modules = sorted(index["modules"])
    command = ["lake", "--rehash", "build", *["+" + module for module in modules]]
    result = bump_jobs.run(root, command, cwd=root, task_id="migration-diagnostics", serialize_build=True)
    output = result.stdout + "\n" + result.stderr
    record = artifacts.store_text(artifact_dir, output, kind="bump_build_diagnostics", producer="Unity")
    parsed = diagnostics_from_output(root, index, output, result.returncode,
                                     original_root=original_root, build_dir=build_dir)
    if bump_preparation.source_files(root, build_dir=build_dir) != files:
        raise ValueError("target source changed while collecting compiler diagnostics")
    return {**parsed, "passed": result.returncode == 0, "returncode": result.returncode,
            "source_sha256": source_sha, "source_files": files,
            "log_ref": {"artifact_id": record["artifact_id"], "sha256": record["sha256"]},
            "command": command, "not_an_acceptance_check": True}
