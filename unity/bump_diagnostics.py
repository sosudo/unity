"""Complete file-backed compiler diagnostics and bounded planning records.

Diagnostics are work discovery, never a proof or preservation verdict. Unknown
formats remain explicit unmapped errors; a missing parsed error is not success.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid

from . import artifacts, bump_migration_project as project
from .bump_inventory import digest, validate_index, read_native_imports

_LOCATION = re.compile(r"^(?:(error|warning|info):\s*)?(.+?\.lean):(\d+):(\d+):\s*(?:(error|warning|info):\s*)?(.*)$")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MAX_LINE = 1024 * 1024
_MAX_RECORDS = 100000


def _missing_import_failure(result) -> bool:
    if result.returncode != 1:
        return False
    output = _ANSI.sub("", result.stderr + "\n" + result.stdout).casefold()
    if any(marker in output for marker in ("panic", "uncaught exception", "segmentation fault", "traceback",
                                            "permission denied", "no space left", "resource temporarily unavailable")):
        return False
    for line in output.splitlines():
        line = re.sub(r"^.*?\.lean:\d+:\d+:\s*", "", line.strip())
        if line.startswith("error:"):
            line = line[6:].lstrip()
        if (line.startswith("unknown module prefix ") or line.startswith("unknown module ")
                or (line.startswith("object file ") and "does not exist" in line)):
            return True
    return False


def _sha_file(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def store_log_artifact(artifact_root: Path, source: Path) -> dict:
    """Store full exact bytes without constructing a giant Python string."""
    source, artifact_root = Path(source), Path(artifact_root)
    before = source.stat()
    sha = _sha_file(source)
    record = {"artifact_id": "artifact-" + uuid.uuid4().hex[:12], "sha256": sha,
        "kind": "bump_build_diagnostics", "producer": "Unity", "source": "",
        "bytes": before.st_size, "lines": None, "created_at": time.time(),
        "metadata": {"complete_bytes": True, "encoding": "compiler-output-bytes"}}
    with artifacts._store_lock(artifact_root):
        blobs, records = artifacts._layout(artifact_root)
        destination = blobs / sha
        if not destination.exists():
            descriptor, staging = tempfile.mkstemp(prefix=".bump-log-", dir=blobs)
            try:
                with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_stream:
                    shutil.copyfileobj(input_stream, output, 1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                if _sha_file(Path(staging)) != sha:
                    raise ValueError("diagnostic output changed during artifact publication")
                os.replace(staging, destination)
            finally:
                Path(staging).unlink(missing_ok=True)
        elif destination.is_symlink() or _sha_file(destination) != sha:
            raise ValueError("existing diagnostic artifact bytes changed")
        after = source.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("diagnostic output changed during capture")
        artifacts._atomic_write(records / (record["artifact_id"] + ".json"),
                                (json.dumps(record, sort_keys=True) + "\n").encode())
    return {"artifact_id": record["artifact_id"], "sha256": sha}


def parse_diagnostics(log_path: Path, root: Path, *, source_sha256: str, returncode: int) -> dict:
    """Parse every line, preserving byte offsets; never treat parsing as build success."""
    root, log_path = Path(root).resolve(), Path(log_path)
    rows, offset, unparsed = [], 0, 0
    with log_path.open("rb") as stream:
        while True:
            raw = stream.readline(_MAX_LINE + 1)
            if not raw:
                break
            if len(raw) > _MAX_LINE:
                raise ValueError("compiler diagnostic line exceeds explicit parser bound; full log preserved")
            text = _ANSI.sub("", raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            match = _LOCATION.match(text)
            if match:
                prefix, filename, line, column, middle, message = match.groups()
                location = Path(filename)
                if location.is_absolute():
                    try:
                        filename = location.resolve().relative_to(root).as_posix()
                    except ValueError:
                        filename = ""
                elif ".." in location.parts:
                    filename = ""
                else:
                    filename = location.as_posix()
                severity = middle or prefix or "error"
                lower = message.casefold()
                kind = "import" if any(term in lower for term in ("unknown module", "object file", "does not exist", "unknown package")) else (
                    "syntax" if any(term in lower for term in ("unexpected token", "expected token", "unterminated", "invalid syntax")) else "declaration")
                row = {"path": filename or None, "line": int(line), "column": int(column),
                       "severity": severity, "kind": kind, "message": message[:2000],
                       "message_truncated": len(message) > 2000, "log_offset": offset, "log_bytes": len(raw)}
                row["id"] = "diag-" + digest([source_sha256, offset, hashlib.sha256(raw).hexdigest()])
                rows.append(row)
            elif text.lstrip().startswith(("error:", "fatal error:", "PANIC", "uncaught exception")):
                unparsed += 1
                row = {"path": None, "line": None, "column": None, "severity": "error", "kind": "environment",
                       "message": text[:2000], "message_truncated": len(text) > 2000,
                       "log_offset": offset, "log_bytes": len(raw)}
                row["id"] = "diag-" + digest([source_sha256, offset, hashlib.sha256(raw).hexdigest()])
                rows.append(row)
            offset += len(raw)
            if len(rows) > _MAX_RECORDS:
                raise ValueError("too many compiler diagnostics; full log preserved")
    # Lake's generic aggregate summary does not turn a located Lean failure
    # into a project-wide environment blocker. Exact raw bytes remain stored.
    if any(row["severity"] == "error" and row["path"] is not None for row in rows):
        rows = [row for row in rows if not (row["path"] is None and row["message"].strip() in {
            "error: build failed", "error: Lean exited with code 1"})]
        unparsed = sum(row["severity"] == "error" and row["path"] is None for row in rows)
    if returncode != 0 and not any(row["severity"] == "error" for row in rows):
        rows.append({"id": "diag-" + digest([source_sha256, "unmapped-build-failure", returncode]),
            "path": None, "line": None, "column": None, "severity": "error", "kind": "environment",
            "message": "Build failed without a recognized located error; consult complete diagnostic artifact.",
            "message_truncated": False, "log_offset": 0, "log_bytes": offset})
        unparsed += 1
    return {"diagnostics": rows, "unmapped_error_count": unparsed, "log_bytes": offset,
            "parser": "lean-lake-located-lines-v1", "not_an_acceptance_check": True}


def _require_acyclic(graph: dict[str, list[str]]) -> None:
    remaining = {module: set(imports) for module, imports in graph.items()}
    completed = set()
    while remaining:
        ready = {module for module, imports in remaining.items() if imports <= completed}
        if not ready:
            raise ValueError("target/original module prerequisites contain a cycle; no repair dispatch is safe")
        completed.update(ready)
        for module in ready:
            del remaining[module]


def validate_target_imports(receipt: dict, index: dict | None = None) -> None:
    fields = {"version", "source_sha256", "source_files_sha256", "environment_sha256",
              "original_index_sha256", "modules", "sha256"}
    if (not isinstance(receipt, dict) or set(receipt) != fields or receipt.get("version") != 1
            or receipt.get("sha256") != digest({k: v for k, v in receipt.items() if k != "sha256"})
            or not isinstance(receipt.get("modules"), dict) or not receipt["modules"]):
        raise ValueError("invalid target native import receipt")
    for field in fields - {"version", "modules", "sha256"}:
        if not isinstance(receipt[field], str) or not re.fullmatch(r"[0-9a-f]{64}", receipt[field]):
            raise ValueError("invalid target import source binding")
    graph = receipt["modules"]
    for module, row in graph.items():
        if (not isinstance(module, str) or not module or not isinstance(row, dict)
                or set(row) != {"path", "imports", "compiler_derived", "status", "unavailable_reason"}
                or row["compiler_derived"] is not True or not isinstance(row["path"], str)
                or Path(row["path"]).is_absolute() or ".." in Path(row["path"]).parts
                or not isinstance(row["imports"], list)
                or any(not isinstance(dep, str) for dep in row["imports"])
                or row["imports"] != sorted(set(row["imports"]))
                or set(row["imports"]) - set(graph)
                or row["status"] not in {"complete", "unavailable"}
                or row["unavailable_reason"] not in {None, "header_syntax", "missing_import"}
                or (row["status"] == "complete") != (row["unavailable_reason"] is None)):
            raise ValueError("target native imports cross the sealed module scope")
    if index is not None:
        if (receipt["original_index_sha256"] != index["index_sha256"]
                or set(graph) != set(index["modules"])
                or any(row["path"] != index["modules"][module]["path"] for module, row in graph.items())):
            raise ValueError("target native imports change original module ownership")
    _require_acyclic({module: row["imports"] for module, row in graph.items()})


def scheduling_dependencies(index: dict, target_imports: dict) -> dict[str, list[str]]:
    """New native edges may add prerequisites, never erase original obligations."""
    validate_target_imports(target_imports, index)
    graph = {module: sorted(set(original["imports"]) | set(target_imports["modules"][module]["imports"]))
             for module, original in index["modules"].items()}
    _require_acyclic(graph)
    return graph


def capture_target_imports(root: Path, index: dict, scope: dict, identity: dict, source_files_sha: str) -> dict:
    """Read every current native header; unavailable evidence is never a success receipt.

    Syntax/unknown-import errors may discover file repairs. Tool failures, scope
    crossings, cycles and unsupported native output still fail closed.
    """
    root = Path(root).resolve()
    selected = {module: row["path"] for module, row in index["modules"].items()}
    if scope.get("selected_modules") != selected:
        raise ValueError("diagnostic native imports differ from the sealed scope")
    errors = project.scope_errors(scope, project.source_files(root))
    if errors:
        raise ValueError("; ".join(errors))
    if scope["mode"] == "build":
        native, defaults, _ = project._scope_native(root)
        if native != scope["native_metadata"] or defaults != scope["native_default_modules"]:
            raise ValueError("native target ownership changed before diagnostics")
    from . import bump_migration_contract as native
    executable = native._native_executable(root, Path(__file__).with_name("bump_inventory.lean"), identity["environment"])
    output = (root / scope.get("native_metadata", {}).get("build_dir", ".lake/build") / "lib/lean").resolve()
    owned = {(output / (module.replace(".", "/") + ".olean")).resolve(): module for module in selected}
    graph = {}
    for module, filename in selected.items():
        header = read_native_imports(root, filename, environment=identity["environment"], executable=executable)
        if set(header["imports"]) & set(scope.get("excluded_modules", {})):
            raise ValueError("target native header crosses excluded Bump module boundary")
        imports = set(header["imports"]) & set(selected)
        reason = "header_syntax" if header["header_errors"] else None
        if reason is None:
            result = project._run(root, ["lake", "env", "lean", "--deps", filename])
            if result.returncode:
                # --deps only parses headers/resolves imports; arbitrary compiler
                # failures must not be reclassified as a repairable source error.
                if not _missing_import_failure(result):
                    raise ValueError("target native dependency probe failed operationally")
                reason = "missing_import"
            native_imports = set()
            for line in result.stdout.splitlines():
                path = line.strip()
                if not path:
                    continue
                if not path.endswith(".olean"):
                    if reason is not None:
                        continue  # Error text is not native provenance evidence.
                    raise ValueError("unsupported target native dependency output")
                path = Path(path)
                if not path.is_absolute():
                    path = root / path
                path = path.resolve()
                owner = owned.get(path)
                if path.is_relative_to(output) and owner is None:
                    raise ValueError("target native dependency crosses the sealed Bump boundary")
                if owner is not None:
                    native_imports.add(owner)
            if reason is None and not imports <= native_imports:
                raise ValueError("native dependency output omits a selected header import")
            imports |= native_imports
        graph[module] = {"path": filename, "imports": sorted(imports), "compiler_derived": True,
                         "status": "complete" if reason is None else "unavailable", "unavailable_reason": reason}
    receipt = {"version": 1, "original_index_sha256": index["index_sha256"],
        "source_sha256": identity["source_sha256"], "source_files_sha256": source_files_sha,
        "environment_sha256": digest(identity["environment"]), "modules": graph}
    receipt["sha256"] = digest(receipt)
    scheduling_dependencies(index, receipt)
    return receipt


def validate_diagnostics(receipt: dict) -> None:
    if (not isinstance(receipt, dict) or receipt.get("version") != 1
            or receipt.get("kind") != "bump-build-diagnostics-v1"
            or receipt.get("snapshot_sha256") != digest({k: v for k, v in receipt.items() if k != "snapshot_sha256"})
            or receipt.get("complete_output") is not True or receipt.get("source_unchanged") is not True
            or type(receipt.get("returncode")) is not int or type(receipt.get("passed")) is not bool
            or receipt["passed"] != (receipt["returncode"] == 0)
            or not isinstance(receipt.get("diagnostics"), list)
            or not isinstance(receipt.get("compiled_modules"), list)):
        raise ValueError("invalid build diagnostic snapshot")
    for field in ("source_sha256", "source_files_sha256", "environment_sha256", "original_index_sha256"):
        if not isinstance(receipt.get(field), str) or not re.fullmatch(r"[0-9a-f]{64}", receipt[field]):
            raise ValueError("invalid diagnostic source binding")
    imports = receipt.get("target_imports")
    validate_target_imports(imports)
    if any(imports[field] != receipt[field] for field in (
            "source_sha256", "source_files_sha256", "environment_sha256", "original_index_sha256")):
        raise ValueError("target native imports belong to a different diagnostic snapshot")
    seen = set()
    for row in receipt["diagnostics"]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in seen
                or row.get("severity") not in {"error", "warning", "info"}
                or row.get("kind") not in {"declaration", "syntax", "import", "environment"}
                or not isinstance(row.get("message"), str) or len(row["message"]) > 2000
                or type(row.get("log_offset")) is not int or row["log_offset"] < 0
                or type(row.get("log_bytes")) is not int or row["log_bytes"] < 0):
            raise ValueError("invalid diagnostic row")
        seen.add(row["id"])
    located_errors = {row["path"] for row in receipt["diagnostics"] if row["severity"] == "error"}
    for module, row in imports["modules"].items():
        if row["status"] == "unavailable" and (module in receipt["compiled_modules"] or row["path"] not in located_errors):
            raise ValueError("unavailable import evidence requires a located failed-module diagnostic")
    if not receipt["passed"]:
        checks = receipt.get("module_checks", [])
        proved = {row.get("module") for row in checks if row.get("passed") is True}
        if set(receipt["compiled_modules"]) != proved:
            raise ValueError("compiled prerequisites require actual successful module build receipts")


def collect_build_diagnostics(target_root: Path, index: dict, *, artifact_dir: Path,
                              scope: dict, modules: list[str] | None = None) -> dict:
    """One normal build, complete output artifact, bounded source-bound discovery."""
    from . import bump_contract
    validate_index(index)
    root = Path(target_root).resolve()
    selected = sorted(index["modules"]) if modules is None else sorted(set(modules))
    if not selected or set(selected) - set(index["modules"]):
        raise ValueError("diagnostic build crosses original module scope")
    before = bump_contract.source_identity(root)
    source_files_sha = project.snapshot(root)
    target_imports = capture_target_imports(root, index, scope, before, source_files_sha)
    prerequisites = scheduling_dependencies(index, target_imports)
    def one_build(names):
        log_path = root / ".unity" / "bump-diagnostics" / (uuid.uuid4().hex + ".log")
        built = project.build(root, modules=names, scope=scope, diagnostics_path=log_path,
                              diagnostic_imports=target_imports)
        if not log_path.is_file():
            raise ValueError("build produced no complete diagnostic log")
        reference = store_log_artifact(artifact_dir, log_path)
        if not built.get("source_unchanged") or not built.get("diagnostics_complete") or built["returncode"] in {124, 127}:
            raise ValueError("build diagnostic capture did not complete; log preserved")
        parsed = parse_diagnostics(log_path, root, source_sha256=before["source_sha256"], returncode=built["returncode"])
        for row in parsed["diagnostics"]:
            row["artifact_ref"] = reference
            row["id"] = "diag-" + digest([row["id"], reference["sha256"]])
        return built, reference, parsed

    built, reference, parsed = one_build(selected)
    compiled, checks, extra_refs = set(selected) if built["passed"] else set(), [], []
    if not built["passed"]:
        # An aggregate failed build says nothing about which prerequisite jobs
        # passed. Recheck each reachable module at most once, in import order;
        # successful receipts (often cached builds) unlock the actual frontier.
        needed = set(selected)
        pending = list(selected)
        while pending:
            for dep in prerequisites[pending.pop()]:
                if dep not in needed:
                    needed.add(dep)
                    pending.append(dep)
        waiting, finished, failed = set(needed), set(), set()
        while waiting:
            frontier = sorted(module for module in waiting if set(prerequisites[module]) <= finished)
            if not frontier:
                raise ValueError("original module graph has a cycle; no repair dispatch is safe")
            for module in frontier:
                waiting.remove(module)
                finished.add(module)
                dependencies = set(prerequisites[module])
                if dependencies & failed:
                    failed.add(module)
                    continue
                checked, module_ref, module_parsed = one_build([module])
                extra_refs.append(module_ref)
                checks.append({"module": module, "passed": checked["passed"], "returncode": checked["returncode"],
                    "source_sha256": before["source_sha256"], "artifact_ref": module_ref})
                if checked["passed"]:
                    compiled.add(module)
                else:
                    failed.add(module)
                parsed["diagnostics"].extend(module_parsed["diagnostics"])
        # Collapse exact repeated root diagnostics, preserving one byte-bound
        # location and all complete aggregate/module log artifacts separately.
        unique = {}
        for row in parsed["diagnostics"]:
            key = digest([row["path"], row["line"], row["column"], row["severity"], row["kind"], row["message"]])
            unique.setdefault(key, row)
        parsed["diagnostics"] = list(unique.values())
        parsed["unmapped_error_count"] = sum(row["severity"] == "error" and row["path"] is None
                                              for row in parsed["diagnostics"])
    # A fresh target may initially lack a local .olean even though the source
    # header is valid. Successful builds can make that native evidence available;
    # refresh it once rather than misclassifying a clean module as an import error.
    if any(row["unavailable_reason"] == "missing_import" for row in target_imports["modules"].values()):
        target_imports = capture_target_imports(root, index, scope, before, source_files_sha)
    after = bump_contract.source_identity(root)
    if before != after or source_files_sha != project.snapshot(root) or not built["source_unchanged"]:
        raise ValueError("source/environment changed during diagnostic build; log preserved")
    by_path = {row["path"] for row in parsed["diagnostics"] if row["severity"] == "error"}
    for module, imports in target_imports["modules"].items():
        if imports["status"] == "unavailable" and (module in compiled or imports["path"] not in by_path):
            raise ValueError("unavailable import evidence requires a located failed-module diagnostic")
    receipt = {"version": 1, "kind": "bump-build-diagnostics-v1", "original_index_sha256": index["index_sha256"],
        "source_sha256": before["source_sha256"], "source_files_sha256": source_files_sha,
        "environment_sha256": digest(before["environment"]), "source_unchanged": True,
        "target_imports": target_imports,
        "artifact_ref": reference, "complete_output": True, "modules": selected,
        "module_source_hashes": {name: _sha_file(root / row["path"]) for name, row in index["modules"].items()},
        "compiled_modules": sorted(compiled), "module_checks": checks, "additional_artifact_refs": extra_refs,
        "returncode": built["returncode"],
        "passed": built["passed"], **parsed}
    receipt["snapshot_sha256"] = digest(receipt)
    validate_diagnostics(receipt)
    return receipt
