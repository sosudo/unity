"""Optimize-owned durable coordination, not metric or proof acceptance.

Source ranges are lexical scheduling hints.  Candidate correctness, actual Git
patches and measurements are checked by the controller, never inferred from a
worker saying that a task is complete.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

SCHEMA = "metric-optimization-v1"
STATE_FILE = "optimize-workflow.json"


class PlanValidationError(ValueError):
    """A bounded, actionable plan rejection, safe for forum error artifacts."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.details = details


def _plan_error(code: str, message: str, **collections):
    details = {"inventory_tool": "optimize_inventory", "sample_limit": 10}
    for key, values in collections.items():
        values = sorted(set(values))
        details[key + "_count"] = len(values)
        details[key + "_sample"] = [str(value)[:240] for value in values[:10]]
    return PlanValidationError(code, message, **details)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or str(path) == ".":
        raise ValueError("expected a project-relative path")
    if any(part in {".git", ".unity", ".lake"} for part in path.parts):
        raise ValueError("internal project paths cannot be assigned")
    return path.as_posix()


def load(forum_dir: Path) -> dict:
    state = json.loads((Path(forum_dir) / STATE_FILE).read_text())
    if state.get("schema") != SCHEMA:
        raise ValueError("not metric-driven Optimize state; legacy checkpoints need explicit migration")
    return state


