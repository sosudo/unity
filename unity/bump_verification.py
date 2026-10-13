"""Compiler-driven Bump evidence, source audits and revision-bound reports.

This module deliberately does not import the old native preservation contract.
Compilation is machine evidence; whether an API/proof edit is reasonable remains
an independent review of the actual diff, not a cross-version AST equality test.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from . import artifacts, bump_jobs

_RUNTIME = {".git", ".lake", ".unity", ".worktrees", "lake-packages", "__pycache__"}
_CONFIG = {"lean-toolchain", "lakefile.lean", "lakefile.toml", "lake-manifest.json"}
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_LOCATION = re.compile(r"^(?:(error|warning|info):\s*)?(.+?\.lean):(\d+):(\d+):\s*(?:(error|warning|info):\s*)?(.*)$")
_DECL = re.compile(r"(?m)^[ \t]*(?:@\[[^\n]*\][ \t]*)?(?:(?:private|protected|noncomputable|unsafe|partial|local|scoped)\s+)*(theorem|lemma|def|abbrev|opaque|axiom|constant|instance|structure|class|inductive|example|initialize|builtin_initialize)\b(?:[ \t]+([\w'.«»]+))?")
_TOKEN = re.compile(r"(?<![\w'])\b(sorry|admit|axiom|constant)\b(?![\w'])|(?m:^\s*(#exit)\b)")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _git(root: Path, *args: str, binary=False):
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
                            cwd=root, capture_output=True, text=not binary)
    if result.returncode:
        raise ValueError("Bump Git evidence command failed: " + " ".join(args[:2]))
    return result.stdout if binary else result.stdout.strip()


def _relative(root: Path, name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or not path.parts or "\x00" in name:
        raise ValueError("non-project path in Bump verification")
    candidate = root / path
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("Bump source path escapes project")
    return candidate


def _files(root: Path, build_dir=".lake/build") -> dict[str, str]:
    """Hash versioned/nonignored project files; never sweep ignored credentials."""
    files = {}
    names = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", binary=True)
    for raw in names.split(b"\x00"):
        if not raw:
            continue
        rel = raw.decode()
        if any(part in _RUNTIME for part in Path(rel).parts) or rel.startswith(build_dir.rstrip("/") + "/"):
            continue
        path = _relative(root, rel)
        if path.is_symlink():
            files[rel] = "symlink:" + hashlib.sha256(os.readlink(path).encode()).hexdigest()
        elif path.is_file():
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(files.items()))


def _tool_identity(root: Path, command: list[str]) -> dict:
    result = bump_jobs.run(root, command, cwd=root, task_id="workflow-toolchain-identity")
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode:
        raise ValueError("requested target executable does not run: " + " ".join(command))
    return {"command": command, "returncode": result.returncode, "version": output.strip(),
            "output_sha256": hashlib.sha256(output.encode()).hexdigest()}


def source_identity(root: Path, *, lake_args=(), scope=None) -> dict:
    root = Path(root).resolve()
    files = _files(root, (scope or {}).get("build_dir", ".lake/build"))
    result = {"schema_version": 1, "main_sha": _git(root, "rev-parse", "HEAD"),
              "files": files, "config": {name: sha for name, sha in files.items() if name in _CONFIG},
              "vendor": {name: sha for name, sha in files.items() if name.startswith("vendor/")},
              "scope": deepcopy(scope or {}), "lake_args": list(lake_args),
              "lean": _tool_identity(root, ["lake", *lake_args, "env", "lean", "--version"]),
              "lake": _tool_identity(root, ["lake", "--version"])}
    result["sha256"] = digest(result)
    return result


def mask_source(text: str, *, strict=True) -> str:
    """Mask nested comments, quoted identifiers and strings, keeping locations."""
    chars = list(text)
    i, depth, quote = 0, 0, None
    while i < len(text):
        pair, char = text[i:i + 2], text[i]
        if depth:
            if pair in {"/-", "-/"}:
                depth += 1 if pair == "/-" else -1
                chars[i:i + 2] = "  "
                i += 2
                continue
            chars[i] = "\n" if char == "\n" else " "
        elif quote:
            chars[i] = "\n" if char == "\n" else " "
            if quote == '"' and char == "\\" and i + 1 < len(text):
                chars[i + 1] = "\n" if text[i + 1] == "\n" else " "
                i += 2
                continue
            if char == quote:
                quote = None
        elif pair == "/-":
            chars[i:i + 2] = "  "
            depth = 1
            i += 2
            continue
        elif pair == "--":
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            chars[i:end] = " " * (end - i)
            i = end
            continue
        elif char in {'"', "«"}:
            quote = '"' if char == '"' else "»"
            chars[i] = " "
        i += 1
    if strict and (depth or quote):
        raise ValueError("source audit found unterminated comment, string or quoted identifier")
    return "".join(chars)


def _source_tokens(text: str, path: str, *, strict=True) -> tuple[list[dict], list[dict]]:
    masked = mask_source(text, strict=strict)
    declarations, name_counts = [], Counter()
    for match in _DECL.finditer(masked):
        kind, name = match.groups()
        # Anonymous instances begin with ':' or a binder, not a declaration name.
        name = name if name and kind != "example" and name not in {"where", "extends"} else "<anonymous-" + kind + ">"
        name_counts[name] += 1
        declarations.append({"name": name, "ordinal": name_counts[name], "kind": kind,
                             "offset": match.start(), "line": masked.count("\n", 0, match.start()) + 1})
    rows, counts = [], Counter()
    for match in _TOKEN.finditer(masked):
        kind = match.group(1) or match.group(2)
        offset = match.start(1) if match.group(1) else match.start(2)
        declaration = next((d for d in reversed(declarations) if d["offset"] <= offset), None)
        owner = (declaration or {}).get("name", "<commands>")
        owner_ordinal = (declaration or {}).get("ordinal", 0)
        key = (kind, owner, owner_ordinal)
        counts[key] += 1
        rows.append({"path": path, "kind": kind, "line": text.count("\n", 0, offset) + 1,
                     "column": offset - text.rfind("\n", 0, offset) - 1,
                     "declaration": owner, "declaration_ordinal": owner_ordinal,
                     "occurrence": counts[key], "context": text.splitlines()[text.count("\n", 0, offset)].strip()})
    return rows, declarations


def _token_key(row):
    return tuple(row[key] for key in ("path", "kind", "declaration", "declaration_ordinal", "occurrence"))


def _module_paths(scope: dict, key="selected_modules") -> dict:
    values = scope.get(key, {})
    return dict(values) if isinstance(values, dict) else {name: name.replace(".", "/") + ".lean" for name in values}


def audit_sources(root: Path, base_sha: str, scope: dict | None = None) -> dict:
    """Location/owner-aware hole audit, not merely a before/after token count."""
    root, scope = Path(root).resolve(), scope or {}
    base_paths = _git(root, "ls-tree", "-r", "--name-only", base_sha).splitlines()
    current_files = _files(root, scope.get("build_dir", ".lake/build"))
    before, after, issues, removed, skipped, limitations = [], [], [], [], [], []
    before_declarations, after_declarations = {}, {}
    for path in sorted(set(base_paths) | set(current_files)):
        if not path.endswith(".lean") or any(part in _RUNTIME for part in Path(path).parts):
            continue
        if path in base_paths:
            text = _git(root, "show", f"{base_sha}:{path}", binary=True).decode("utf-8")
            try:
                rows, decls = _source_tokens(text, path)
            except ValueError as exc:
                # A successful old build is deliberately not a prerequisite.
                rows, decls = _source_tokens(text, path, strict=False)
                limitations.append({"path": path, "snapshot": "baseline", "reason": str(exc)})
            before.extend(rows)
            before_declarations[path] = decls
        if path in current_files:
            file = _relative(root, path)
            if file.is_symlink():
                issues.append({"kind": "source_symlink", "path": path})
                continue
            try:
                rows, decls = _source_tokens(file.read_text(), path)
            except ValueError as exc:
                # Invalid candidate Lean is repairable work, not a controller crash.
                issues.append({"kind": "source_audit_parse_error", "path": path, "reason": str(exc)})
                rows, decls = _source_tokens(file.read_text(), path, strict=False)
            after.extend(rows)
            after_declarations[path] = decls
        elif path in base_paths:
            removed.append(path)
    baseline_keys = {_token_key(row) for row in before}
    inherited = [row for row in after if _token_key(row) in baseline_keys]
    # New dependency checkouts bring their own disclosed unfinished source;
    # existing vendored source is still checked for newly inserted holes.
    introduced = [row for row in after if _token_key(row) not in baseline_keys
                  and (not row["path"].startswith("vendor/") or row["path"] in base_paths)]
    dependency_holes = [row for row in after if row["path"].startswith("vendor/")]
    issues.extend({**row, "kind": "introduced_" + row["kind"].lstrip("#"), "token": row["kind"]} for row in introduced)
    selected_paths = set(_module_paths(scope).values()) | set(_module_paths(scope, "default_modules").values())
    excluded = scope.get("excluded_files", {})
    issues.extend({"kind": "removed_source_file", "path": path} for path in removed
                  if path in selected_paths or not isinstance(excluded, dict) or not excluded.get(path))
    for row in after:
        if row["kind"] != "#exit":
            continue
        path = row["path"]
        old_exits = [r["line"] for r in before if r["path"] == path and r["kind"] == "#exit"]
        previously_reached = {(d["name"], d["ordinal"]) for d in before_declarations.get(path, [])
                              if not old_exits or d["line"] < min(old_exits)}
        now_skipped = [d for d in after_declarations.get(path, []) if d["line"] > row["line"]]
        skipped.extend({"path": path, "name": d["name"], "line": d["line"]} for d in now_skipped)
        for decl in now_skipped:
            if (decl["name"], decl["ordinal"]) in previously_reached:
                issues.append({"kind": "newly_skipped_declaration", "path": path, "name": decl["name"]})
    if isinstance(excluded, dict):
        for path, reason in excluded.items():
            if not isinstance(reason, str) or not reason.strip():
                issues.append({"kind": "unexplained_exclusion", "path": path})
    if selected_paths:
        uncovered = sorted(path for path in after_declarations
                           if path not in selected_paths and path not in excluded
                           and path != "lakefile.lean" and not path.startswith("vendor/"))
        issues.extend({"kind": "unclassified_source_module", "path": path} for path in uncovered)
    for row in after:
        row["scope"] = ("dependency" if row["path"].startswith("vendor/") else
                        "selected" if row["path"] in selected_paths else "excluded_or_unselected")
    return {"passed": not issues, "baseline": before, "current": after, "inherited": inherited,
            "introduced": introduced, "dependency_holes": dependency_holes, "skipped_declarations": skipped,
            "removed_files": removed, "excluded_files": deepcopy(excluded), "issues": issues,
            "limitations": limitations,
            "qualification": "Source token and scope audit; not transitive axiom certification or semantic equivalence."}


def _parse_diagnostics(output: str, root: Path) -> list[dict]:
    rows = []
    for line in _ANSI.sub("", output).splitlines():
        match = _LOCATION.match(line)
        if match:
            first, filename, number, column, second, message = match.groups()
            path = Path(filename)
            if path.is_absolute():
                try:
                    path = path.relative_to(root)
                except ValueError:
                    path = None
            if path is not None and ".." in path.parts:
                path = None
            row = {"path": path.as_posix() if path else None, "line": int(number), "column": int(column),
                   "severity": second or first or "error", "message": message,
                   "kind": "import" if any(s in message.lower() for s in ("unknown module", "unknown package", "object file")) else "declaration"}
        elif line.startswith(("error:", "fatal error:", "PANIC", "uncaught exception")) and line.strip() not in {"error: build failed", "error: Lean exited with code 1"}:
            row = {"path": None, "line": None, "column": None, "severity": "error", "message": line, "kind": "environment"}
        else:
            continue
        row["id"] = "diagnostic-" + digest(row)[:20]
        rows.append(row)
    return rows


def _fresh_project_outputs(root: Path, build_dir: str) -> dict:
    """Retain old project outputs separately; never delete dependencies/sources."""
    path = Path(build_dir)
    if (path.is_absolute() or ".." in path.parts or not path.parts
            or path.as_posix() in {".", ".lake"} or path.parts[0] in {"vendor", ".git", ".unity"}
            or "packages" in path.parts or "lake-packages" in path.parts):
        raise ValueError("unsafe project output directory")
    target = _relative(root, build_dir)
    if any((root / Path(*path.parts[:n])).is_symlink() for n in range(1, len(path.parts) + 1)):
        raise ValueError("project output directory contains symlink")
    tracked = _git(root, "ls-files").splitlines()
    if any(name == build_dir or name.startswith(build_dir.rstrip("/") + "/") for name in tracked):
        raise ValueError("project output directory contains tracked sources")
    if not target.exists():
        return {"fresh": True, "retained_outputs": None, "build_dir": build_dir}
    if not target.is_dir() or any(p.suffix == ".lean" for p in target.rglob("*.lean")):
        raise ValueError("project output directory contains source files")
    retained = root / ".unity" / "bump" / "previous-builds" / uuid.uuid4().hex
    retained.parent.mkdir(parents=True, exist_ok=True)
    target.rename(retained)
    return {"fresh": True, "retained_outputs": str(retained.relative_to(root)), "build_dir": build_dir}


def build(root: Path, *, modules=(), lake_args=(), fresh_project=False, on_output=None,
          project_root=None, scope=None) -> dict:
    root = Path(root).resolve()
    scope = deepcopy(scope or {})
    owner_root = Path(project_root or root).resolve()
    with bump_jobs._build_lock(owner_root):
        fresh = _fresh_project_outputs(root, scope.get("build_dir", ".lake/build")) if fresh_project else {"fresh": False}
        identity = source_identity(root, lake_args=lake_args, scope=scope)
        command = ["lake", *lake_args, "--rehash", "build", *["+" + m.removeprefix("+") for m in modules]]
        result = bump_jobs.run(owner_root, command, cwd=root, task_id="workflow-build", serialize_build=True, on_output=on_output)
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        diagnostics = _parse_diagnostics(output, root)
        if result.returncode and not any(d["severity"] == "error" for d in diagnostics):
            row = {"path": None, "line": None, "column": None, "severity": "error", "kind": "environment",
                   "message": "Build failed without a located diagnostic; inspect the exact log."}
            diagnostics.append({**row, "id": "diagnostic-" + digest(row)[:20]})
        stable = identity["files"] == _files(root, scope.get("build_dir", ".lake/build"))
        if not stable:
            diagnostics.append({"id": "source-changed", "path": None, "line": None, "column": None,
                                "severity": "error", "kind": "environment", "message": "Source changed during authoritative build."})
        module_paths = {**_module_paths(scope, "default_modules"), **_module_paths(scope)}
        requested = list(modules) or list(_module_paths(scope, "default_modules"))
        for name in modules:
            module_paths.setdefault(name, name.replace(".", "/") + ".lean")
        checked, attempted = set(), {d["path"] for d in diagnostics if d.get("path")}
        for line in _ANSI.sub("", output).splitlines():
            match = re.search(r"\b(Built|Replayed|Building|Running|Failed)\s+([\w.]+)(?::\w+)?\b", line)
            if match and match[2] in module_paths:
                attempted.add(module_paths[match[2]])
                if match[1] in {"Built", "Replayed"}:
                    checked.add(module_paths[match[2]])
        errors = [d for d in diagnostics if d["severity"] == "error"]
        passed = result.returncode == 0 and stable and not errors
        if passed:
            checked.update(module_paths[name] for name in requested if name in module_paths)
        checked.difference_update(d["path"] for d in errors if d.get("path"))
        attempted.update(checked)
        log = artifacts.store_text(owner_root / ".unity" / "artifacts", output, kind="bump_workflow_build", producer="Unity",
                                   metadata={"command": command, "source_sha256": identity["sha256"], "returncode": result.returncode})
    return {"passed": passed, "completed": passed, "returncode": result.returncode, "command": command,
            "diagnostics": diagnostics, "errors": errors, "unmapped": [d for d in errors if not d.get("path")],
            "checked_files": sorted(checked), "covered_files": sorted(checked), "attempted_files": sorted(attempted),
            "blocked_files": sorted({d["path"] for d in errors if d.get("path") and d["kind"] == "import"}),
            "source_identity": identity, "source_sha256": identity["sha256"], "log_ref": log,
            "fresh_project": fresh, "source_stable": stable}


def _patch_replay(root: Path, base_sha: str, current_files: dict, artifacts_dir: Path) -> dict:
    """Create and replay a binary patch inside an independent temporary Git repo."""
    with tempfile.TemporaryDirectory(prefix="unity-bump-patch-") as directory:
        sandbox = Path(directory)
        _git(sandbox, "init", "--quiet")
        _git(sandbox, "config", "user.name", "Unity patch check")
        _git(sandbox, "config", "user.email", "unity-patch-check@localhost")
        tree = _git(root, "ls-tree", "-r", "-z", base_sha, binary=True)
        baseline_files = []
        for entry in tree.split(b"\x00"):
            if not entry:
                continue
            header, encoded_name = entry.split(b"\t", 1)
            mode, kind, sha = header.decode().split()
            name = encoded_name.decode()
            if kind != "blob":
                raise ValueError("portable patch replay does not support Git submodules")
            if any(part in _RUNTIME for part in Path(name).parts):
                raise ValueError("tracked runtime data is not a portable migration artifact")
            path = _relative(sandbox, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = _git(root, "cat-file", "blob", sha, binary=True)
            if mode == "120000":
                path.symlink_to(data.decode())
            else:
                path.write_bytes(data)
                path.chmod(0o755 if mode == "100755" else 0o644)
            baseline_files.append(name)
        _git(sandbox, "add", "--all", "--force")
        _git(sandbox, "commit", "--quiet", "--allow-empty", "-m", "Patch baseline")
        for name in baseline_files:
            path = sandbox / name
            if path.is_file() or path.is_symlink():
                path.unlink()
        for name in current_files:
            source, target = _relative(root, name), _relative(sandbox, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                target.symlink_to(os.readlink(source))
            else:
                target.write_bytes(source.read_bytes())
                target.chmod(source.stat().st_mode & 0o777)
        _git(sandbox, "add", "--all", "--force")
        patch = _git(sandbox, "diff", "--cached", "--binary", "HEAD", binary=True)
        # Both this checkout and all its files were created above for this check.
        _git(sandbox, "restore", "--source=HEAD", "--staged", "--worktree", ".")
        if patch:
            for args in (["apply", "--check", "--index", "-"], ["apply", "--index", "-"]):
                proc = subprocess.run(["git", *args], input=patch, cwd=sandbox, capture_output=True)
                if proc.returncode:
                    raise ValueError("portable binary patch replay failed")
        actual = _files(sandbox)
        if actual != current_files:
            raise ValueError("portable patch replay byte comparison failed")
        record = artifacts.store_text(artifacts_dir, patch.decode(), kind="bump_project_patch", producer="Unity")
        return {"passed": True, "checked_files": len(current_files), "base_sha": base_sha,
                "patch_ref": record, "commands": ["git apply --check --index", "git apply --index"],
                "qualification": "Patch replay and byte comparison; not a second dependency rebuild."}


def verify_final(root: Path, *, base_sha: str, scope: dict, lake_args=(), artifacts_dir=None,
                 config_revision=0) -> dict:
    root = Path(root).resolve()
    artifacts_dir = Path(artifacts_dir or root / ".unity" / "artifacts")
    before = source_identity(root, lake_args=lake_args, scope=scope)
    builds = [build(root, lake_args=lake_args, fresh_project=True, scope=scope)]
    supplemental = sorted(set(_module_paths(scope)) - set(_module_paths(scope, "default_modules")))
    if supplemental:
        builds.append(build(root, modules=supplemental, lake_args=lake_args, scope=scope))
    audit = audit_sources(root, base_sha, scope)
    issues = list(audit["issues"])
    checked = set().union(*(set(item["checked_files"]) for item in builds))
    missing = sorted(set(_module_paths(scope).values()) - checked)
    if missing:
        issues.append({"kind": "unbuilt_selected_files", "paths": missing})
    if not all(item["passed"] for item in builds):
        issues.append({"kind": "required_build_failed"})
    after = source_identity(root, lake_args=lake_args, scope=scope)
    if before != after:
        issues.append({"kind": "snapshot_changed_during_verification"})
    replay = _patch_replay(root, base_sha, after["files"], artifacts_dir)
    result = {"schema_version": 1, "snapshot_id": "bump-review-" + uuid.uuid4().hex,
              "created_at": time.time(), "main_sha": after["main_sha"], "base_sha": base_sha,
              "config_revision": config_revision,
              "source_identity": after, "scope": deepcopy(scope), "lake_args": list(lake_args),
              "passed": not issues, "issues": issues, "builds": builds, "hole_audit": audit,
              "patch_replay": replay, "checked_files": sorted(checked),
              "diagnostics": [row for item in builds for row in item["diagnostics"]],
              "qualification": "Target compilation and source/patch evidence; independent critic judges the diff, not AST equivalence."}
    result["sha256"] = digest(result)
    return result


def _diagnostic_key(row):
    return row.get("path"), row.get("kind"), row.get("message")


def candidate_progress(before_diagnostics, after_build: dict, task: dict, inventory: dict) -> dict:
    before = before_diagnostics.get("diagnostics", []) if isinstance(before_diagnostics, dict) else before_diagnostics
    before = [d for d in before if d.get("severity", "error") == "error"]
    after = after_build.get("errors", [d for d in after_build.get("diagnostics", []) if d.get("severity") == "error"])
    files = set(task.get("files", []))
    chunks = inventory.get("chunks", inventory)
    owned_chunks = [chunks.get(key, {}) for key in task.get("chunk_ids", [])] if isinstance(chunks, dict) else []
    def owned_diagnostic(row):
        if row.get("path") not in files:
            return False
        if not owned_chunks:
            return True  # Explicit module/import/configuration assignment.
        return any(row.get("path") == c.get("path", c.get("file"))
                   and c.get("start_line", 0) <= (row.get("line") or 0) <= c.get("end_line", 0)
                   for c in owned_chunks)
    assigned = [d for d in before if owned_diagnostic(d)]
    old_keys, new_keys = {_diagnostic_key(d) for d in before}, {_diagnostic_key(d) for d in after}
    resolved = [d for d in assigned if _diagnostic_key(d) not in new_keys]
    module_paths = {p.removesuffix(".lean").replace("/", "."): p for p in inventory.get("files", {})}
    downstream = set(files)
    while True:
        expanded = downstream | {p for p, row in inventory.get("files", {}).items()
                                 if any(module_paths.get(m) in downstream for m in row.get("imports", []))}
        if expanded == downstream:
            break
        downstream = expanded
    previously_checked = set(before_diagnostics.get("checked_files", [])) if isinstance(before_diagnostics, dict) else set()
    exposed = [d for d in after if _diagnostic_key(d) not in old_keys and d.get("path") in downstream - files - previously_checked]
    regressions = [d for d in after if _diagnostic_key(d) not in old_keys
                   and (d.get("path") not in downstream or d.get("path") in previously_checked)]
    blocked = {d.get("path") for d in after if d.get("kind") == "import"}
    checked = set(after_build.get("checked_files", []))
    reached = set(checked)
    for chunk_id in task.get("chunk_ids", []):
        chunk = chunks.get(chunk_id, {}) if isinstance(chunks, dict) else {}
        path = chunk.get("path", chunk.get("file"))
        end = chunk.get("end_line", chunk.get("range", {}).get("end_line"))
        if path and end and any(d.get("path") == path and d.get("kind") != "import" and (d.get("line") or 0) >= end for d in after):
            reached.add(path)
    eligible = after_build.get("source_stable", True) and not after_build.get("unmapped") and not (blocked & files) and not regressions
    accepted = bool(eligible and (after_build.get("passed") or (resolved and files <= reached)))
    return {"accepted": accepted, "made_progress": accepted, "resolved_diagnostic_ids": [d.get("id") for d in resolved],
            "remaining_errors": after, "regressions": regressions, "exposed_downstream_errors": exposed,
            "blocked_files": sorted(p for p in blocked if p),
            "reason": "checked repair with unrelated failures retained" if accepted else "no safely demonstrated repair progress",
            "qualification": "Provisional compiler progress, not final migration acceptance."}


def completion_report(state: dict, snapshot: dict | None = None, verdict=None) -> dict:
    snapshot = snapshot or state.get("review", {}).get("snapshot") or {}
    review = state.get("review", {})
    verdict = verdict or review.get("verdict") or next(iter(reversed(review.get("verdicts", []))), None)
    accepted = state.get("phase") == "complete" or state.get("status") == "accepted"
    if accepted:
        content = {k: v for k, v in snapshot.items() if k != "sha256"}
        if not snapshot.get("passed") or snapshot.get("sha256") != digest(content):
            raise ValueError("accepted migration requires current passed machine evidence")
        identity = snapshot.get("source_identity", {})
        builds = snapshot.get("builds", [])
        required = set(_module_paths(snapshot.get("scope", {})).values())
        required.update(_module_paths(snapshot.get("scope", {}), "default_modules").values())
        if (not identity or identity.get("sha256") != digest({k: value for k, value in identity.items() if k != "sha256"})
                or identity.get("main_sha") != snapshot.get("main_sha")
                or identity.get("scope") != snapshot.get("scope")
                or not builds or not builds[0].get("fresh_project", {}).get("fresh")
                or any(not item.get("passed") or not item.get("completed") or item.get("returncode") != 0
                       or not item.get("source_stable") or item.get("errors") for item in builds)
                or not required <= set(snapshot.get("checked_files", []))
                or not snapshot.get("hole_audit", {}).get("passed") or snapshot.get("hole_audit", {}).get("issues")
                or not snapshot.get("patch_replay", {}).get("passed") or snapshot.get("issues")):
            raise ValueError("accepted migration lacks complete build, scope, source audit or patch evidence")
        if state.get("main_sha") and state["main_sha"] != snapshot.get("main_sha"):
            raise ValueError("accepted migration snapshot is not current main")
        if state.get("config") is not None and snapshot.get("config_revision") != state["config"].get("revision"):
            raise ValueError("accepted migration snapshot is not current configuration")
        authors = {str(c.get("author", c.get("owner", ""))).casefold() for c in state.get("candidates", {}).values() if c.get("status") in {"merged", "accepted"}}
        authors.update(str(author).casefold() for author in snapshot.get("authors", []))
        if (not verdict or verdict.get("verdict") != "approved" or not verdict.get("author")
                or verdict["author"].casefold() in authors or verdict.get("snapshot_id") != snapshot.get("snapshot_id")
                or verdict.get("snapshot_sha256") != snapshot.get("sha256")
                or verdict.get("reopen_task_ids") or verdict.get("reopen_tasks")):
            raise ValueError("accepted migration requires independent review bound to the identical snapshot")
    return {"schema_version": 1, "pipeline": "bump", "run_id": state.get("run_id"),
            "status": "accepted" if accepted else "incomplete", "phase": state.get("phase"),
            "project_changes": {"base_sha": state.get("base_sha", snapshot.get("base_sha")),
                "main_sha": snapshot.get("main_sha", state.get("main_sha")), "scope": snapshot.get("scope", state.get("scope")),
                "dependencies": deepcopy(state.get("dependencies", {})), "configuration": deepcopy(state.get("config", {})),
                "builds": snapshot.get("builds", []), "hole_audit": snapshot.get("hole_audit", {}),
                "patch_replay": snapshot.get("patch_replay", {}), "candidates": deepcopy(state.get("candidates", {})),
                "critic": deepcopy(verdict), "issues": snapshot.get("issues", []),
                "not_checked": ["cross-version semantic equivalence", "whole transitive axiom certification", "interactive UI behavior"]},
            "workflow_changes": deepcopy(state.get("workflow_changes", [])),
            "snapshot_id": snapshot.get("snapshot_id"), "snapshot_sha256": snapshot.get("sha256"),
            "qualification": "Compilation is machine evidence. Diff reasonableness is an independent critic judgment. Inherited holes are not newly proved."}


def persist_report(paths, state: dict, snapshot=None, verdict=None, *, accepted=None) -> dict:
    report = completion_report(state, snapshot, verdict)
    if accepted is True and report["status"] != "accepted":
        raise ValueError("cannot persist accepted report for incomplete migration")
    root = Path(paths.project_root) if hasattr(paths, "project_root") else Path(paths)
    directory = Path(getattr(paths, "artifacts", getattr(paths, "artifacts_dir", root / ".unity" / "artifacts")))
    refs = {}
    for kind in ("project_changes", "workflow_changes"):
        refs[kind] = artifacts.store_text(directory, json.dumps(report[kind], indent=2, sort_keys=True) + "\n",
                                         kind="bump_" + kind, producer="Unity")
    refs["final_report"] = artifacts.store_text(directory, json.dumps(report, indent=2, sort_keys=True) + "\n",
                                               kind="bump_final_report", producer="Unity")
    return {"report": report, "artifacts": refs, "status": report["status"]}
