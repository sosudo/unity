"""Compiler-driven Bump work, without original-source preservation contracts.

This module owns only Bump's compact coordination state.  Source scanning is a
lexical scheduling aid, never a Lean parser or a proof/acceptance checker.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from datetime import datetime, timezone

SCHEMA = "compiler-migration-v2"
STATE_FILE = "bump-workflow.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "." == str(path):
        raise ValueError("expected a project-relative path")
    if any(part in {".git", ".unity", ".lake"} for part in path.parts):
        raise ValueError("internal project paths cannot be assigned")
    return path.as_posix()


def _read(forum_dir: Path) -> dict:
    state = json.loads((Path(forum_dir) / STATE_FILE).read_text())
    if state.get("schema") != SCHEMA:
        raise ValueError("not compiler-driven Bump state; legacy checkpoints require explicit migration")
    return state


def load(forum_dir: Path) -> dict:
    """Atomic readers see either the previous or next complete JSON revision."""
    return _read(forum_dir)


def _write(forum_dir: Path, state: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".bump-workflow-", dir=forum_dir)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, Path(forum_dir) / STATE_FILE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def transaction(forum_dir: Path, *, expected_revision: int | None = None):
    """Exclusive atomic mutation; exceptions never publish partial changes."""
    forum_dir = Path(forum_dir)
    forum_dir.mkdir(parents=True, exist_ok=True)
    with (forum_dir / "bump-workflow.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            state = _read(forum_dir)
            if expected_revision is not None and state["revision"] != expected_revision:
                raise ValueError("stale state revision")
            revision = state["revision"]
            yield state
            state["revision"] = revision + 1
            state["updated_at"] = now()
            _write(forum_dir, state)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def initialize(forum_dir: Path, root: Path, *, run_id: str, base_sha: str,
               version: str, config: dict | None = None) -> dict:
    forum_dir = Path(forum_dir)
    forum_dir.mkdir(parents=True, exist_ok=True)
    with (forum_dir / "bump-workflow.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if (forum_dir / STATE_FILE).exists():
            raise ValueError("Bump workflow already initialized")
        settings = {"revision": 0, "lake_args": [], "scope": {}}
        settings.update(deepcopy(config or {}))
        state = {"schema": SCHEMA, "root": str(Path(root).resolve()), "run_id": run_id,
                 "base_sha": base_sha, "main_sha": base_sha, "target_version": version,
                 "revision": 0, "phase": "preparing", "created_at": now(),
                 "config": settings, "chunks": {}, "files": {}, "tasks": {},
                 "candidates": {}, "leases": {}, "strategies": {}, "events": [],
                 "config_requests": {}, "workflow_changes": [], "review": {},
                 "diagnostics": [], "limitations": [], "chunk_attempts": {}}
        _write(forum_dir, state)
        return deepcopy(state)


def mask_source(source: str) -> str:
    """Blank comments and strings while preserving offsets and line numbers.

    Lean quoted identifiers are intentionally retained, including spaces.
    Nested block comments and escaped quotes in strings are recognized.
    """
    chars = list(source)
    i, depth, string, quoted = 0, 0, False, False
    while i < len(source):
        ch, pair = source[i], source[i:i + 2]
        if depth:
            if pair == "/-":
                chars[i:i + 2] = "  "; depth += 1; i += 2; continue
            if pair == "-/":
                chars[i:i + 2] = "  "; depth -= 1; i += 2; continue
            if ch != "\n": chars[i] = " "
        elif string:
            if ch == "\\":
                chars[i] = " "
                if i + 1 < len(source):
                    if source[i + 1] != "\n": chars[i + 1] = " "
                    i += 2; continue
            if ch == '"': string = False
            if ch != "\n": chars[i] = " "
        elif quoted:
            if ch == "»": quoted = False
        elif ch == "«":
            quoted = True
        elif pair == "/-":
            chars[i:i + 2] = "  "; depth = 1; i += 2; continue
        elif pair == "--":
            end = source.find("\n", i)
            if end < 0: end = len(source)
            chars[i:end] = " " * (end - i); i = end; continue
        elif ch == '"':
            chars[i] = " "; string = True
        i += 1
    return "".join(chars)


_IDENT = r"(?:«[^»\n]+»|[\w\u0080-\uffff][\w\u0080-\uffff'.]*)"
_COMMAND = re.compile(
    r"^\s*(?:@\[[^\n]*?\]\s*)*(?:(?:private|protected|noncomputable|unsafe|partial|local|scoped)\s+)*"
    r"(?P<kind>theorem|lemma|def|abbrev|opaque|axiom|constant|instance|inductive|structure|class|example|"
    r"namespace|section|end|mutual|import|open|variable|universe|set_option|attribute|export|"
    r"syntax|macro_rules|macro|elab_rules|elab|notation|infixl|infixr|infix|prefix|postfix|initialize|#exit)"
    r"(?=\s|$|\[|\(|:)(?P<rest>.*)$")
_DECL = {"theorem", "lemma", "def", "abbrev", "opaque", "axiom", "constant", "instance",
         "inductive", "structure", "class", "example", "initialize"}


def scan_sources(root: Path, files) -> dict:
    """Inventory current source, including declarations without diagnostics.

    Ranges are line-based lexical approximations.  Ambiguous compiler locations
    are assigned command/file repair work rather than silently ignored.
    """
    root = Path(root).resolve()
    inventory = {"files": {}, "chunks": {}, "limitations": [
        "Lexical source-command ranges are scheduling hints, not elaboration or semantic evidence."]}
    for value in sorted(files):
        path = relative_path(str(value))
        source_path = root / path
        if not source_path.resolve().is_relative_to(root):
            raise ValueError("source symlink escapes project")
        source = source_path.read_text()
        masked = mask_source(source)
        lines = masked.splitlines()
        commands = [(number, match) for number, line in enumerate(lines, 1)
                    if (match := _COMMAND.match(line))]
        imports, namespace, blocks, mutual, counts = [], [], [], None, {}
        exit_line = next((n for n, m in commands if m["kind"] == "#exit"), None)
        for index, (line, match) in enumerate(commands):
            kind, rest = match["kind"], match["rest"].strip()
            if kind == "import":
                if exit_line is None or line < exit_line:
                    imports.extend(re.findall(_IDENT, rest))
                continue
            if kind in {"namespace", "section", "mutual"}:
                name = re.match(_IDENT, rest)
                blocks.append((kind, len(namespace), mutual))
                if kind == "namespace" and name: namespace.append(name[0])
                if kind == "mutual": mutual = "mutual-" + digest([path, line])[:16]
                continue
            if kind == "end":
                if blocks:
                    _, length, previous = blocks.pop(); namespace = namespace[:length]; mutual = previous
                continue
            if kind not in _DECL:
                continue
            name_match = re.match(_IDENT, rest)
            name = name_match[0] if name_match else ""
            if kind in {"example", "initialize"} or (kind == "instance" and (not name or rest.startswith(("[", "(", ":")))):
                name = "<anonymous-" + kind + ">"
            if not name:
                name = "<unparsed-" + kind + ">"
            qualified = ".".join([*namespace, name])
            counts[qualified] = counts.get(qualified, 0) + 1
            key = "chunk-" + digest([path, qualified, counts[qualified]])[:20]
            end_line = commands[index + 1][0] - 1 if index + 1 < len(commands) else len(lines)
            inventory["chunks"][key] = {"chunk_id": key, "path": path, "name": qualified,
                "kind": kind, "start_line": line, "end_line": max(line, end_line),
                "group_id": mutual, "after_exit": exit_line is not None and line > exit_line,
                "source_sha256": hashlib.sha256("\n".join(source.splitlines()[line - 1:end_line]).encode()).hexdigest(),
                "status": "skipped" if exit_line is not None and line > exit_line else "unobserved"}
        inventory["files"][path] = {"sha256": hashlib.sha256(source.encode()).hexdigest(),
                                      "imports": sorted(set(imports)), "exit_line": exit_line}
    return inventory


def _event(state: dict, kind: str, author: str, **details) -> dict:
    entry = {"event_id": "event-" + str(len(state["events"]) + 1), "at": now(),
             "kind": kind, "author": author, **details}
    state["events"].append(entry)
    return entry


def _task(task_id: str, kind: str, chunks: list[str], files: list[str]) -> dict:
    return {"task_id": task_id, "kind": kind, "chunk_ids": chunks, "files": files,
            "dependencies": [], "status": "pending", "revision": 0, "attempts": 0, "owner": None}


def _declaration_resolution(task: dict, inventory: dict, rows: list, build: dict) -> dict | None:
    """Current compiler progress for an entire task, not candidate acceptance.

    The controller supplies only finished, identity-bound project builds. Source
    hashes bind the freshly scanned ranges to that build; absence of an error by
    itself says nothing about whether Lean reached a declaration.
    """
    if (not build or build.get("source_stable") is not True
            or build.get("diagnostics") != rows or build.get("unmapped")
            or not task.get("diagnostics") or task.get("kind") != "repair"):
        return None
    chunks = inventory["chunks"]
    members, files = set(task.get("chunk_ids", [])), set(task.get("files", []))
    if not members or not files or not members <= chunks.keys():
        return None
    assigned = [chunks[cid] for cid in members]
    if any(c.get("after_exit") or c.get("scope_excluded") or c.get("scope_unclassified")
           or c.get("status") in {"excluded", "skipped"} or c.get("path") not in files
           or type(c.get("start_line")) is not int or type(c.get("end_line")) is not int
           or not 0 < c["start_line"] <= c["end_line"] for c in assigned):
        return None
    groups = {c["group_id"] for c in assigned if c.get("group_id")}
    if any(cid not in members for cid, c in chunks.items() if c.get("group_id") in groups):
        return None
    # Only compiler-origin repairs qualify, never an explicit noncompiler task.
    if any(d.get("severity", "error") != "error" or d.get("path") not in files
           or d.get("kind") not in {None, "declaration"} for d in task["diagnostics"]):
        return None
    source_files = (build.get("source_identity") or {}).get("files", {})
    if any(not inventory["files"].get(path, {}).get("sha256")
           or inventory["files"][path]["sha256"] != source_files.get(path) for path in files):
        return None
    errors = [d for d in rows if d.get("severity", "error") == "error"]
    if any(not d.get("path") for d in errors):
        return None
    checked, evidence = set(build.get("checked_files", [])), {}
    for path in sorted(files):
        located = [d for d in errors if d.get("path") == path]
        if any(d.get("kind") == "import" or type(d.get("line")) is not int
               or d["line"] <= 0 for d in located):
            return None
        if path in checked and not located:
            evidence[path] = {"basis": "checked_file"}
            continue
        end = max((c["end_line"] for c in assigned if c["path"] == path), default=0)
        first_error = min((d["line"] for d in located), default=0)
        if not end or first_error <= end:
            return None
        evidence[path] = {"basis": "compiler_reached_later_error",
                          "end_line": end, "first_error_line": first_error}
    return evidence


def refresh(forum_dir: Path, inventory: dict, diagnostics, *, main_sha: str,
            config_revision: int, checked_files=(), complete: bool = False,
            reconcile_environment: bool = False, declaration_build: dict | None = None) -> dict:
    """Refresh compiler tasks; a supplied declaration build guards completion.

    None retains the low-level checked-file API. Controllers pass an empty dict
    when evidence is untrusted, so those files cannot bypass declaration guards.
    """
    rows = deepcopy(diagnostics.get("diagnostics", []) if isinstance(diagnostics, dict) else diagnostics)
    environment_clear = reconcile_environment and not any(
        d.get("severity", "error") == "error" and not d.get("path") for d in rows)
    with transaction(forum_dir) as state:
        if state["config"]["revision"] != config_revision:
            raise ValueError("diagnostics use a stale configuration")
        old_chunks = state["chunks"]
        chunks = deepcopy(inventory["chunks"])
        checked = set(checked_files) if declaration_build is None or declaration_build else set()
        failures = {d.get("path") for d in rows if d.get("severity", "error") == "error"}
        for cid, row in chunks.items():
            old = old_chunks.get(cid, {})
            if row["after_exit"] or row.get("scope_excluded") or row.get("status") == "excluded":
                continue
            if state["main_sha"] == main_sha and old.get("source_sha256") == row["source_sha256"]:
                row["status"] = old.get("status", "unobserved")
            if row["path"] in checked and row["path"] not in failures:
                row["status"] = "clean"
            elif complete and not failures and row["path"] in checked:
                row["status"] = "clean"
            elif row["path"] in failures:
                row["status"] = "unobserved"
        state.update(chunks=chunks, files=deepcopy(inventory["files"]), main_sha=main_sha,
                     diagnostics=rows, limitations=inventory.get("limitations", []))
        touched, reset_diagnostics = set(), set()
        for diagnostic in rows:
            if diagnostic.get("severity", "error") != "error":
                continue
            path, line = diagnostic.get("path"), diagnostic.get("line")
            matching = [cid for cid, row in chunks.items() if row["path"] == path and not row["after_exit"]
                        and not row.get("scope_excluded") and row.get("status") != "excluded"
                        and line and row["start_line"] <= line <= row["end_line"]]
            if matching:
                groups = {chunks[c]["group_id"] for c in matching if chunks[c]["group_id"]}
                matching = sorted(set(matching) | {c for c, r in chunks.items() if r["group_id"] in groups and not r["after_exit"]
                                                    and not r.get("scope_excluded") and r.get("status") != "excluded"})
                for cid in matching: chunks[cid]["status"] = "needs_repair"
                existing = [t for t in state["tasks"].values() if set(t["chunk_ids"]) & set(matching)
                            and t["status"] != "grouped"]
                task = existing[0] if existing else _task("repair-" + digest(matching)[:16], "repair", matching, [path])
            else:
                task_id = "command-" + digest([path, diagnostic.get("kind", "command")])[:16]
                excluded = path in state["config"].get("scope", {}).get("excluded_files", {})
                task = state["tasks"].get(task_id) or _task(task_id, "scope_review" if excluded else ("command" if path else "configuration"), [], [path] if path else [])
            state["tasks"][task["task_id"]] = task
            touched.add(task["task_id"])
            if task["status"] in {"complete", "yielded", "failed", "blocked"}: task["status"] = "pending"
            task.setdefault("diagnostics", [])
            if task["task_id"] not in reset_diagnostics:
                task["diagnostics"] = []; reset_diagnostics.add(task["task_id"])
            task["diagnostics"].append(diagnostic)
        for tid, task in state["tasks"].items():
            if tid not in touched and task["status"] not in {"running", "submitted", "merging", "grouped"}:
                review_repair = any(task.get(key) for key in (
                    "audit_feedback", "critic_feedback", "review_snapshot_id", "grouped_review_feedback"))
                resolution = None
                if (declaration_build is not None and not review_repair and task["status"] != "complete"
                        and task.get("owner") is None
                        and not any(lease.get("task_id") == tid or path in task["files"]
                                    for path, lease in state["leases"].items())
                        and not any(candidate.get("task_id") == tid
                                    and candidate.get("status") in {"submitted", "merging"}
                                    for candidate in state["candidates"].values())):
                    resolution = _declaration_resolution(task, inventory, rows, declaration_build)
                if resolution is not None:
                    resolved = [d.get("id") or "diagnostic-" + digest(d)[:20] for d in task["diagnostics"]]
                    task.update(status="complete", diagnostics=[])
                    for cid in task["chunk_ids"]:
                        chunks[cid]["status"] = "clean"
                    _event(state, "declaration_resolved", "controller", task_id=tid,
                           main_sha=main_sha, config_revision=config_revision,
                           resolved_diagnostic_ids=resolved, evidence=resolution,
                           source_sha256=declaration_build.get("source_sha256"),
                           log_ref=deepcopy(declaration_build.get("log_ref")))
                elif (not review_repair and task["files"] and set(task["files"]) <= checked
                        # With authoritative declaration evidence, use the same
                        # ownership/range guards even for fully checked files.
                        and (declaration_build is None or task["kind"] != "repair")
                        and not set(task["files"]) & failures):
                    task["status"] = "complete"
                elif (environment_clear and not review_repair and task["status"] != "complete"
                        and task["kind"] == "configuration" and tid not in state.get("config_requests", {})
                        and task.get("diagnostics") and all(
                            d.get("severity", "error") == "error" and not d.get("path")
                            and d.get("kind") == "environment"
                            and tid == "command-" + digest([d.get("path"), d["kind"]])[:16]
                            for d in task["diagnostics"])
                        and task.get("owner") is None
                        and not any(lease.get("task_id") == tid or path in task["files"]
                                    for path, lease in state["leases"].items())
                        and not any(candidate.get("task_id") == tid
                                    and candidate.get("status") in {"submitted", "merging"}
                                    for candidate in state["candidates"].values())):
                    # Only an authoritative project build can retire an obsolete
                    # environment diagnostic; compiled-file lists cannot cover
                    # lean-toolchain or lake-manifest.json. Keep its retry history.
                    task["status"] = "complete"
                    _event(state, "configuration_resolved", "controller", task_id=tid,
                           reason="Authoritative project build has no pathless error diagnostics",
                           main_sha=main_sha, config_revision=config_revision)
        # Reconcile every status before rebuilding edges: task IDs/insertion
        # order must not leave stale prerequisites after indirect repairs.
        for tid, task in state["tasks"].items():
            if task["kind"] not in {"repair", "command"}: continue
            modules = {p.removesuffix(".lean").replace("/", "."): p for p in state["files"]}
            imported = {modules[m] for p in task["files"] for m in state["files"].get(p, {}).get("imports", []) if m in modules}
            task["dependencies"] = sorted(other_id for other_id, other in state["tasks"].items()
                if other_id != tid and other["status"] not in {"complete", "grouped"} and imported & set(other["files"]))
            # Lean source commands elaborate in order. Without compiled name
            # information, earlier failed commands in the same file are a safe
            # scheduling prerequisite, not a claimed semantic dependency DAG.
            starts = {path: min((chunks[cid]["start_line"] for cid in task["chunk_ids"]
                      if cid in chunks and chunks[cid]["path"] == path), default=0) for path in task["files"]}
            for other_id, other in state["tasks"].items():
                if other_id == tid or other["status"] in {"complete", "grouped"}:
                    continue
                for path in set(task["files"]) & set(other["files"]):
                    earlier = min((chunks[cid]["start_line"] for cid in other["chunk_ids"]
                                   if cid in chunks and chunks[cid]["path"] == path), default=0)
                    if earlier < starts[path]:
                        task["dependencies"] = sorted(set(task["dependencies"]) | {other_id})
            task["dependency_basis"] = "current imports and conservative source-command order"
        state["review"] = {}
    return load(forum_dir)


def claim(forum_dir: Path, author: str, task_id: str, max_attempts: int = 5) -> dict:
    with transaction(forum_dir) as state:
        task = state["tasks"][task_id]
        if task["status"] not in {"pending", "yielded", "blocked"}:
            raise ValueError("task is not available")
        if any(state["tasks"][key]["status"] != "complete" for key in task["dependencies"]):
            raise ValueError("task has unfinished dependencies")
        if task["attempts"] >= max_attempts or any(state["chunk_attempts"].get(cid, 0) >= max_attempts for cid in task["chunk_ids"]):
            raise ValueError("task attempt budget exhausted")
        for path in task["files"]:
            relative_path(path)
            if path in state["leases"]: raise ValueError("file has an active writer: " + path)
        if task.get("owner") is not None:
            raise ValueError("previous writer closure must be recorded before reassignment")
        previous_yield = task.pop("yield_requested", None)
        if previous_yield:
            _event(state, "yield_cleared", author, task_id=task_id, previous_yield=previous_yield)
        task.update(owner=author, status="running", attempts=task["attempts"] + 1, revision=task["revision"] + 1)
        for cid in task["chunk_ids"]: state["chunk_attempts"][cid] = state["chunk_attempts"].get(cid, 0) + 1
        for path in task["files"]:
            state["leases"][path] = {"task_id": task_id, "author": author, "task_revision": task["revision"]}
        task["base_main_sha"] = state["main_sha"]
        task["config_revision"] = state["config"]["revision"]
        _event(state, "claim", author, task_id=task_id)
        result = deepcopy(task)
    return result


def configure_assignment(forum_dir: Path, author: str, task_id: str, worktree: Path,
                         base_main_sha: str) -> dict:
    with transaction(forum_dir) as state:
        task = _owned(state, author, task_id)
        if base_main_sha != state["main_sha"]: raise ValueError("assignment base is stale")
        task.update(worktree=str(Path(worktree).resolve()), base_main_sha=base_main_sha)
        result = deepcopy(task)
    return result


def _owned(state: dict, author: str, task_id: str) -> dict:
    task = state["tasks"][task_id]
    if task.get("owner") != author or task["status"] not in {"running", "submitted", "merging"}:
        raise ValueError("task is not owned by this active writer")
    return task


def release(forum_dir: Path, author: str, task_id: str, reason: str, closed: bool = True) -> dict:
    with transaction(forum_dir) as state:
        task = state["tasks"][task_id]
        if task.get("owner") != author: raise ValueError("task owner mismatch")
        if not closed: raise ValueError("writer closure must be confirmed before releasing files")
        if task["status"] in {"submitted", "merging"}: raise ValueError("candidate still awaits integration")
        for path, lease in list(state["leases"].items()):
            if lease["task_id"] == task_id: del state["leases"][path]
        task["owner"] = None
        if task["status"] not in {"complete", "failed", "grouped"}: task["status"] = "pending"
        previous_yield = task.pop("yield_requested", None)
        _event(state, "release", author, task_id=task_id, reason=reason, previous_yield=previous_yield)
        result = deepcopy(task)
    return result


def request_scope(forum_dir: Path, author: str, task_id: str, chunk_ids: list[str],
                  files: list[str], reason: str, expected_revision: int) -> dict:
    if not reason.strip(): raise ValueError("scope changes require a reason")
    with transaction(forum_dir) as state:
        task = _owned(state, author, task_id)
        if task["status"] != "running" or task["revision"] != expected_revision:
            raise ValueError("stale or submitted assignment")
        members = set(task["chunk_ids"]) | set(chunk_ids)
        if not members <= state["chunks"].keys(): raise ValueError("unknown chunk")
        paths = sorted(set(task["files"]) | {relative_path(p) for p in files} | {state["chunks"][c]["path"] for c in members})
        if task["kind"] not in {"configuration", "dependency", "scope_review"} and any(
            Path(p).name in {"lean-toolchain", "lakefile.lean", "lakefile.toml", "lake-manifest.json"}
            or "vendor" in Path(p).parts for p in paths):
            raise ValueError("configuration and vendored dependency edits need a controller configuration assignment")
        if any(state["chunks"][cid].get("scope_excluded") or state["chunks"][cid].get("after_exit") for cid in members):
            raise ValueError("changing compiled scope requires controller review")
        for path in paths:
            if path in state["leases"] and state["leases"][path]["task_id"] != task_id:
                raise ValueError("scope conflicts with another active writer")
        absorbed = [other for key, other in state["tasks"].items() if key != task_id
                    and set(other["chunk_ids"]) & members and other["status"] != "grouped"]
        for other in absorbed:
            if other.get("owner"): raise ValueError("scope includes another owned assignment")
            feedback = {key: deepcopy(other[key]) for key in (
                "audit_feedback", "critic_feedback", "review_snapshot_id", "grouped_review_feedback") if other.get(key)}
            if feedback:
                task.setdefault("grouped_review_feedback", []).append({"task_id": other["task_id"], **feedback})
            members.update(other["chunk_ids"])
            for path in other["files"]:
                if path in state["leases"] and state["leases"][path]["task_id"] != task_id:
                    raise ValueError("scope conflicts with another active writer")
                if path not in paths: paths.append(path)
            other.update(status="grouped", grouped_into=task_id)
        absorbed_ids = {other["task_id"] for other in absorbed}
        for other in state["tasks"].values():
            other["dependencies"] = sorted((set(other["dependencies"]) - absorbed_ids) |
                ({task_id} if set(other["dependencies"]) & absorbed_ids and other["task_id"] != task_id else set()))
        task["dependencies"] = sorted(set(task["dependencies"]) | {
            dep for other in absorbed for dep in other["dependencies"] if dep != task_id and dep not in absorbed_ids})
        task.update(chunk_ids=sorted(members), files=sorted(paths), revision=task["revision"] + 1,
                    reason=reason, attempts=max([task["attempts"], *[other["attempts"] for other in absorbed]]))
        for path in paths: state["leases"][path] = {"task_id": task_id, "author": author, "task_revision": task["revision"]}
        _event(state, "scope_change", author, task_id=task_id, reason=reason, chunk_ids=sorted(members), files=paths)
        result = deepcopy(task)
    return result


def _submit_candidate(state: dict, author: str, task_id: str, *, commit_sha: str,
                     base_main_sha: str, task_revision: int, config_revision: int,
                     files: list[str], notes: str = "", diff_sha256: str = "", artifact: dict | None = None) -> dict:
    task = _owned(state, author, task_id)
    if task["status"] != "running" or task["revision"] != task_revision:
        raise ValueError("stale or already submitted assignment")
    if config_revision != state["config"]["revision"] or config_revision != task["config_revision"]:
        raise ValueError("stale candidate configuration")
    if base_main_sha != task["base_main_sha"]: raise ValueError("candidate base mismatch")
    checked = sorted({relative_path(path) for path in files})
    if not checked: raise ValueError("candidate contains no changes")
    if not set(checked) <= set(task["files"]): raise ValueError("candidate changes unassigned files")
    for path in checked:
        if state["leases"].get(path, {}).get("task_id") != task_id:
            raise ValueError("candidate file lease missing")
    cid = "candidate-" + digest([task_id, author, commit_sha, task_revision])[:20]
    candidate = {"candidate_id": cid, "task_id": task_id, "author": author, "status": "submitted",
        "submitted": now(), "commit_sha": commit_sha, "base_main_sha": base_main_sha,
        "task_revision": task_revision, "config_revision": config_revision, "files": checked,
        "notes": notes, "diff_sha256": diff_sha256, "artifact": artifact}
    if cid in state["candidates"]: raise ValueError("candidate already submitted")
    state["candidates"][cid] = candidate
    task["status"] = "submitted"
    _event(state, "candidate_submitted", author, task_id=task_id, candidate_id=cid)
    return candidate


def submit_candidate(forum_dir: Path, author: str, task_id: str, **kwargs) -> dict:
    with transaction(forum_dir) as state:
        return _submit_candidate(state, author, task_id, **kwargs)


def record_workflow_change(forum_dir: Path, author: str, *, original: str, problem: str,
                           evidence: str, revised: str, result: str) -> dict:
    if not all(value.strip() for value in (original, problem, evidence, revised, result)):
        raise ValueError("workflow changes require procedure, evidence, revision and observed result")
    with transaction(forum_dir) as state:
        row = {"id": "workflow-" + str(len(state["workflow_changes"]) + 1), "at": now(),
               "author": author, "original": original, "problem": problem, "evidence": evidence,
               "revised": revised, "result": result}
        state["workflow_changes"].append(row)
    return row
