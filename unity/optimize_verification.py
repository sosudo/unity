"""Optimize-owned build, preservation and snapshot-bound completion evidence.

File identity and completion_report are read-only and never run Lean/Lake.
Builds are explicit operations. Lexical headers and holes are conservative
evidence, not an elaborated semantic equivalence checker: the primary still
reviews the actual patch and the claimed metric improvement.
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
import time
import uuid

from . import artifacts
from .optimize_metrics import compare_measurements, digest, load_metric, validate_measurement_binding

_RUNTIME = {".git", ".lake", ".unity", ".worktrees", "lake-packages", "__pycache__"}
_CONFIG = {"lean-toolchain", "lakefile.lean", "lakefile.toml", "lake-manifest.json"}
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_LOCATION = re.compile(r"^(?:(error|warning|info):\s*)?(.+?\.lean):(\d+):(\d+):\s*(?:(error|warning|info):\s*)?(.*)$")
_DECL = re.compile(r"(?m)^[ \t]*(?:@\[[^\n]*\][ \t]*)?(?:(?:private|protected|noncomputable|unsafe|partial|local|scoped)\s+)*(theorem|lemma|def|abbrev|opaque|axiom|constant|instance|structure|class|inductive|example)\b(?:[ \t]+((?:«[^»]*»|[\w'.])+))?")
_HOLE = re.compile(r"\b(sorry|admit|axiom|constant|unsafe)\b|(?m:^\s*(#exit)\b)|\b(implemented_by|extern)\b")


def _seal(value):
    value["sha256"] = digest(value)
    return value


def _valid(value):
    return isinstance(value, dict) and value.get("sha256") == digest({k: v for k, v in value.items() if k != "sha256"})


def _git(root, *args, binary=False):
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
                            cwd=root, capture_output=True, text=not binary)
    if result.returncode:
        raise ValueError("Optimize Git evidence command failed: " + " ".join(args[:2]))
    return result.stdout if binary else result.stdout.strip()


def _relative(root, name):
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or not path.parts or "\x00" in name:
        raise ValueError("non-project path in Optimize verification")
    target = Path(root) / path
    if not target.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError("Optimize source path escapes project")
    return target


def _files(root, build_dir=".lake/build"):
    files = {}
    names = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", binary=True)
    for raw in names.split(b"\x00"):
        if not raw:
            continue
        name = raw.decode()
        if any(part in _RUNTIME for part in Path(name).parts) or name.startswith(build_dir.rstrip("/") + "/"):
            continue
        path = _relative(root, name)
        if path.is_symlink():
            files[name] = "symlink:" + hashlib.sha256(os.readlink(path).encode()).hexdigest()
        elif path.is_file():
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(files.items()))


def file_identity(root, scope=None, *, lake_args=()):
    """Hash current Git/source/configuration without compiler/version probes."""
    root, scope = Path(root).resolve(), deepcopy(scope or {})
    files = _files(root, scope.get("build_dir", ".lake/build"))
    config = {name: sha for name, sha in files.items() if name in _CONFIG}
    # Active agent/instruction bytes are deliberately not exposed in evidence.
    for name in (".unity/agents.yaml", ".unity/config.yaml", ".unity/UNITY.md", "UNITY.md"):
        path = root / name
        if path.is_file():
            config[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return _seal({"schema_version": 1, "main_sha": _git(root, "rev-parse", "HEAD"),
                  "files": files, "config": config, "scope": scope, "lake_args": list(lake_args)})


source_identity = file_identity  # Unlike migration's historical helper, no tool probes.


def mask_source(text, *, strings=True, identifiers=True):
    """Mask nested comments and optionally strings; retain byte/line positions."""
    chars, depth, quote, i = list(text), 0, None, 0
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
            if (strings and quote == '"') or (identifiers and quote == '»'):
                chars[i] = "\n" if char == "\n" else " "
            if char == "\\" and quote == '"' and i + 1 < len(text):
                if strings:
                    chars[i + 1] = " "
                i += 2
                continue
            if char == quote:
                quote = None
        elif pair == "/-":
            chars[i:i + 2] = "  "
            depth, i = 1, i + 2
            continue
        elif pair == "--":
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            chars[i:end] = " " * (end - i)
            i = end
            continue
        elif char in {'"', '«'}:
            quote = '"' if char == '"' else '»'
            if (strings and quote == '"') or (identifiers and quote == '»'):
                chars[i] = " "
        i += 1
    return "".join(chars)


def _header(text):
    """Conservative lexical header; ignore comments but keep literal contents."""
    visible, masked = mask_source(text, strings=False, identifiers=False), mask_source(text)
    depth = 0
    for i, char in enumerate(masked):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        boundary = char == "\n" and re.match(r"\n\s*(?:end|namespace|section|variable|universe|open|attribute|set_option|notation|infix|prefix|#)\b", masked[i:])
        where = re.match(r"where\b", masked[i:]) and (i == 0 or not re.match(r"[\w']", masked[i - 1]))
        if depth == 0 and (masked[i:i + 2] == ":=" or where or boundary):
            visible = visible[:i]
            break
    # Normalize whitespace between tokens, not whitespace inside literals or
    # quoted declaration names (which can change the actual statement).
    parts, start, quote, i = [], 0, None, 0
    while i < len(visible):
        char = visible[i]
        if quote:
            if char == "\\" and quote == '"':
                i += 2
                continue
            if char == quote:
                parts.append(visible[start:i + 1])
                start, quote = i + 1, None
        elif char in {'"', '«'}:
            parts.extend(visible[start:i].split())
            start, quote = i, '"' if char == '"' else '»'
        i += 1
    parts.extend(visible[start:].split())
    return " ".join(parts)


def _source_record(text):
    masked, counts, declarations = mask_source(text, identifiers=False), Counter(), []
    matches = list(_DECL.finditer(masked))
    for index, match in enumerate(matches):
        kind, name = match.groups()
        name = name if name and name not in {"where", "extends"} else "<anonymous-" + kind + ">"
        counts[name] += 1
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        declarations.append({"name": name, "ordinal": counts[name], "kind": kind,
                             "header": _header(text[match.start():end]), "offset": match.start(),
                             "line": text.count("\n", 0, match.start()) + 1})
    holes, occurrences = [], Counter()
    for match in _HOLE.finditer(mask_source(text)):
        kind = next(group for group in match.groups() if group)
        owner = next((d for d in reversed(declarations) if d["offset"] <= match.start()), {})
        key = (kind, owner.get("name", "<commands>"), owner.get("ordinal", 0))
        occurrences[key] += 1
        holes.append({"kind": kind, "declaration": key[1], "declaration_ordinal": key[2],
                      "occurrence": occurrences[key], "line": text.count("\n", 0, match.start()) + 1})
    return {"declarations": declarations, "holes": holes}


def _source_audit(root, files):
    rows = {}
    for name in files:
        if name.endswith(".lean") and name != "lakefile.lean":
            path = _relative(root, name)
            if path.is_symlink():
                rows[name] = {"symlink": True}
            else:
                payload = path.read_bytes()
                if hashlib.sha256(payload).hexdigest() != files[name]:
                    raise ValueError("source changed during source audit")
                rows[name] = _source_record(payload.decode())
    return rows


def audit_sources(root, baseline):
    """Compare named headers and owner-aware trust tokens with frozen baseline."""
    root = Path(root).resolve()
    before = baseline.get("audit", {}).get("sources", baseline.get("sources", {}))
    current = _source_audit(root, _files(root, baseline.get("scope", {}).get("build_dir", ".lake/build")))
    issues, inherited, introduced = [], [], []
    for name in sorted(set(before) | set(current)):
        old, new = before.get(name, {}), current.get(name, {})
        if name not in current:
            issues.append({"kind": "removed_source_file", "path": name})
        if new.get("symlink"):
            issues.append({"kind": "source_symlink", "path": name})
        previous = {(d["name"], d["ordinal"]): d for d in old.get("declarations", [])}
        latest = {(d["name"], d["ordinal"]): d for d in new.get("declarations", [])}
        for key, declaration in previous.items():
            if key not in latest:
                issues.append({"kind": "removed_declaration", "path": name, "name": key[0]})
            elif declaration["header"] != latest[key]["header"]:
                # Completion may replace an axiom/constant by a theorem with
                # exactly the same remaining statement, not remove the claim.
                old_header = re.sub(r"^(axiom|constant)\b", "theorem", declaration["header"])
                new_header = re.sub(r"^(lemma)\b", "theorem", latest[key]["header"])
                allowed = baseline.get("metric", {}).get("builtin") == "completion" and old_header == new_header
                if not allowed:
                    issues.append({"kind": "changed_declaration_header", "path": name, "name": key[0]})
        key = lambda row: (row["kind"], row["declaration"], row["declaration_ordinal"], row["occurrence"])
        old_holes = {key(row) for row in old.get("holes", [])}
        for row in new.get("holes", []):
            record = {"path": name, **row}
            (inherited if key(row) in old_holes else introduced).append(record)
    issues.extend({**row, "kind": "introduced_" + row["kind"].lstrip("#")} for row in introduced)
    return {"passed": not issues, "sources": current, "issues": issues,
            "inherited": inherited, "introduced": introduced,
            "holes_remaining": sum(row["kind"] in {"sorry", "admit", "axiom", "constant"} for row in inherited + introduced),
            "qualification": "Lexical declaration-header and trust-token audit, not elaborated type equality or semantic equivalence; primary patch review remains required."}


def _parse_diagnostics(output, root):
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
                   "severity": second or first or "error", "message": message}
        elif line.startswith(("error:", "fatal error:", "PANIC", "uncaught exception")) and line not in {"error: build failed", "error: Lean exited with code 1"}:
            row = {"path": None, "line": None, "column": None, "severity": "error", "message": line}
        else:
            continue
        rows.append({**row, "id": "diagnostic-" + digest(row)[:20]})
    return rows


def scope_build_commands(scope=None, lake_args=(), *, modules=()):
    default = ["lake", *lake_args, "--rehash", "build"]
    selected = sorted(set((scope or {}).get("modules", [])) | set(modules))
    return [default, [*default, *["+" + module.removeprefix("+") for module in selected]]] if selected else [default]


def build_scope(root, *, scope=None, lake_args=(), project_root=None, on_output=None, selected_files=()):
    """Run the full configured default build and any explicit selected modules."""
    from . import optimize_jobs, optimize_workspace
    root, owner = Path(root).resolve(), Path(project_root or root).resolve()
    scope = deepcopy(scope or {})
    lake_args = optimize_workspace.checked_lake_args(lake_args)
    requested = sorted(set(selected_files or ()))
    if any(not isinstance(name, str) or not name.endswith(".lean") or name == "lakefile.lean" for name in requested):
        raise ValueError("selected checks must name project Lean modules, not Lake configuration")
    for name in requested:
        _relative(root, name)
    builds, diagnostics, seen = [], [], set()
    with optimize_jobs._build_lock(owner):
        before = file_identity(root, scope=scope, lake_args=lake_args)
        present = [name for name in requested if (root / name).is_file()]
        layout = optimize_workspace.discover(root, present, lake_args=lake_args) if present else {"modules": {}, "unmatched": [], "issues": []}
        if present and layout.get("build_dir", ".lake/build") != scope.get("build_dir", ".lake/build"):
            raise ValueError("Lake build directory differs from frozen Optimize scope")
        mapped = {name: layout["modules"][name] for name in present if name in layout["modules"]}
        unchecked = sorted(set(requested) - set(mapped))
        commands = scope_build_commands(scope, lake_args, modules=mapped.values())
        for command in commands:
            result = optimize_jobs.run(owner, command, cwd=root, task_id="workflow-build", serialize_build=True, on_output=on_output)
            output = (result.stdout or "") + "\n" + (result.stderr or "")
            rows = _parse_diagnostics(output, root)
            if result.returncode and not any(row["severity"] == "error" for row in rows):
                rows.append({"path": None, "line": None, "column": None, "severity": "error",
                             "message": "Build failed without a located diagnostic; inspect exact log.", "id": "unlocated-build-failure"})
            log = artifacts.store_text(owner / ".unity" / "artifacts", output, kind="optimize_build", producer="Unity",
                                       metadata={"command": command, "source_sha256": before["sha256"], "returncode": result.returncode})
            builds.append({"command": command, "returncode": result.returncode, "passed": result.returncode == 0 and not any(row["severity"] == "error" for row in rows),
                           "source_sha256": before["sha256"], "log_ref": log, "diagnostics": rows})
            for row in rows:
                if digest(row) not in seen:
                    diagnostics.append(row)
                    seen.add(digest(row))
        after = file_identity(root, scope=scope, lake_args=lake_args)
    stable = before == after
    errors = [row for row in diagnostics if row["severity"] == "error"]
    passed = stable and not unchecked and all(row["passed"] for row in builds)
    return {"passed": passed, "completed": passed, "source_stable": stable, "source_identity": before,
            "source_sha256": before["sha256"], "commands": commands,
            "builds": builds, "diagnostics": diagnostics, "errors": errors,
            "selected_files": requested, "selected_modules": mapped, "unchecked_files": unchecked,
            "workspace": layout,
            "warnings": [row for row in diagnostics if row["severity"] == "warning"],
            "returncode": next((row["returncode"] for row in builds if row["returncode"]), 0),
            "qualification": "Configured default targets plus selected modules resolved through actual Lake ownership; unowned/missing files are unchecked, never counted as passed. No additional executable tests are implied."}


def capture_baseline(root, *, metric, scope, measurements=None, lake_args=(), project_root=None):
    root = Path(root).resolve()
    if not _valid(metric):
        raise ValueError("baseline requires a frozen metric")
    if load_metric(root, metric["name"]) != metric:
        raise ValueError("metric definition or helper bytes changed")
    build = build_scope(root, scope=scope, lake_args=lake_args, project_root=project_root)
    identity = file_identity(root, scope=scope, lake_args=lake_args)
    if not build["source_stable"] or build["source_identity"] != identity:
        raise ValueError("source changed while recording Optimize baseline")
    if not build["passed"] and metric.get("builtin") != "completion":
        raise ValueError("baseline build failed; only completion accepts an initially broken project")
    measured = deepcopy(measurements or {})
    for row in measured.values():
        if not validate_measurement_binding(row, metric, identity):
            raise ValueError("baseline measurement is not source/metric bound")
    audit = {"sources": _source_audit(root, identity["files"])}
    if file_identity(root, scope=scope, lake_args=lake_args) != identity or load_metric(root, metric["name"]) != metric:
        raise ValueError("source or metric changed while recording Optimize baseline")
    return _seal({"schema_version": 1, "created_at": time.time(), "identity": identity,
                  "source_identity": identity, "main_sha": identity["main_sha"], "scope": deepcopy(scope),
                  "metric": deepcopy(metric), "build": build, "measurements": measured,
                  "audit": audit})


def _comparisons(baseline, measurements, metric, identity, judgments=None):
    comparisons, issues = {}, []
    original = baseline.get("measurements", {})
    if not original:
        issues.append({"kind": "missing_baseline_measurements"})
    for subject in sorted(set(original) | set(measurements)):
        before, after = original.get(subject), measurements.get(subject)
        if before is None or after is None:
            issues.append({"kind": "measurement_coverage_changed", "subject": subject})
        elif not validate_measurement_binding(before, metric, baseline["identity"]):
            issues.append({"kind": "invalid_baseline_measurement", "subject": subject})
        elif not validate_measurement_binding(after, metric, identity):
            issues.append({"kind": "stale_current_measurement", "subject": subject})
        else:
            try:
                comparisons[subject] = compare_measurements(before, after, judgment=(judgments or {}).get(subject))
            except ValueError as exc:
                issues.append({"kind": "incomparable_measurement", "subject": subject, "reason": str(exc)})
    return comparisons, issues


def _patch(root, baseline, files, owner):
    """Retain exact tracked diff and added-file bytes, bound to the file map.

    This does not claim an independent rebuild or replay of a dirty baseline.
    """
    base = baseline["identity"]
    before, after = base["files"], files
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    patch = _git(root, "diff", "--binary", base["main_sha"], "--", *changed, binary=True) if changed else b""
    extra = {}
    tracked = set(_git(root, "ls-files", "-z").split("\x00"))
    for name in changed:
        if name in after and name not in tracked:
            path = _relative(root, name)
            if not path.is_symlink():
                extra[name] = artifacts.store_text(owner / ".unity" / "artifacts", path.read_text(), kind="optimize_added_file", producer="Unity")
    record = artifacts.store_text(owner / ".unity" / "artifacts", patch.decode(), kind="optimize_patch", producer="Unity")
    return {"passed": True, "base_sha": base["main_sha"], "baseline_file_map_sha256": digest(before),
            "current_file_map_sha256": digest(after), "changed_files": changed, "patch_ref": record,
            "added_files": extra, "qualification": "Git diff plus added-file artifacts and exact baseline/current hashes; not semantic equivalence or an independent rebuild."}


def _verify(root, *, baseline, metric, scope, measurements, task=None, lake_args=(), project_root=None, judgments=None,
            comparison=None, config_revision=0, selected_files=None, final=False):
    root, owner = Path(root).resolve(), Path(project_root or root).resolve()
    if not _valid(baseline) or not _valid(metric) or baseline.get("metric") != metric:
        raise ValueError("Optimize baseline/metric is missing or has changed")
    if load_metric(root, metric["name"]) != metric:
        raise ValueError("metric definition or helper bytes changed")
    if baseline.get("scope") != scope:
        raise ValueError("Optimize scope differs from frozen baseline")
    if selected_files is None:
        selected_files = (task or {}).get("files", scope.get("files", []))
    selected_files = sorted({name for name in selected_files if name.endswith(".lean") and name != "lakefile.lean"})
    build = build_scope(root, scope=scope, lake_args=lake_args, project_root=owner, selected_files=selected_files)
    identity = file_identity(root, scope=scope, lake_args=lake_args)
    audit = audit_sources(root, baseline)
    if final and comparison is not None:
        raise ValueError("final verification compares against the original baseline")
    comparison = comparison or baseline
    if comparison is not baseline:
        if not _valid(comparison.get("identity", {})) or comparison.get("metric", metric) != metric:
            raise ValueError("candidate comparison needs valid current identity and same metric")
    comparisons, issues = _comparisons(comparison, measurements, metric, identity, judgments)
    issues.extend(audit["issues"])
    if not build["source_stable"] or build["source_identity"] != identity:
        issues.append({"kind": "snapshot_changed_during_verification"})
    if not build["passed"] and (final or metric.get("builtin") != "completion"):
        issues.append({"kind": "required_build_failed"})
    if build["unchecked_files"]:
        issues.append({"kind": "selected_files_unchecked", "files": build["unchecked_files"]})
    if not any(row["improved"] for row in comparisons.values()):
        issues.append({"kind": "no_improvement_found"})
    if any(row["regressed"] for row in comparisons.values()):
        issues.append({"kind": "metric_regression"})
    if final and metric.get("builtin") == "completion" and (audit["holes_remaining"] or any(row["raw_value"] != 0 for row in measurements.values())):
        issues.append({"kind": "completion_not_zero"})
    patch = _patch(root, baseline, identity["files"], owner)
    after = file_identity(root, scope=scope, lake_args=lake_args)
    if after != identity:
        issues.append({"kind": "source_changed_during_patch_recording"})
    if load_metric(root, metric["name"]) != metric:
        issues.append({"kind": "metric_changed_during_verification"})
    return _seal({"schema_version": 1, "snapshot_id": "optimize-review-" + uuid.uuid4().hex,
                  "created_at": time.time(), "main_sha": identity["main_sha"], "source_identity": identity,
                  "baseline_sha256": baseline["sha256"], "metric_sha256": metric["sha256"],
                  "config_revision": config_revision,
                  "selected_files": selected_files,
                  "scope": deepcopy(scope), "passed": not issues, "issues": issues,
                  "final": final, "task": deepcopy(task), "build": build, "builds": build["builds"],
                  "hole_audit": audit, "patch": patch, "measurements": deepcopy(measurements),
                  "comparisons": comparisons,
                  "qualification": "Controller builds are machine evidence. Metric records retain reported tool/agent provenance, not independent scorer execution; metric validity and semantic preservation remain primary review judgments."})


def verify_candidate(root, **kwargs):
    return _verify(root, **kwargs, final=False)


def verify_final(root, **kwargs):
    return _verify(root, **kwargs, final=True)


def completion_report(state, snapshot=None, verdict=None):
    """Pure validator/report constructor; never reads files or starts tools."""
    review = state.get("review", {})
    snapshot = snapshot or review.get("snapshot") or {}
    verdict = verdict or review.get("verdict") or {}
    accepted = state.get("phase") == "complete"
    baseline, metric = state.get("baseline", {}), state.get("metric", {})
    if accepted:
        if state.get("baseline_sealed") is not True:
            raise ValueError("accepted Optimize requires a sealed original baseline")
        if not _valid(baseline) or not _valid(metric) or not _valid(snapshot) or not snapshot.get("passed") or not snapshot.get("final"):
            raise ValueError("accepted Optimize requires intact final evidence and baseline")
        identity, build, audit, patch = (snapshot.get(key, {}) for key in ("source_identity", "build", "hole_audit", "patch"))
        active_tasks = [row for row in state.get("tasks", {}).values() if row.get("status") != "grouped"]
        if any(row.get("status") not in {"complete", "no_improvement"} for row in active_tasks):
            raise ValueError("accepted Optimize requires resolved active tasks")
        subjects = {subject for row in active_tasks for subject in row.get("chunk_ids", [])}
        original, current = baseline.get("measurements", {}), snapshot.get("measurements", {})
        if (set(original) != subjects or set(current) != subjects
                or any(original[subject].get("subject") != subject or current[subject].get("subject") != subject for subject in subjects)):
            raise ValueError("accepted Optimize requires scores for every selected task subject")
        expected_files = {name for row in active_tasks for name in row.get("files", [])}
        expected_files = {name for name in expected_files if name.endswith(".lean") and name != "lakefile.lean"}
        if (not _valid(identity) or identity.get("main_sha") != snapshot.get("main_sha")
                or snapshot.get("main_sha") != state.get("main_sha")
                or identity.get("scope") != state.get("scope") or snapshot.get("scope") != state.get("scope")
                or snapshot.get("baseline_sha256") != baseline.get("sha256")
                or snapshot.get("metric_sha256") != metric.get("sha256") or baseline.get("metric") != metric
                or snapshot.get("config_revision") != state.get("config", {}).get("revision", 0)
                or not build.get("passed") or not build.get("completed") or not build.get("source_stable")
                or build.get("source_identity") != identity or build.get("errors")
                or set(snapshot.get("selected_files", [])) != expected_files or set(build.get("selected_files", [])) != expected_files
                or set(build.get("selected_modules", {})) != expected_files or build.get("unchecked_files")
                or build.get("commands") != scope_build_commands(state.get("scope"), identity.get("lake_args", []), modules=build.get("selected_modules", {}).values())
                or not build.get("builds") or any(not row.get("passed") or row.get("returncode") != 0 or row.get("source_sha256") != identity["sha256"] for row in build["builds"])
                or not audit.get("passed") or audit.get("issues") or not patch.get("passed")
                or patch.get("current_file_map_sha256") != digest(identity.get("files")) or snapshot.get("issues")):
            raise ValueError("accepted Optimize lacks current full-build, scope, audit or patch evidence")
        comparisons, issues = _comparisons(baseline, snapshot.get("measurements", {}), metric, identity,
                                          {k: v.get("judgment") for k, v in snapshot.get("comparisons", {}).items()})
        if issues or comparisons != snapshot.get("comparisons") or not any(row["improved"] for row in comparisons.values()) or any(row["regressed"] for row in comparisons.values()):
            raise ValueError("accepted Optimize requires comparable current improvement evidence")
        if metric.get("builtin") == "completion" and (audit.get("holes_remaining") or any(row["raw_value"] != 0 for row in snapshot["measurements"].values())):
            raise ValueError("accepted completion metric has unresolved errors/holes")
        if (verdict.get("verdict") != "approved" or not review.get("reviewer")
                or review.get("reviewer") != state.get("config", {}).get("primary")
                or verdict.get("author") != review["reviewer"]
                or verdict.get("snapshot_id") != snapshot.get("snapshot_id")
                or verdict.get("snapshot_sha256") != snapshot.get("sha256")
                or verdict.get("reopen_tasks") or verdict.get("reopen_task_ids")):
            raise ValueError("accepted Optimize requires configured-primary verdict on identical snapshot")
    status = "accepted" if accepted else {"exhausted": "exhausted", "stopped": "stopped", "blocked": "blocked", "no_improvement": "no_improvement_found"}.get(state.get("phase"), "incomplete")
    return {"schema_version": 1, "pipeline": "optimize", "run_id": state.get("run_id"), "phase": state.get("phase"), "status": status,
            "project_changes": {"baseline_sha256": baseline.get("sha256"), "main_sha": state.get("main_sha"),
                                "scope": deepcopy(state.get("scope")), "metric": deepcopy(metric),
                                "baseline_measurements": deepcopy(baseline.get("measurements", {})),
                                "current_measurements": deepcopy(snapshot.get("measurements", state.get("measurements", {}))),
                                "comparisons": deepcopy(snapshot.get("comparisons", {})),
                                "build": deepcopy(snapshot.get("build", {})), "hole_audit": deepcopy(snapshot.get("hole_audit", {})),
                                "patch": deepcopy(snapshot.get("patch", {})), "review": deepcopy(verdict),
                                "not_checked": ["semantic equivalence by an independent prover", "global metric optimality", "additional executable tests unless explicitly recorded"]},
            "workflow_changes": deepcopy(state.get("workflow_changes", [])), "rounds": deepcopy(state.get("rounds", {})),
            "snapshot_id": snapshot.get("snapshot_id"), "snapshot_sha256": snapshot.get("sha256"),
            "qualification": "Compilation is machine evidence; primary review judges meaning and agent-scored metrics. No improvement found is not global optimality."}


def persist_report(paths, state, snapshot=None, verdict=None, *, accepted=None):
    report = completion_report(state, snapshot, verdict)
    if accepted is True and report["status"] != "accepted":
        raise ValueError("cannot persist an accepted report for incomplete Optimize")
    root = Path(paths.project_root) if hasattr(paths, "project_root") else Path(paths)
    directory = Path(getattr(paths, "artifacts", getattr(paths, "artifacts_dir", root / ".unity" / "artifacts")))
    refs = {}
    for key in ("project_changes", "workflow_changes"):
        refs[key] = artifacts.store_text(directory, json.dumps(report[key], indent=2, sort_keys=True) + "\n", kind="optimize_" + key, producer="Unity")
    refs["final_report"] = artifacts.store_text(directory, json.dumps(report, indent=2, sort_keys=True) + "\n", kind="optimize_final_report", producer="Unity")
    return {"report": report, "artifacts": refs, "status": report["status"]}