def _write(forum_dir: Path, state: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".optimize-workflow-", dir=forum_dir)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, forum_dir / STATE_FILE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def transaction(forum_dir: Path, *, expected_revision: int | None = None):
    """Publish one revision or none; a sealed baseline cannot be rewritten."""
    forum_dir = Path(forum_dir)
    forum_dir.mkdir(parents=True, exist_ok=True)
    with (forum_dir / "optimize-workflow.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            state = load(forum_dir)
            revision = state["revision"]
            if expected_revision is not None and expected_revision != revision:
                raise ValueError("stale state revision")
            baseline, sealed = deepcopy(state["baseline"]), state["baseline_sealed"]
            original = {key: deepcopy(value) for key, value in baseline.items() if key not in {"measurements", "sha256"}}
            yield state
            if baseline and original != {key: value for key, value in state["baseline"].items() if key not in {"measurements", "sha256"}}:
                raise ValueError("original baseline is immutable")
            if sealed and (not state["baseline_sealed"] or state["baseline"] != baseline):
                raise ValueError("sealed baseline is immutable")
            state["revision"] = revision + 1
            state["updated_at"] = now()
            _write(forum_dir, state)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def initialize(forum_dir: Path, root: Path, *, run_id: str, base_sha: str,
               metric: dict | str, scope=None, config: dict | None = None,
               baseline: dict | None = None) -> dict:
    directory = Path(forum_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "optimize-workflow.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if (directory / STATE_FILE).exists():
            raise ValueError("Optimize workflow already initialized")
        settings = {"revision": 0}
        settings.update(deepcopy(config or {}))
        state = {"schema": SCHEMA, "root": str(Path(root).resolve()), "run_id": run_id,
                 "base_sha": base_sha, "main_sha": base_sha, "phase": "preparation", "revision": 0,
                 "metric": deepcopy(metric), "scope": deepcopy(scope), "config": settings,
                 "created_at": now(), "baseline": deepcopy(baseline or {}), "baseline_sealed": False,
                 "baseline_measurements": {}, "chunks": {}, "files": {}, "tasks": {},
                 "plan_proposals": {}, "candidates": {}, "strategies": {}, "assignments": {},
                 "measurements": {}, "events": [], "workflow_changes": [], "scope_requests": {},
                 "rounds": {"started": 0, "completed": 0, "active": None, "history": []},
                 "review": {}, "limitations": [], "integration": None}
        _write(directory, state)
        return deepcopy(state)


def _event(state: dict, kind: str, author: str, **details) -> dict:
    row = {"event_id": "event-" + str(len(state["events"]) + 1), "at": now(),
           "kind": kind, "author": author, **deepcopy(details)}
    state["events"].append(row)
    return deepcopy(row)


def _primary(state: dict, author: str) -> None:
    primary = state["config"].get("primary")
    if not primary or author != primary:
        raise ValueError("operation requires the configured primary")


def _agent(state: dict, author: str) -> None:
    roster = state["config"].get("agents", state["config"].get("roster", []))
    names = {row if isinstance(row, str) else row["name"] for row in roster}
    if not author.strip() or (names and author not in names):
        raise ValueError("unknown agent")


def begin_phase_dispatch(forum_dir: Path, profile: str, authors: list[str]) -> dict:
    """Bind a non-task turn without rewriting legacy phase-completion flags."""
    profiles = {"architect", "exploration", "chunking", "optimizing", "critic", "retrospective"}
    if profile not in profiles or not authors or len(authors) != len(set(authors)):
        raise ValueError("phase dispatch needs a known profile and distinct authors")
    with transaction(forum_dir) as state:
        if state["phase"] != profile:
            raise ValueError("phase dispatch does not match the current phase")
        for author in authors:
            _agent(state, author)
        dispatches = state.setdefault("phase_dispatches", {})
        if any(row["status"] == "running" for row in dispatches.values()):
            raise ValueError("a phase dispatch is already running")
        did = "phase-dispatch-" + str(len(dispatches) + 1)
        row = {"dispatch_id": did, "profile": profile, "phase": state["phase"],
               "authors": list(authors), "status": "running", "results": {},
               "main_sha": state["main_sha"], "config_revision": state["config"]["revision"],
               "started_at": now()}
        dispatches[did] = row
        _event(state, "phase_dispatched", "controller", dispatch_id=did, profile=profile)
    return deepcopy(row)


def record_phase_result(forum_dir: Path, author: str, dispatch_id: str, status: str,
                        summary: str, evidence: dict | list | str) -> dict:
    """Record preparation contribution, not proof or phase acceptance."""
    if status not in {"complete", "blocked", "skipped"}:
        raise ValueError("phase result status must be complete, blocked or skipped")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("phase result needs a nonempty summary")
    if not isinstance(evidence, (dict, list, str)) or not evidence or isinstance(evidence, str) and not evidence.strip():
        raise ValueError("phase result needs nonempty evidence")
    with transaction(forum_dir) as state:
        _agent(state, author)
        dispatch = state.get("phase_dispatches", {}).get(dispatch_id)
        if not dispatch or dispatch["status"] != "running":
            raise ValueError("phase result requires the current running dispatch")
        if dispatch["profile"] not in {"architect", "exploration", "chunking"}:
            raise ValueError("phase results are only reported during preparation")
        if state["phase"] != dispatch["phase"] or dispatch["profile"] != state["phase"]:
            raise ValueError("phase result does not match the current phase")
        if author not in dispatch["authors"]:
            raise ValueError("author is not assigned to this phase dispatch")
        if dispatch["main_sha"] != state["main_sha"] or dispatch["config_revision"] != state["config"]["revision"]:
            raise ValueError("phase dispatch source or configuration binding is stale")
        if status == "skipped" and dispatch["profile"] != "architect":
            raise ValueError("only optional architect setup may be skipped")
        row = {"author": author, "dispatch_id": dispatch_id, "profile": dispatch["profile"],
               "status": status, "summary": summary.strip(), "evidence": deepcopy(evidence), "at": now()}
        dispatch["results"][author] = row
        _event(state, "phase_result", author, dispatch_id=dispatch_id, status=status)
    return deepcopy(row)


def close_phase_dispatch(forum_dir: Path, dispatch_id: str, status: str, reason: str = "") -> dict:
    """Controller-only completion accounting; does not advance phase or budget."""
    if status not in {"complete", "blocked", "interrupted"}:
        raise ValueError("invalid terminal phase dispatch status")
    with transaction(forum_dir) as state:
        row = state.get("phase_dispatches", {}).get(dispatch_id)
        if not row or row["status"] != "running":
            raise ValueError("phase dispatch is not running")
        row.update(status=status, reason=reason, finished_at=now())
        _event(state, "phase_dispatch_closed", "controller", dispatch_id=dispatch_id, status=status)
    return deepcopy(row)


def set_baseline(forum_dir: Path, baseline: dict) -> dict:
    if not baseline:
        raise ValueError("baseline evidence required")
    with transaction(forum_dir) as state:
        if state["baseline"]:
            raise ValueError("original baseline already captured")
        state["baseline"] = deepcopy(baseline)
    return deepcopy(baseline)


def record_baseline_measurement(forum_dir: Path, author: str, chunk_id: str, measurement: dict) -> dict:
    with transaction(forum_dir) as state:
        _agent(state, author)
        if state["baseline_sealed"] or state["phase"] != "chunking":
            raise ValueError("baseline measurements are only collected during initial chunking")
        if chunk_id not in state["chunks"]:
            raise ValueError("unknown baseline chunk")
        if measurement.get("subject") != chunk_id:
            raise ValueError("baseline measurement subject does not match chunk")
        from . import optimize_metrics
        identity = state["baseline"].get("source_identity", state["baseline"].get("identity"))
        if identity is None or not optimize_metrics.validate_measurement_binding(measurement, state["metric"], identity):
            raise ValueError("baseline measurement does not bind baseline source and metric")
        previous = state["baseline_measurements"].get(chunk_id)
        history = state.setdefault("baseline_measurement_history", {}).setdefault(chunk_id, [])
        if previous != measurement:
            if previous and not history:
                prior = next((event for event in reversed(state["events"])
                              if event["kind"] in {"baseline_measurement", "baseline_measurement_revised"}
                              and event.get("chunk_id") == chunk_id), {})
                history.append({"author": prior.get("author", "unrecorded"),
                                "at": prior.get("at"), "measurement": deepcopy(previous)})
            history.append({"author": author, "at": now(), "measurement": deepcopy(measurement)})
        state["baseline_measurements"][chunk_id] = deepcopy(measurement)
        _event(state, "baseline_measurement_revised" if previous and previous != measurement else "baseline_measurement",
               author, chunk_id=chunk_id, previous_sha256=(previous or {}).get("sha256"),
               measurement_sha256=measurement.get("sha256"))
    return deepcopy(measurement)


def seal_baseline(forum_dir: Path) -> dict:
    with transaction(forum_dir) as state:
        if not state["baseline"]:
            raise ValueError("baseline has not been captured")
        if not state["baseline_sealed"]:
            selected = {cid for task in state["tasks"].values() for cid in task["chunk_ids"]}
            measurements = state["baseline_measurements"]
            state["baseline"]["measurements"] = deepcopy({cid: row for cid, row in measurements.items()
                                                           if not state["tasks"] or cid in selected})
            state["baseline"]["sha256"] = digest({key: value for key, value in state["baseline"].items() if key != "sha256"})
            state["baseline_sealed"] = True
            _event(state, "baseline_sealed", "controller")
        baseline = deepcopy(state["baseline"])
    return baseline


def mask_source(source: str) -> str:
    """Blank nested comments and strings without moving declaration offsets."""
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
        elif ch == "«": quoted = True
        elif pair == "/-":
            chars[i:i + 2] = "  "; depth = 1; i += 2; continue
        elif pair == "--":
            end = source.find("\n", i)
            if end < 0: end = len(source)
            chars[i:end] = " " * (end - i); i = end; continue
        elif ch == '"': chars[i] = " "; string = True
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
    """Inventory all declarations, including compiling ones, without Lean calls."""
    root = Path(root).resolve()
    inventory = {"files": {}, "chunks": {}, "limitations": [
        "Lexical declaration ranges are scheduling hints, not parsing, semantic or correctness evidence."]}
    for value in sorted(files):
        path = relative_path(str(value))
        source_path = root / path
        if not source_path.resolve().is_relative_to(root):
            raise ValueError("source symlink escapes project")
        source = source_path.read_text()
        lines = mask_source(source).splitlines()
        commands = [(n, m) for n, line in enumerate(lines, 1) if (m := _COMMAND.match(line))]
        imports, namespace, blocks, mutual, counts = [], [], [], None, {}
        exit_line = next((n for n, m in commands if m["kind"] == "#exit"), None)
        for index, (line, match) in enumerate(commands):
            kind, rest = match["kind"], match["rest"].strip()
            if kind == "import":
                if exit_line is None or line < exit_line: imports.extend(re.findall(_IDENT, rest))
                continue
            if kind in {"namespace", "section", "mutual"}:
                name = re.match(_IDENT, rest)
                blocks.append((kind, len(namespace), mutual))
                if kind == "namespace" and name: namespace.append(name[0])
                if kind == "mutual": mutual = "mutual-" + digest([path, line])[:16]
                continue
            if kind == "end":
                if blocks:
                    _, length, mutual = blocks.pop()
                    namespace = namespace[:length]
                continue
            if kind not in _DECL: continue
            name_match = re.match(_IDENT, rest)
            name = name_match[0] if name_match else ""
            if kind in {"example", "initialize"} or (kind == "instance" and (not name or rest.startswith(("[", "(", ":")))):
                name = "<anonymous-" + kind + ">"
            if not name: name = "<unparsed-" + kind + ">"
            qualified = ".".join([*namespace, name])
            counts[qualified] = counts.get(qualified, 0) + 1
            key = "chunk-" + digest([path, qualified, counts[qualified]])[:20]
            end = commands[index + 1][0] - 1 if index + 1 < len(commands) else len(lines)
            inventory["chunks"][key] = {"chunk_id": key, "path": path, "name": qualified,
                "kind": kind, "start_line": line, "end_line": max(line, end), "group_id": mutual,
                "after_exit": exit_line is not None and line > exit_line,
                "source_sha256": hashlib.sha256("\n".join(source.splitlines()[line - 1:end]).encode()).hexdigest(),
                "status": "excluded" if exit_line is not None and line > exit_line else "pending"}
        inventory["files"][path] = {"sha256": hashlib.sha256(source.encode()).hexdigest(),
                                    "imports": sorted(set(imports)), "exit_line": exit_line}
    return inventory


def install_inventory(forum_dir: Path, inventory: dict) -> dict:
    with transaction(forum_dir) as state:
        if state["baseline_sealed"]:
            raise ValueError("initial inventory already sealed; refresh affected work instead")
        state["chunks"] = deepcopy(inventory["chunks"])
        state["files"] = deepcopy(inventory["files"])
        state["limitations"] = list(inventory.get("limitations", []))
    return load(forum_dir)


_INVENTORY_FIELDS = ("path", "name", "kind", "start_line", "end_line", "group_id",
                     "after_exit", "source_sha256")


def _inventory_row(chunk_id: str, chunk: dict) -> dict:
    return {"chunk_id": chunk_id, **{key: chunk.get(key) for key in _INVENTORY_FIELDS},
            "eligible": not chunk.get("after_exit") and chunk.get("status") != "excluded"}


def inventory_identity(state: dict) -> str:
    """Hash inventory content, not forum events, phase results or state revision."""
    return digest({"files": state.get("files", {}),
                   "chunks": [_inventory_row(cid, row) for cid, row in sorted(state["chunks"].items())]})


def inventory_page(state: dict, path: str = "", name: str = "", offset: int = 0,
                   limit: int = 50, inventory_sha256: str = "") -> dict:
    """Read canonical chunks; path and qualified declaration name are exact filters."""
    if path and (not isinstance(path, str) or relative_path(path) != path):
        raise ValueError("inventory path must be an exact project-relative file path")
    if not isinstance(name, str):
        raise ValueError("inventory name must be an exact qualified declaration name")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("inventory pages need nonnegative offset and limit from 1 to 100")
    identity = inventory_identity(state)
    if inventory_sha256 and inventory_sha256 != identity:
        raise PlanValidationError("stale_inventory", "stale inventory hash; retrieve the current inventory",
                                  inventory_sha256=identity, inventory_tool="optimize_inventory")
    rows = [_inventory_row(cid, chunk) for cid, chunk in state["chunks"].items()
            if (not path or chunk["path"] == path) and (not name or chunk["name"] == name)]
    rows.sort(key=lambda row: (row["path"], row["start_line"], row["name"], row["chunk_id"]))
    next_offset = offset + limit if offset + limit < len(rows) else None
    return {"inventory_sha256": identity, "total": len(state["chunks"]), "matched": len(rows),
            "offset": offset, "limit": limit, "next_offset": next_offset,
            "chunks": rows[offset:offset + limit]}


def _scope_targets(state: dict):
    scope = state.get("scope")
    return state["config"].get("targets", scope.get("targets", "Universe")
                               if isinstance(scope, dict) else scope or "Universe")


def _plan_exclusions(state: dict, exclusions: dict | None, paths: list[str] | None,
                     reason: str, inventory_sha256: str) -> dict:
    expanded = deepcopy(exclusions or {})
    if not isinstance(expanded, dict):
        raise PlanValidationError("invalid_exclusions", "exclusions must map canonical chunk IDs to reasons")
    identity = inventory_identity(state)
    if inventory_sha256 and inventory_sha256 != identity:
        raise PlanValidationError("stale_inventory", "stale inventory hash; retrieve the current inventory",
                                  inventory_sha256=identity, inventory_tool="optimize_inventory")
    if paths is None:
        return expanded
    if _scope_targets(state) == "Universe":
        raise PlanValidationError("universe_exclusion", "Universe scope cannot omit declaration chunks")
    if not isinstance(paths, list) or not paths or any(not isinstance(path, str) for path in paths):
        raise PlanValidationError("invalid_exclusion_selector", "exclusion selector needs nonempty exact file paths")
    known = state.get("files", {})
    for path in paths:
        try:
            valid = relative_path(path) == path and path in known
        except ValueError:
            valid = False
        if not valid:
            raise _plan_error("unknown_exclusion_path", "exclusion selector needs known exact project-relative file paths",
                              unknown_paths=[path])
    if len(set(paths)) != len(paths):
        raise PlanValidationError("duplicate_exclusion_path", "exclusion selector file paths must be distinct")
    if not isinstance(reason, str) or not reason.strip() or not inventory_sha256:
        raise PlanValidationError("incomplete_exclusion_selector", "exclusion selector needs a reason and current inventory hash")
    selected = set(paths)
    outside = {cid for cid, row in state["chunks"].items()
               if not row.get("after_exit") and row.get("status") != "excluded" and row["path"] not in selected}
    conflicts = {cid for cid in outside & expanded.keys() if expanded[cid] != reason.strip()}
    if conflicts:
        raise _plan_error("conflicting_exclusions", "selector and explicit exclusion reasons conflict",
                          conflicts=conflicts)
    expanded.update({cid: reason.strip() for cid in outside})
    return expanded


def _validated_plan(state: dict, rows: list[dict], exclusions: dict | None = None) -> dict:
    """Validate a complete proposed task DAG; cycles need explicit grouping."""
    tasks, covered = {}, set()
    eligible = {cid for cid, chunk in state["chunks"].items() if not chunk.get("after_exit")
                and chunk.get("status") != "excluded"}
    exclusions = exclusions or {}
    raw_targets = _scope_targets(state)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise PlanValidationError("invalid_plan_rows", "plan needs a list of declaration-task rows")
    if not isinstance(exclusions, dict) or any(not isinstance(cid, str) for cid in exclusions):
        raise PlanValidationError("invalid_exclusions", "exclusions must map canonical chunk IDs to reasons")
    if exclusions:
        if raw_targets == "Universe":
            raise PlanValidationError("universe_exclusion", "Universe scope cannot omit declaration chunks")
        if not set(exclusions) <= eligible or any(not isinstance(reason, str) or not reason.strip() for reason in exclusions.values()):
            raise _plan_error("invalid_exclusions", "each excluded chunk needs an explicit target-scope rationale",
                              unknown_chunks=set(exclusions) - eligible)
        eligible -= exclusions.keys()
    for row in rows:
        raw_members = row.get("chunk_ids", [row["chunk_id"]] if "chunk_id" in row else [])
        if not isinstance(raw_members, list) or any(not isinstance(cid, str) for cid in raw_members):
            raise PlanValidationError("invalid_chunk_ids", "plan chunk_ids must be a list of canonical chunk IDs")
        members = sorted(set(raw_members))
        if not members or not set(members) <= eligible:
            raise _plan_error("unknown_or_excluded_chunks", "plan needs known in-scope declaration chunks",
                              unknown_chunks=set(members) - state["chunks"].keys(),
                              excluded_chunks=set(members) & (state["chunks"].keys() - eligible))
        if covered & set(members):
            raise _plan_error("overlapping_tasks", "each declaration belongs to one scheduling task",
                              overlap=covered & set(members))
        covered.update(members)
        tid = row.get("task_id") or (members[0] if len(members) == 1 else "group-" + digest(members)[:20])
        if not isinstance(tid, str) or not tid or tid in tasks:
            raise PlanValidationError("invalid_task_id", "invalid or duplicate task id")
        reason = str(row.get("reason", "")).strip()
        if len(members) > 1 and not reason:
            raise _plan_error("missing_group_rationale", "grouping requires concrete evidence and rationale", chunks=members)
        raw_files, dependencies = row.get("files", []), row.get("dependencies", [])
        if not isinstance(raw_files, list) or any(not isinstance(path, str) for path in raw_files):
            raise PlanValidationError("invalid_task_files", "task files must be project-relative file paths")
        if not isinstance(dependencies, list) or any(not isinstance(dep, str) for dep in dependencies):
            raise PlanValidationError("invalid_dependency", "task dependencies must be canonical task or chunk IDs")
        try:
            files = sorted({state["chunks"][cid]["path"] for cid in members}
                           | {relative_path(path) for path in raw_files})
        except ValueError as exc:
            raise PlanValidationError("invalid_task_files", str(exc)) from exc
        tasks[tid] = {"task_id": tid, "chunk_ids": members, "files": files,
                      "dependencies": sorted(set(dependencies)), "reason": reason,
                      "status": "pending", "revision": 0, "assignments": []}
    if covered != eligible:
        raise _plan_error("incomplete_coverage", "plan must cover every in-scope declaration, including compiling declarations",
                          missing_chunks=eligible - covered)
    owner = {cid: tid for tid, row in tasks.items() for cid in row["chunk_ids"]}
    groups = {}
    for cid in eligible:
        group = state["chunks"][cid].get("group_id")
        if group: groups.setdefault(group, set()).add(owner[cid])
    if any(len(owners) > 1 for owners in groups.values()):
        raise _plan_error("split_mutual_group", "mutual declarations require an explicit shared task",
                          groups=[group for group, owners in groups.items() if len(owners) > 1])
    for tid, row in tasks.items():
        row["dependencies"] = sorted({owner.get(dep, dep) for dep in row["dependencies"]})
        if tid in row["dependencies"] or not set(row["dependencies"]) <= tasks.keys():
            raise _plan_error("invalid_dependency", "unknown or self task dependency",
                              invalid_dependencies=set(row["dependencies"]) - (tasks.keys() - {tid}))
    pending = {tid: set(row["dependencies"]) for tid, row in tasks.items()}
    while pending:
        ready = {tid for tid, dependencies in pending.items() if not dependencies}
        if not ready:
            raise _plan_error("cyclic_dependencies", "cyclic task graph; explicitly group the dependent declarations",
                              blocked_tasks=pending)
        pending = {tid: dependencies - ready for tid, dependencies in pending.items() if tid not in ready}
    return tasks


def propose_plan(forum_dir: Path, author: str, chunks: list[dict], reason: str = "",
                 exclusions: dict | None = None, *, exclude_outside_paths: list[str] | None = None,
                 exclusion_reason: str = "", inventory_sha256: str = "") -> dict:
    with transaction(forum_dir) as state:
        _agent(state, author)
        if state["phase"] != "chunking" or state["baseline_sealed"]:
            raise ValueError("initial chunk plans are only proposed during chunking")
        expanded = _plan_exclusions(state, exclusions, exclude_outside_paths, exclusion_reason, inventory_sha256)
        planned = _validated_plan(state, chunks, expanded)
        pid = "plan-" + str(len(state["plan_proposals"]) + 1)
        row = {"proposal_id": pid, "author": author, "tasks": planned,
               "reason": reason, "exclusions": expanded, "status": "proposed", "at": now()}
        if inventory_sha256:
            row["inventory_sha256"] = inventory_sha256
        if exclude_outside_paths is not None:
            row["exclusion_selector"] = {"exclude_outside_paths": list(exclude_outside_paths),
                                         "exclusion_reason": exclusion_reason.strip(),
                                         "inventory_sha256": inventory_sha256}
        state["plan_proposals"][pid] = row
        _event(state, "plan_proposed", author, proposal_id=pid)
    return deepcopy(row)


def accept_plan(forum_dir: Path, proposal_id: str) -> dict:
    """Controller action after chunking; no extra primary-only planning gate."""
    with transaction(forum_dir) as state:
        if state["baseline_sealed"] or state["assignments"]:
            raise ValueError("initial plan cannot overwrite active or sealed work")
        row = state["plan_proposals"][proposal_id]
        identity = inventory_identity(state)
        if row.get("inventory_sha256", identity) != identity:
            raise PlanValidationError("stale_inventory", "stale inventory hash; retrieve the current inventory",
                                      inventory_sha256=identity, inventory_tool="optimize_inventory")
        state["tasks"] = _validated_plan(state, list(row["tasks"].values()), row.get("exclusions"))
        state["scope_decisions"] = deepcopy(row.get("exclusions", {}))
        row["status"] = "accepted"
        _event(state, "plan_accepted", "controller", proposal_id=proposal_id)
    return load(forum_dir)


def ready_tasks(state: dict) -> list[dict]:
    return [deepcopy(row) for row in state["tasks"].values() if row["status"] in {"pending", "running"}
            and all(state["tasks"][dep]["status"] in {"complete", "no_improvement"}
                    for dep in row["dependencies"])]


def begin_round(forum_dir: Path, *, max_rounds: float) -> dict:
    with transaction(forum_dir) as state:
        if not state["baseline_sealed"]:
            raise ValueError("baseline must be sealed before optimization")
        rounds = state["rounds"]
        if rounds["active"] is None:
            if rounds["started"] >= max_rounds:
                raise ValueError("optimization/review round budget exhausted")
            rounds["started"] += 1
            rounds["active"] = {"number": rounds["started"], "started_at": now(), "dispatched": []}
            state["phase"] = "optimizing"
        result = deepcopy(rounds["active"])
    return result


def record_dispatch(forum_dir: Path, author: str, task_id: str) -> dict:
    with transaction(forum_dir) as state:
        _agent(state, author)
        active = state["rounds"]["active"]
        if active is None or task_id not in state["tasks"]:
            raise ValueError("dispatch requires an active round and known task")
        revision = state["tasks"][task_id]["revision"]
        if any(row["author"] == author and row["task_id"] == task_id and row.get("task_revision", 0) == revision
               and row.get("base_main_sha", state["main_sha"]) == state["main_sha"]
               for row in active["dispatched"]):
            raise ValueError("agent/task already dispatched this round at this revision and integration base")
        row = {"author": author, "task_id": task_id, "task_revision": revision,
               "base_main_sha": state["main_sha"], "at": now()}
        active["dispatched"].append(row)
    return row


def finish_round(forum_dir: Path, *, outcome: str) -> dict:
    with transaction(forum_dir) as state:
        active = state["rounds"]["active"]
        if active is None: raise ValueError("no active round")
        if any(row["status"] == "running" for row in state["assignments"].values()):
            raise ValueError("cannot finish a round while workers are running")
        active.update(outcome=outcome, finished_at=now())
        state["rounds"]["history"].append(deepcopy(active))
        state["rounds"]["completed"] += 1
        state["rounds"]["active"] = None
        result = deepcopy(active)
    return result


def register_strategy(forum_dir: Path, author: str, task_id: str, title: str, description: str) -> dict:
    if not title.strip() or not description.strip():
        raise ValueError("strategy needs a title and concrete approach")
    with transaction(forum_dir) as state:
        _agent(state, author)
        if task_id not in state["tasks"]: raise ValueError("unknown task")
        normalized = " ".join(description.split()).casefold()
        if any(row["task_id"] == task_id and " ".join(row["description"].split()).casefold() == normalized
               and row["status"] in {"open", "claimed", "running"} for row in state["strategies"].values()):
            raise ValueError("this approach already exists; assist or choose a distinct strategy")
        sid = "strategy-" + str(len(state["strategies"]) + 1)
        row = {"strategy_id": sid, "task_id": task_id, "author": author, "owner": None,
               "title": title, "description": description, "assistants": [], "status": "open"}
        state["strategies"][sid] = row
        _event(state, "strategy_registered", author, strategy_id=sid, task_id=task_id)
    return deepcopy(row)


def claim_strategy(forum_dir: Path, author: str, strategy_id: str) -> dict:
    with transaction(forum_dir) as state:
        _agent(state, author)
        row = state["strategies"][strategy_id]
        if row["owner"] not in {None, author}:
            raise ValueError("strategy is claimed; assist or choose another approach")
        if row["status"] not in {"open", "claimed"}:
            raise ValueError("strategy is no longer claimable")
        task = state["tasks"][row["task_id"]]
        if task["task_id"] not in {t["task_id"] for t in ready_tasks(state)}:
            raise ValueError("task has unfinished dependencies or is not active")
        row.update(owner=author, status="claimed")
    return deepcopy(row)


def configure_assignment(forum_dir: Path, author: str, strategy_id: str, worktree: Path,
                         base_main_sha: str) -> dict:
    """Bind controller-provided private tree; no cross-strategy file exclusion."""
    root = Path(worktree).resolve()
    with transaction(forum_dir) as state:
        row = state["strategies"][strategy_id]
        task = state["tasks"][row["task_id"]]
        if row["owner"] != author or row["status"] != "claimed":
            raise ValueError("strategy not claimed by this worker")
        if root == Path(state["root"]).resolve() or not root.is_dir():
            raise ValueError("optimization requires an existing private worktree")
        if state["main_sha"] != base_main_sha:
            raise ValueError("assignment base is stale")
        if any(a["status"] == "running" and (a["author"] == author or Path(a["worktree"]).resolve() == root)
               for a in state["assignments"].values()):
            raise ValueError("agent or private worktree already has an active assignment")
        aid = "assignment-" + str(len(state["assignments"]) + 1)
        assignment = {"assignment_id": aid, "author": author, "task_id": task["task_id"],
                      "strategy_id": strategy_id, "worktree": str(root), "base_main_sha": base_main_sha,
                      "task_revision": task["revision"], "config_revision": state["config"]["revision"],
                      "round": (state["rounds"]["active"] or {}).get("number"), "status": "running", "at": now()}
        state["assignments"][aid] = assignment
        task["assignments"].append(aid)
        task["status"] = "running"
        row.update(status="running", assignment_id=aid)
    return deepcopy(assignment)


assign_strategy = configure_assignment


def _owned(state: dict, author: str, assignment_id: str) -> dict:
    row = state["assignments"][assignment_id]
    if row["author"] != author or row["status"] != "running":
        raise ValueError("no live assignment owned by this worker")
    if row["task_revision"] != state["tasks"][row["task_id"]]["revision"]:
        raise ValueError("assignment task revision is stale")
    if row["config_revision"] != state["config"]["revision"]:
        raise ValueError("assignment configuration is stale")
    return row


def submit_candidate(forum_dir: Path, author: str, assignment_id: str, notes: str,
                     measurements: dict | None = None, **git_evidence) -> dict:
    with transaction(forum_dir) as state:
        row = _owned(state, author, assignment_id)
        if not notes.strip(): raise ValueError("candidate needs explanation and correctness evidence")
        task = state["tasks"][row["task_id"]]
        if not set(measurements or {}) <= set(task["chunk_ids"]):
            raise ValueError("candidate measurements name unassigned chunks")
        cid = "candidate-" + str(len(state["candidates"]) + 1)
        candidate = {"candidate_id": cid, "author": author, "assignment_id": assignment_id,
                     "strategy_id": row["strategy_id"], "task_id": row["task_id"], "status": "submitted",
                     "base_main_sha": row["base_main_sha"], "task_revision": row["task_revision"],
                     "config_revision": row["config_revision"], "worktree": row["worktree"],
                     "notes": notes, "measurements": deepcopy(measurements or {}), "at": now(),
                     "endorsements": [], "objections": []}
        allowed_evidence = {"commit_sha", "files", "diff_sha256", "artifact", "judgments"}
        if not set(git_evidence) <= allowed_evidence: raise ValueError("unexpected Git evidence fields")
        if not set(git_evidence.get("judgments", {})) <= set(task["chunk_ids"]):
            raise ValueError("candidate judgments name unassigned chunks")
        candidate.update(deepcopy(git_evidence))
        state["candidates"][cid] = candidate
        row.update(status="submitted", candidate_id=cid)
        state["strategies"][row["strategy_id"]]["status"] = "submitted"
        _event(state, "candidate_submitted", author, candidate_id=cid, task_id=row["task_id"])
    return deepcopy(candidate)


def candidate_opinion(forum_dir: Path, author: str, candidate_id: str, *, endorse: bool,
                      reason: str, evidence: str = "") -> dict:
    if not reason.strip(): raise ValueError("candidate opinion needs a reason")
    with transaction(forum_dir) as state:
        _agent(state, author)
        candidate = state["candidates"][candidate_id]
        row = {"author": author, "reason": reason, "evidence": evidence, "at": now()}
        candidate["endorsements" if endorse else "objections"].append(row)
        _event(state, "candidate_endorsed" if endorse else "candidate_objected", author,
               candidate_id=candidate_id, reason=reason)
    return row


def select_candidate(forum_dir: Path, author: str, candidate_id: str, reason: str) -> dict:
    if not reason.strip(): raise ValueError("primary selection needs a rationale")
    with transaction(forum_dir) as state:
        _primary(state, author)
        candidate = state["candidates"][candidate_id]
        if candidate["status"] not in {"checked", "selected"} or candidate.get("verification", {}).get("passed") is not True:
            raise ValueError("selection requires a controller-checked candidate")
        if candidate["base_main_sha"] != state["main_sha"] or candidate["config_revision"] != state["config"]["revision"]:
            raise ValueError("candidate evidence is stale")
        task = state["tasks"][candidate["task_id"]]
        if candidate["task_revision"] != task["revision"]:
            raise ValueError("candidate task revision is stale")
        for other in state["candidates"].values():
            if other["task_id"] == candidate["task_id"] and other["status"] == "selected": other["status"] = "checked"
        candidate.update(status="selected", selection={"author": author, "reason": reason, "at": now()})
        _event(state, "candidate_selected", author, candidate_id=candidate_id)
    return deepcopy(candidate)


record_selection = select_candidate


def record_no_improvement(forum_dir: Path, author: str, assignment_id: str, reason: str,
                          evidence: str) -> dict:
    if not reason.strip() or not evidence.strip():
        raise ValueError("no-improvement outcome needs attempted approach and evidence")
    with transaction(forum_dir) as state:
        row = _owned(state, author, assignment_id)
        row.update(status="no_improvement", outcome={"reason": reason, "evidence": evidence, "at": now()})
        state["strategies"][row["strategy_id"]]["status"] = "no_improvement"
        _event(state, "no_improvement", author, assignment_id=assignment_id, reason=reason, evidence=evidence)
    return deepcopy(row)


def request_scope(forum_dir: Path, author: str, task_id: str, chunk_ids: list[str], files: list[str],
                  reason: str, evidence: str = "") -> dict:
    if not reason.strip() or not evidence.strip():
        raise ValueError("scope/group adaptation needs concrete reason and evidence")
    with transaction(forum_dir) as state:
        _agent(state, author)
        if task_id not in state["tasks"] or not set(chunk_ids) <= state["chunks"].keys():
            raise ValueError("unknown task or chunk")
        checked = sorted({relative_path(path) for path in files})
        rid = "scope-" + str(len(state["scope_requests"]) + 1)
        row = {"request_id": rid, "author": author, "task_id": task_id, "chunk_ids": sorted(set(chunk_ids)),
               "files": checked, "reason": reason, "evidence": evidence, "status": "pending", "at": now()}
        state["scope_requests"][rid] = row
        _event(state, "scope_requested", author, request_id=rid, task_id=task_id)
    return deepcopy(row)


def refresh_affected(forum_dir: Path, changed_files: list[str], *, main_sha: str,
                     inventory: dict | None = None, measurements: dict | None = None,
                     integrated_task_id: str | None = None, integrated_candidate_id: str | None = None,
                     verification: dict | None = None) -> dict:
    """Invalidate dependent work and old-source evidence, never reset budgets."""
    changed = {relative_path(path) for path in changed_files}
    with transaction(forum_dir) as state:
        integrated = state["candidates"].get(integrated_candidate_id) if integrated_candidate_id else None
        if integrated_candidate_id and not integrated:
            raise ValueError("unknown integrated candidate")
        if integrated and integrated.get("status") == "merged":
            if integrated.get("integrated_sha") != main_sha or state["main_sha"] != main_sha:
                raise ValueError("integrated candidate checkpoint conflicts with journal")
            return deepcopy(state)
        if integrated and integrated["task_id"] != integrated_task_id:
            raise ValueError("integrated candidate task does not match journal")
        affected = {tid for tid, task in state["tasks"].items() if task["status"] != "grouped" and changed & set(task["files"])}
        while True:
            expanded = affected | {tid for tid, task in state["tasks"].items() if task["status"] != "grouped" and affected & set(task["dependencies"])}
            if expanded == affected: break
            affected = expanded
        state["main_sha"] = main_sha
        state["review"] = {}
        for tid in affected:
            task = state["tasks"][tid]
            task["revision"] += 1
            task["status"] = "complete" if tid == integrated_task_id else "pending"
        for candidate in state["candidates"].values():
            if candidate["status"] in {"submitted", "checked", "selected"} and candidate["base_main_sha"] != main_sha:
                candidate["status"] = "stale"
        for assignment in state["assignments"].values():
            if assignment["status"] == "running" and assignment["task_id"] in affected:
                assignment["refresh_required"] = True
        if inventory is not None:
            inventory = deepcopy(inventory)
            missing = set(state["chunks"]) - inventory["chunks"].keys()
            for cid in missing:
                retained = deepcopy(state["chunks"][cid])
                retained["missing_from_current_inventory"] = True
                inventory["chunks"][cid] = retained
            for task in state["tasks"].values():
                if missing & set(task["chunk_ids"]):
                    task["needs_regroup"] = True
                    if task["task_id"] != integrated_task_id: task["status"] = "pending"
            state["chunks"] = deepcopy(inventory["chunks"])
            state["files"] = deepcopy(inventory["files"])
        if measurements is not None: state["measurements"].update(deepcopy(measurements))
        if integrated:
            integrated.update(status="merged", integrated_sha=main_sha,
                              integrated_verification=deepcopy(verification or {}))
        _event(state, "affected_refreshed", "controller", tasks=sorted(affected), main_sha=main_sha)
    return load(forum_dir)


def apply_scope_request(forum_dir: Path, request_id: str) -> dict:
    """Apply evidenced in-scope grouping after affected workers have closed.

    Retain prior task/candidate records and redirect dependency edges.  This is
    not permission to expand the user's selected declaration/file scope.
    """
    with transaction(forum_dir) as state:
        request = state["scope_requests"][request_id]
        if request["status"] == "applied": return deepcopy(request)
        if request["status"] != "pending": raise ValueError("scope request is no longer pending")
        anchor_id = request["task_id"]
        anchor = state["tasks"][anchor_id]
        if anchor["status"] == "grouped": raise ValueError("scope request refers to a superseded task")
        active = {tid: row for tid, row in state["tasks"].items() if row["status"] != "grouped"}
        owners = {cid: tid for tid, row in active.items() for cid in row["chunk_ids"]}
        if not set(request["chunk_ids"]) <= owners.keys():
            raise ValueError("scope request would widen the selected declaration scope")
        affected = {anchor_id} | {owners[cid] for cid in request["chunk_ids"]}
        allowed_files = {path for row in active.values() for path in row["files"]}
        if not set(request["files"]) <= allowed_files:
            raise ValueError("scope request would widen the selected file scope")
        if any(row["status"] == "running" and row["task_id"] in affected for row in state["assignments"].values()):
            raise ValueError("close affected workers before applying a scope/group request")
        members = sorted({cid for tid in affected for cid in active[tid]["chunk_ids"]})
        files = sorted({path for tid in affected for path in active[tid]["files"]} | set(request["files"]))
        dependencies = sorted({dep for tid in affected for dep in active[tid]["dependencies"]} - affected)
        proposed = deepcopy(active)
        proposed[anchor_id].update(chunk_ids=members, files=files, dependencies=dependencies)
        for tid in affected - {anchor_id}: del proposed[tid]
        for row in proposed.values():
            row["dependencies"] = sorted({anchor_id if dep in affected else dep for dep in row["dependencies"]}
                                         - {row["task_id"]})
        pending = {tid: set(row["dependencies"]) for tid, row in proposed.items()}
        while pending:
            ready = {tid for tid, dependencies in pending.items() if not dependencies}
            if not ready: raise ValueError("scope/group request creates a cyclic task graph; include the cycle explicitly")
            pending = {tid: dependencies - ready for tid, dependencies in pending.items() if tid not in ready}
        for tid, row in proposed.items():
            if row["dependencies"] != active[tid]["dependencies"]:
                state["tasks"][tid]["revision"] += 1
                state["tasks"][tid]["status"] = "pending"
            state["tasks"][tid]["dependencies"] = row["dependencies"]
        anchor.update(chunk_ids=members, files=files, dependencies=proposed[anchor_id]["dependencies"],
                      status="pending", revision=anchor["revision"] + 1,
                      reason=request["reason"], grouping_evidence=request["evidence"])
        for tid in affected - {anchor_id}:
            state["tasks"][tid].update(status="grouped", grouped_into=anchor_id,
                                       revision=state["tasks"][tid]["revision"] + 1)
        for candidate in state["candidates"].values():
            if candidate["task_id"] in affected and candidate["status"] in {"submitted", "checked", "selected"}:
                candidate["status"] = "stale"
        state["review"] = {}
        request.update(status="applied", applied_at=now(), grouped_task_ids=sorted(affected), result_task_id=anchor_id)
        _event(state, "scope_applied", "controller", request_id=request_id, task_id=anchor_id)
        return deepcopy(request)


def record_workflow_change(forum_dir: Path, author: str, *, original: str, problem: str,
                           evidence: str, revised: str, result: str) -> dict:
    if not all(value.strip() for value in (original, problem, evidence, revised, result)):
        raise ValueError("workflow change needs original procedure, problem, evidence, revision and result")
    with transaction(forum_dir) as state:
        _agent(state, author)
        row = {"author": author, "original": original, "problem": problem, "evidence": evidence,
               "revised": revised, "result": result, "at": now()}
        state["workflow_changes"].append(row)
    return row
