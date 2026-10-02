"""Event-driven formalization workers for ``unity bump``.

The command owns phase order. This runtime launches cancellable workers, consumes
authoritative Forum events, and returns when formalization is quiescent or advances.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
from threading import Event

from rich.console import Console

from . import artifacts, library, bump_contract, bump_jobs, bump_state
from . import bump_worktree as worktree
from . import bump_files, bump_representation
from .bump_input import require_source_matches
from .forum import bump_server
from .bump_orchestrator import _preamble, load_prompt, stop_requested
from .bump_spawn import spawn


_console = Console()
PIPELINE = "bump"


def _scheduler_projection(state: dict) -> dict:
    """Detach routing data without copying native baselines or granting authority.

    This is deliberately not a valid migration state/contract. Eligibility,
    dispatch, merge, and terminal decisions still read the complete state.
    The two omitted payloads are not used by the loop's routing observations.
    """
    contract = state.get("formalization", {}).get("contract") or {}
    if contract.get("migration_policy") == 1 and contract.get("representation_review_policy") == 1:
        # Existing representation helpers resolve typed names through this
        # baseline. Fixed migration contracts do not enable that policy, but
        # preserve the original behavior if an explicit policy is encountered.
        return deepcopy(state)
    value = {key: item for key, item in state.items() if key != "project_baseline"}
    formal = dict(value.get("formalization") or {})
    contract = formal.get("contract")
    if isinstance(contract, dict):
        formal["contract"] = {key: item for key, item in contract.items()
                              if key != "project_baseline"}
    value["formalization"] = formal
    return deepcopy(value)


class _SchedulerObservations:
    """Coalesce unchanged routing reads within ONE event-loop iteration only.

    File metadata is an observation hint, never a receipt or proof cache. Every
    public result is detached; the private projection cannot become a mutable
    state alias. A new instance is required after each scheduling sleep. Normal
    atomic publication, same-size replacements, and in-place changes invalidate
    it. Full authority boundaries use ``fresh`` or the ordinary state loader.
    """

    def __init__(self, forum: Path):
        self.forum = Path(forum)
        self.path = bump_state.state_path(self.forum)
        self._identity = None
        self._projection = None

    def _file_identity(self):
        try:
            value = self.path.stat()
        except FileNotFoundError:
            return None
        return (value.st_dev, value.st_ino, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns)

    def fresh(self) -> dict:
        """Load full state anew; no cached snapshot authorizes a boundary."""
        self._identity, self._projection = None, None
        for _ in range(3):
            before = self._file_identity()
            state = bump_state.load_state(self.forum)
            projection = _scheduler_projection(state)
            after = self._file_identity()
            if before == after:
                # Missing state retains the ordinary loader's behavior, but is
                # never a cache hit: creation must be observed immediately.
                if after is not None:
                    self._identity, self._projection = after, projection
                return state
        raise ValueError("Bump state changed repeatedly during a scheduler read; no stale routing was used")

    def observe(self) -> dict:
        """Return detached coordination data, never the cached object itself."""
        before = self._file_identity()
        if self._projection is not None and before == self._identity:
            result = deepcopy(self._projection)
            if self._file_identity() == before:
                return result
        return _scheduler_projection(self.fresh())


def _observe_scheduler_sources(paths, observations: _SchedulerObservations) -> dict:
    """Read-only top-of-pass work; the caller awaits completion before routing."""
    state = observations.observe()
    require_source_matches(paths, state)
    return state


def _reconcile_for_scheduler(forum: Path) -> dict:
    """Avoid an inapplicable migration transaction, never an applicable repair.

    Fixed migration contracts do not install representation-review policy.
    ``representation_review_input`` consequently returns None for every task,
    so reconciliation would only parse/hash the full unchanged state twice.
    Read full state afresh, and leave every other policy on the original path.
    """
    state = bump_state.load_state(forum)
    contract = state.get("formalization", {}).get("contract") or {}
    if contract.get("migration_policy") in {1, 2} and "representation_review_policy" not in contract:
        return state
    return bump_state.reconcile_rejected_representations(forum)


def _migration_frontier_is_current(state: dict) -> bool:
    """A failed diagnostic-publication CAS is not a checked frontier."""
    formal = state.get("formalization", {})
    if (formal.get("contract") or {}).get("migration_policy") != 2:
        return True
    return bool(state.get("migration_plan")
                and state.get("migration_plan_main_sha") == formal.get("main_sha")
                and not state.get("migration_refresh_required")
                and not any(row.get("status") == "proposed" for row in
                            state.get("migration_mapping_proposals", {}).values()))


def configure_forum(paths, profile: str) -> None:
    bump_server.configure(paths.forum, paths.project_root, profile)


def forum_brief(paths, profile: str, author: str, task_id: str = "") -> str:
    if os.getenv("UNITY_FORUM_BRIEF", "on").lower() == "off":
        return ""
    configure_forum(paths, profile)
    try:
        return bump_server.bump_brief(author, task_id=task_id)
    except Exception:
        return ""


_CANCEL_GRACE_SECONDS = 20.0
_CANCEL_HARD_SECONDS = 10.0


async def _cancel(
    agent,
    task: asyncio.Task,
    interrupt: asyncio.Event,
    reason: str,
    project_root: Path | None = None,
) -> None:
    if task.done():
        return
    _console.print(f"[yellow]interrupting {agent.name}: {reason}[/yellow]")
    try:
        if agent.backend == "codex":
            interrupt.set()
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=_CANCEL_GRACE_SECONDS,
                )
                return
            except asyncio.TimeoutError:
                pass
        task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=_CANCEL_HARD_SECONDS,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    finally:
        if project_root is not None:
            await asyncio.to_thread(
                bump_jobs.terminate, project_root, owner=agent.name,
            )


def _agent_runtime_env(
    paths, state: dict, agent_name: str, *, task_id: str = "",
) -> dict[str, str]:
    """Give formalization workers isolated, disposable temporary space."""
    run_id = re.sub(r"[^a-zA-Z0-9_-]", "_", str(state.get("run_id") or "unknown-run"))
    safe_agent = re.sub(r"[^a-zA-Z0-9_-]", "_", agent_name)
    scratch = paths.unity / "tmp" / run_id / safe_agent
    scratch.mkdir(parents=True, exist_ok=True)
    # uvx also creates transient tool environments outside its package cache.
    # Keep those writes in the already-permitted worker scratch, not the global
    # uv tools directory (which may contain Unity itself and is read-only here).
    uv_tools = scratch / "uv-tools"
    uv_bin = scratch / "uv-bin"
    lake_cache = scratch / "lake-cache"
    uv_tools.mkdir(exist_ok=True)
    uv_bin.mkdir(exist_ok=True)
    lake_cache.mkdir(exist_ok=True)
    value = str(scratch.resolve())
    # Preserve the active installation in every role, including chunking. Do
    # not resolve the Python executable itself: a venv Python may be a symlink
    # outside the venv while its sibling Unity console script is the right one.
    runtime_bin = Path(sys.executable).absolute().parent.resolve()
    result = {
        "TMPDIR": value,
        "TMP": value,
        "TEMP": value,
        "UV_TOOL_DIR": str(uv_tools.resolve()),
        "UV_TOOL_BIN_DIR": str(uv_bin.resolve()),
        # Lake 4.34 otherwise writes artifacts under the read-only elan
        # toolchain. Relocate its cache without changing artifact/build policy.
        "LAKE_CACHE_DIR": str(lake_cache.resolve()),
        "PATH": str(runtime_bin) + os.pathsep + os.environ.get("PATH", ""),
        "PIP_REQUIRE_VIRTUALENV": "true",
        "PIP_DISABLE_PIP_VERSION_CHECK": "true",
        "UNITY_BUMP_PROJECT_ROOT": str(paths.project_root.resolve()),
        "UNITY_BUMP_TASK_ID": task_id,
    }
    if phase := state.get("phase"):
        result["UNITY_BUMP_PROFILE"] = str(phase)
    toolchain = paths.project_root / "lean-toolchain"
    if toolchain.is_file():
        result["ELAN_TOOLCHAIN"] = toolchain.read_text().strip()
    if state.get("phase") == "formalizing":
        real_lake = shutil.which("lake")
        if real_lake:
            bin_dir = paths.unity / "bin" / "bump"
            bin_dir.mkdir(parents=True, exist_ok=True)
            wrapper = bin_dir / "lake"
            wrapper_source = (
                f"#!{sys.executable}\n"
                "from unity.bump_lake_guard import main\n"
                "raise SystemExit(main())\n"
            )
            if not wrapper.exists() or wrapper.read_text() != wrapper_source:
                temporary = wrapper.with_name(f".{wrapper.name}.{os.getpid()}.tmp")
                temporary.write_text(wrapper_source)
                temporary.chmod(0o700)
                os.replace(temporary, wrapper)
            result.update({
                "PATH": str(bin_dir.resolve()) + os.pathsep + result["PATH"],
                "UNITY_REAL_LAKE": str(Path(real_lake).resolve()),
                "UNITY_BUMP_PROJECT_ROOT": str(paths.project_root.resolve()),
                "UNITY_BUMP_TASK_ID": task_id,
            })
    return result


def _formal_task_assignments(
    ready: list[dict],
    idle_workers: list[str],
    active_targets: list[str],
    *,
    available_to=None,
) -> list[tuple[str, str]]:
    """Cover independent ready tasks before assigning redundant bumprs."""
    if not ready:
        return []
    load = {formal_task["task_id"]: 0 for formal_task in ready}
    for target in active_targets:
        if target in load:
            load[target] += 1
    order = {
        formal_task["task_id"]: index
        for index, formal_task in enumerate(ready)
    }
    assignments = []
    for name in idle_workers:
        eligible = [target for target in load if available_to is None or available_to(name, target)]
        if not eligible:
            continue
        task_id = min(eligible, key=lambda target: (load[target], order[target]))
        assignments.append((name, task_id))
        load[task_id] += 1
    return assignments


def write_formalization_plan(paths, candidate: dict) -> Path:
    """Mechanically scaffold the source identities the semantic DAG must cover."""
    source_refs = candidate["source_refs"]
    plan = {
        "solution_candidate": candidate["candidate_id"],
        "solution_sha256": candidate["sha256"],
        "source_refs": source_refs,
    }
    state = bump_state.load_state(paths.forum)
    baseline = ((state["formalization"].get("contract") or {}).get("project_baseline")
                or state.get("project_baseline"))
    if baseline:
        # Full expression/proof-body evidence remains in controller state. The
        # chunker needs the target signatures, not every existing proof AST.
        available = set(baseline["scope"]["existing_targets"])
        if not baseline["scope"].get("bound"):
            available = set(baseline["project_axioms"]) | set(baseline["project_sorries"])
        if "verification_scope" in baseline:
            editable = set(baseline["verification_scope"]["editable_modules"].values())
            available = {name for name in available
                         if baseline["declarations"][name]["module"] in editable}
        plan["project_baseline"] = {
            key: deepcopy(baseline[key]) for key in (
                "sha256", "version", "policy", "project_scope", "branch", "head",
                "target_scope", "scope", "verification_scope") if key in baseline}
        plan["project_baseline"]["existing_incomplete_declarations"] = {
            name: {key: deepcopy(baseline["declarations"][name].get(key))
                   for key in ("module", "target_kind", "type", "level_params")}
            for name in sorted(available)}
        plan["project_baseline"]["protected_modules"] = sorted(set(baseline["layout"]["modules"].values()))
        if baseline.get("policy") == "changes-v1":
            plan["project_baseline"]["preservation_policy"] = (
                "Original files/configuration/dependencies are frozen. No whole-project native inventory "
                "was taken: an empty existing_incomplete_declarations map is not evidence of no holes. "
                "Read relevant source files, propose exact existing targets and output modules, and let "
                "the controller inspect original target contexts on demand. Unrelated old holes and "
                "optional sibling libraries are not task obligations. New results must not rely on "
                "unproved assumptions. Preserve existing commands outside authorized edits."
            )
    replan = state.get("replan") or {}
    if replan:
        previous = replan.get("previous_formalization") or {}
        plan["replan"] = {
            "request": replan.get("request"), "previous_tasks": replan.get("previous_tasks", {}),
            "previous_formalization": {"spec": previous.get("spec"),
                                       "requirements": previous.get("requirements", [])},
        }
    plan["source_issues"] = {
        key: {field: row.get(field) for field in (
            "issue_id", "anchor_ids", "task_ids", "description", "status", "repair_ids",
            "source_candidate", "source_sha256", "reason",
        )}
        for key, row in state.get("source_issues", {}).items()
    }
    plan["source_repairs"] = state.get("source_repairs", {})
    path = paths.forum / "formalization-plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    return path


def validate_formalization_dag(paths, expected_solution_sha: str) -> dict:
    """Validate the semantic chunker's DAG and its binding to supplied source bytes."""
    dag_path = paths.forum / "dag.json"
    try:
        dag = json.loads(dag_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("chunking did not produce a readable Bump dag.json") from exc
    return normalize_chunking_dag(dag, plan=_read_chunking_plan(paths),
                                  expected_solution_sha=expected_solution_sha)


def _read_chunking_plan(paths) -> dict:
    try:
        return json.loads((paths.forum / "formalization-plan.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("chunking requires a readable formalization-plan.json") from exc


def normalize_chunking_dag(dag: dict, *, plan: dict, expected_solution_sha: str) -> dict:
    """Normalize an in-memory snapshot; never reread an agent's editable draft."""
    dag = deepcopy(dag)
    if not isinstance(dag, dict):
        raise ValueError("formalization DAG must be a JSON object")
    recorded = str(dag.get("solution_sha256") or dag.get("source_sha256") or "")
    if recorded != expected_solution_sha:
        raise ValueError("formalization DAG is not bound to the formalization input SHA-256")
    chunks = dag.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("formalization DAG requires a nonempty chunks list")
    if any(not isinstance(chunk, dict) for chunk in chunks):
        raise ValueError("each formalization chunk must be a JSON object")
    ids = [str(item.get("id") or "").strip() for item in chunks]
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("formalization chunks require unique nonempty ids")
    if not isinstance(plan, dict):
        raise ValueError("formalization plan must be a JSON object")
    required_refs: set[str] = set()
    if plan is not None:
        if plan.get("solution_sha256") != expected_solution_sha:
            raise ValueError("formalization plan is not bound to the formalization input SHA-256")
        if dag.get("solution_candidate") != plan.get("solution_candidate"):
            raise ValueError("formalization DAG names the wrong supplied-source snapshot")
        required_refs = {str(item.get("ref_id")) for item in plan.get("source_refs", [])}
        if not required_refs or "None" in required_refs:
            raise ValueError("formalization plan has invalid source references")
    from .bump_spec import normalize_requirements, normalize_spec, normalize_informal_nodes
    source = {"candidate_id": plan["solution_candidate"], "sha256": plan["solution_sha256"],
              "source_refs": plan["source_refs"]}
    dag["requirements"] = normalize_requirements(dag.get("requirements"), chunks, required_refs)
    dag["spec"] = normalize_spec(dag.get("spec"), source=source,
                                 requirements=dag["requirements"], tasks=chunks,
                                 allow_unresolved=True)
    nodes = normalize_informal_nodes(chunks, dag["requirements"], dag["spec"], source)
    # Informal nodes intentionally discard implementation manifests. Retain
    # only original locations needed for the *initial* existing-target binding;
    # these do not populate trusted contract bindings or machine-owned outputs.
    from .bump_spec import _refs, PlanValidationError
    selected = set(_refs(dag.get("existing_targets", []), "existing_targets", nonempty=False))
    locations = {}
    for chunk in chunks:
        rows = chunk.get("outputs", [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise PlanValidationError("outputs", "original target locations must be a list of objects")
        retained = []
        for row in [chunk, *rows]:
            name = row.get("declaration", row.get("lean_decl"))
            if not isinstance(name, str) or name not in selected:
                continue
            filename = row.get("lean_file", row.get("file"))
            if not isinstance(filename, str) or not filename:
                raise PlanValidationError("outputs", "selected existing targets require their original lean_file")
            if name in locations and locations[name] != filename:
                raise PlanValidationError("outputs", "conflicting original locations for " + name)
            locations[name] = filename
            entry = {"declaration": name, "lean_file": filename}
            if entry not in retained:
                retained.append(entry)
        if retained:
            nodes[chunk["id"].strip()]["outputs"] = retained
    dag["chunks"] = list(nodes.values())
    dag["solution_sha256"] = expected_solution_sha
    return dag


def seed_chunking_draft(state: dict) -> dict | None:
    """Replans contain only mutable fields; source obligations stay controller-owned."""
    previous = state["formalization"]
    if not previous.get("source_obligations"):
        return None
    source = bump_state.formal_source(state)
    return {"solution_candidate": source["candidate_id"], "solution_sha256": source["sha256"],
            "base_revision": previous["revision"],
            "requirement_tasks": {row["id"]: deepcopy(row["tasks"]) for row in previous["requirements"]},
            "prerequisites": deepcopy(previous["spec"]["prerequisites"]),
            "arguments": deepcopy(previous["spec"]["arguments"]),
            "chunks": [{key: deepcopy(value) for key, value in row.items() if key in {
                "id", "title", "predicted_kind", "informal_statement", "informal_proof",
                "statement_dependencies", "proof_dependencies", "proposed_formal_statement",
                "proposed_formal_strategy", "source_components", "anchor_ids", "requirement_ids",
            }} for row in state["formal_tasks"].values()]}


def assemble_chunking_draft(state: dict, draft: dict) -> dict:
    from .bump_spec import PlanValidationError, _object
    if not isinstance(draft, dict):
        raise PlanValidationError("draft", "must be a JSON object")
    previous = state["formalization"]
    frozen = previous.get("source_obligations")
    if not frozen:
        return deepcopy(draft)
    _object(draft, {"solution_candidate", "solution_sha256", "base_revision", "requirement_tasks",
                    "prerequisites", "arguments", "chunks"}, "replan")
    if type(draft["base_revision"]) is not int or draft["base_revision"] != previous["revision"]:
        raise PlanValidationError("base_revision", "draft targets an obsolete plan",
                                  code="stale_base", expected=previous["revision"], actual=draft["base_revision"])
    ids = {row["id"] for row in frozen["requirements"]}
    _object(draft["requirement_tasks"], ids, "requirement_tasks")
    return {"solution_candidate": draft["solution_candidate"], "solution_sha256": draft["solution_sha256"],
            "existing_targets": deepcopy((previous.get("contract") or {}).get("project_baseline", {})
                                         .get("scope", {}).get("existing_targets", [])),
            "requirements": [{**deepcopy(row), "tasks": deepcopy(draft["requirement_tasks"][row["id"]])}
                             for row in frozen["requirements"]],
            "spec": {"version": 1, "anchors": deepcopy(frozen["anchors"]), "scope": deepcopy(frozen["scope"]),
                     "prerequisites": deepcopy(draft["prerequisites"]), "arguments": deepcopy(draft["arguments"])},
            "chunks": deepcopy(draft["chunks"])}


MAX_CHUNKING_DRAFT_BYTES = 4 * 1024 * 1024


def read_chunking_draft(path: Path) -> bytes:
    """Read one bounded regular-file snapshot, never follow draft/parent symlinks."""
    from .bump_spec import PlanValidationError
    path = Path(path).absolute()
    if path.parent.resolve() != path.parent:
        raise PlanValidationError("draft", "draft directory must not redirect through symlinks")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        raise PlanValidationError("draft", "write the assigned draft file before validation") from exc
    except OSError as exc:
        if path.is_symlink():
            raise PlanValidationError("draft", "draft must not be a symlink") from exc
        raise
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise PlanValidationError("draft", "draft must be a private regular file, not a hardlink/device")
        data = handle.read(MAX_CHUNKING_DRAFT_BYTES + 1)
        after = os.fstat(handle.fileno())
        try:
            current = path.lstat()
        except FileNotFoundError as exc:
            raise PlanValidationError("draft", "draft was removed during validation; finish editing and retry") from exc
    if len(data) > MAX_CHUNKING_DRAFT_BYTES:
        raise PlanValidationError("draft", "draft exceeds the 4 MiB JSON limit")
    identity = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns)
    if identity(before) != identity(after) or identity(after) != identity(current):
        raise PlanValidationError("draft", "draft changed during validation; retry after finishing the edit")
    return data


def prepare_chunking_draft(paths, state: dict, payload: bytes) -> tuple[dict, dict]:
    """All editable-plan checks, using the bound environment as read-only context.

    Live environment validation remains a controller infrastructure check, not
    an instruction for the model to rewrite its DAG or run Lean builds.
    """
    from .bump_spec import PlanValidationError
    try:
        draft = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PlanValidationError("draft", str(exc), code="invalid_json") from exc
    source = bump_state.formal_source(state)
    plan = {**source, "solution_candidate": source["candidate_id"], "solution_sha256": source["sha256"]}
    dag = normalize_chunking_dag(assemble_chunking_draft(state, draft), plan=plan,
                                 expected_solution_sha=source["sha256"])
    main_sha = state["formalization"]["main_sha"]
    environment = (state["formalization"].get("contract") or {}).get(
        "environment", (state.get("project_baseline") or {}).get("environment", {}))
    contract = bump_contract.prepare_source_contract(
        paths, dag, state=state, environment=environment, main_sha=main_sha)
    bump_state.prepare_informal_plan(state, dag, main_sha=main_sha, contract=contract)
    return dag, contract


def chunking_diagnostic(exc: ValueError) -> dict:
    from .bump_spec import PlanValidationError
    row = (exc.diagnostic if isinstance(exc, PlanValidationError)
           else {"path": "dag", "code": "invalid_plan", "message": str(exc)})
    # The exact draft is an artifact; feedback must remain bounded even if a
    # malformed draft has thousands of IDs or deeply nested unexpected values.
    def compact(value, depth=0):
        if isinstance(value, str):
            return value[:800]
        if depth >= 3:
            return str(value)[:200]
        if isinstance(value, dict):
            return {str(key)[:100]: compact(item, depth + 1)
                    for key, item in list(value.items())[:12]}
        if isinstance(value, (list, tuple)):
            return [compact(item, depth + 1) for item in value[:12]]
        return value
    return compact(row)


def validate_chunking_draft(paths, draft_path: Path) -> dict:
    """Read-only preflight; neither acceptance nor attempt bookkeeping."""
    state = bump_state.load_state(paths.forum)
    payload = None
    try:
        require_source_matches(paths, state)
        payload = read_chunking_draft(draft_path)
        dag, _ = prepare_chunking_draft(paths, state, payload)
    except (ValueError, RecursionError) as exc:
        return {"ok": False, "error": str(exc)[:2000], "errors": [chunking_diagnostic(exc)],
                "draft_sha256": hashlib.sha256(payload).hexdigest() if payload is not None else None}
    return {"ok": True, "chunk_count": len(dag["chunks"]), "draft_sha256": hashlib.sha256(payload).hexdigest(),
            "base_revision": state["formalization"]["revision"]}


def _git(project: Path, *args: str) -> subprocess.CompletedProcess:
    return bump_jobs.run(
        project, ["git", *args], cwd=project, owner="Unity", task_id="integration-git",
    )


def _tracked_tree_matches(root: Path, tree: str) -> bool:
    """Check actual bytes/modes, including assume-unchanged files Git diff skips."""
    listing = _git(root, "ls-tree", "-r", "-z", tree)
    object_format = _git(root, "rev-parse", "--show-object-format")
    if listing.returncode or object_format.returncode:
        return False
    algorithm = object_format.stdout.strip()
    if algorithm not in {"sha1", "sha256"}:
        return False
    for entry in listing.stdout.split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, kind, oid = metadata.split()
        path = root / name
        if kind != "blob" or mode not in {"100644", "100755", "120000"}:
            return False  # No implicit handling of submodule work or other modes.
        if any(parent.is_symlink() for parent in path.parents if parent != root and parent.is_relative_to(root)):
            return False
        try:
            if mode == "120000":
                if not path.is_symlink():
                    return False
                payload = os.fsencode(os.readlink(path))
            else:
                if path.is_symlink() or not path.is_file():
                    return False
                executable = bool(path.stat().st_mode & 0o111)
                if executable != (mode == "100755"):
                    return False
                payload = path.read_bytes()
        except OSError:
            return False
        digest = hashlib.new(algorithm, f"blob {len(payload)}\0".encode() + payload).hexdigest()
        if digest != oid:
            return False
    return True


def _owned_application(root: Path, before: str, receipt: dict) -> bool:
    head = _git(root, "rev-parse", "HEAD")
    branch = _git(root, "symbolic-ref", "--quiet", "HEAD")
    return (not head.returncode and head.stdout.strip() == before
            and not branch.returncode and branch.stdout.strip() == receipt.get("branch")
            and _checked_tree(root) == receipt.get("applied_tree")
            and _tracked_tree_matches(root, receipt["applied_tree"]))


def _rollback(root: Path, before: str, *, receipt: dict | None = None) -> subprocess.CompletedProcess:
    """Undo only the exact controller-applied index; never reset an unknown HEAD."""
    with bump_jobs.cancellation_disabled():
        if not receipt or not receipt.get("applied_tree"):
            # A preflight/identity exception did not authorize changing main.
            head = _git(root, "rev-parse", "HEAD")
            branch = _git(root, "symbolic-ref", "--quiet", "HEAD")
            dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
            before_tree = _checked_tree(root, before)
            if (head.returncode or head.stdout.strip() != before or dirty.returncode or dirty.stdout.strip()
                    or (receipt and receipt.get("branch") != branch.stdout.strip())
                    or _checked_tree(root) != before_tree or not _tracked_tree_matches(root, before_tree)):
                raise ValueError("Main changed before controller application; all edits/commits preserved. "
                                 "Stop and reconcile main before resuming.")
            return subprocess.CompletedProcess(["rollback-not-needed"], 0, "", "")
        if not _owned_application(root, before, receipt):
            raise ValueError("Main no longer matches the controller-applied candidate; all edits/commits preserved. "
                             "Stop and reconcile main before resuming.")
        if receipt["applied_tree"] == receipt["before_tree"]:
            receipt.clear()
            return subprocess.CompletedProcess(["rollback-no-tree-change"], 0, "", "")
        # Two-tree read-tree updates only index/worktree and has Git's normal
        # dirty/untracked protections. Unlike reset --hard it never rewinds HEAD.
        result = _git(root, "read-tree", "-u", "-m", receipt["applied_tree"], receipt["before_tree"])
        if result.returncode:
            raise ValueError("Safe main rollback refused; all remaining work preserved. Reconcile before resuming: "
                             + (result.stderr.strip() or result.stdout.strip() or "git read-tree failed"))
        receipt.clear()
        if worktree.main_commit(root) != before:
            raise ValueError("Main HEAD changed during rollback; the commit was preserved. Reconcile before resuming.")
        return result


def _preview_candidate_tree(root: Path, before: str, patch: str, task_id: str) -> subprocess.CompletedProcess:
    """Resolve three-way conflicts against an isolated index, never main's files."""
    with tempfile.TemporaryDirectory(prefix="unity-bump-index-") as directory:
        index = str(Path(directory) / "index")
        prefix = ["env", f"GIT_INDEX_FILE={index}", "git"]
        for command, payload in ((["read-tree", before], None),
                                 (["apply", "--cached", "--3way", "-"], patch)):
            result = bump_jobs.run(root, [*prefix, *command], cwd=root, input=payload,
                                         owner="Unity", task_id=task_id)
            if result.returncode:
                return result
        return bump_jobs.run(root, [*prefix, "write-tree"], cwd=root,
                                  owner="Unity", task_id=task_id)


def _formal_worktree(project_root: Path, author: str) -> Path:
    """Reuse an owned registered tree; never let generic creation erase old work."""
    tree = worktree.agent_worktree(project_root, author)
    branch = worktree.agent_branch(author, project_path=project_root)
    if not tree.exists() and not tree.is_symlink():
        if _git(project_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
            raise ValueError(f"Preserved branch {branch} has no worktree; reconcile it before resuming bump")
        return worktree.create_worktree(author, project_root)

    registered = _git(project_root, "worktree", "list", "--porcelain")
    top = _git(tree, "rev-parse", "--show-toplevel") if tree.is_dir() else None
    head = _git(tree, "symbolic-ref", "--short", "HEAD") if tree.is_dir() else None
    expected = f"worktree {tree.resolve()}\n"
    matching = any(record.startswith(expected) and f"branch refs/heads/{branch}" in record.splitlines()
                   for record in registered.stdout.split("\n\n"))
    root_common = _git(project_root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    tree_common = _git(tree, "rev-parse", "--path-format=absolute", "--git-common-dir") if tree.is_dir() else None
    if (tree.is_symlink() or registered.returncode or not matching or top is None or top.returncode
            or Path(top.stdout.strip()).resolve() != tree.resolve()
            or head is None or head.returncode or head.stdout.strip() != branch
            or root_common.returncode or tree_common is None or tree_common.returncode
            or root_common.stdout.strip() != tree_common.stdout.strip()):
        raise ValueError(f"Existing path {tree} is not the expected registered worktree for {author}; "
                         "preserved unchanged. Reconcile it before resuming bump")
    return tree


@contextmanager
def _merge_lock(project_root: Path):
    path = project_root / ".unity" / "forum" / "merge.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        while True:
            bump_jobs.check_cancelled()
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _review_new_declaration(project_root: Path, task: dict, diff: str, *,
                            contract: dict | None = None,
                            formal_tasks: list[dict] | None = None,
                            layout: dict | None = None, environment: dict | None = None,
                            timings: dict | None = None, candidate: dict | None = None) -> dict:
    issues = []
    expected = task.get("lean_decl", "")
    tasks = formal_tasks or [task]
    stage = (candidate or {}).get("stage", "complete")
    completed = {item["task_id"] for item in tasks
                 if item.get("status") == "complete"
                 or (item["task_id"] == task["task_id"] and stage == "complete")}
    incremental = {}
    if (contract or {}).get("version") in {3, 4}:
        incremental = {"task_id": task["task_id"], "stage": stage,
                       "proposed_outputs": (candidate or {}).get("outputs"),
                       "final": (contract or {}).get("migration_policy") not in {1, 2} and len(completed) == len(tasks)}
    try:
        check = bump_contract.check_formal_contract(project_root, contract or {}, tasks,
                                                     completed=completed, layout=layout,
                                                     environment=environment, timings=timings,
                                                     **incremental)
    except (OSError, ValueError) as exc:
        check = {"passed": False, "issues": [f"formal contract verification unavailable: {exc}"]}
    issues.extend(check["issues"])
    return {
        "status": "passed" if not issues else "failed",
        "expected_decl": expected,
        "source_components": list(task.get("source_components", [])),
        "mode": "formal_contract",
        "contract_sha256": check.get("proposed_contract", contract or {}).get("sha256"),
        "policy_sha256": bump_contract.policy_hash(),
        "stage": stage,
        "verified_tasks": check.get("verified_tasks", sorted(completed)),
        **{key: check[key] for key in ("proposed_contract", "verified_targets", "final", "project_declarations", "compiled_receipt", "module_receipt", "source_identity") if key in check},
        "issues": issues,
        "blockers": check.get("blockers", []),
    }


def _candidate_preflight(state: dict, candidate: dict) -> list[dict]:
    return bump_state.submission_blockers(
        state, candidate["task_id"], candidate.get("stage", "complete"),
        outputs=candidate.get("outputs"),
    )


def _checked_tree(root: Path, revision: str | None = None) -> str:
    """Read a committed or index tree, failing closed on Git errors."""
    result = (_git(root, "rev-parse", f"{revision}^{{tree}}") if revision is not None
              else _git(root, "write-tree"))
    if result.returncode or not result.stdout.strip():
        raise ValueError(result.stderr.strip() or "could not read candidate source tree")
    return result.stdout.strip()


def _verification_payload(verification: dict) -> str:
    """Keep every verification field without indenting deep native evidence."""
    return json.dumps(verification, sort_keys=True, separators=(",", ":")) + "\n"


def _apply_formal_candidate(paths, candidate: dict, task: dict, *, timings: dict | None = None,
                            rollback_receipt: dict | None = None) -> dict:
    """Verify an immutable candidate; commit only if its integration changes main."""
    root = paths.project_root
    current = bump_state.load_state(paths.forum)
    require_source_matches(paths, current)
    contract = current["formalization"].get("contract", {})
    if not contract:
        return {"ok": False, "error": "missing formal contract; request re-chunking before proving"}
    if not bump_state.candidate_is_current(current, candidate):
        return {"ok": False, "error": "candidate belongs to a superseded formal contract"}
    # A queued candidate may become the final task after another merge. Recheck
    # the same cheap gate used at submission before mutating main or building.
    blockers = _candidate_preflight(current, candidate)
    if blockers:
        issues = [row["message"] for row in blockers]
        return {"ok": False, "error": "; ".join(issues), "blockers": blockers,
                "failure_context": bump_state.failure_state_context(current),
                "verification": {"status": "failed", "mode": "preflight",
                                 "issues": issues, "blockers": blockers}}
    baseline = (bump_contract.migration_baseline_v2(root, contract)
                if contract.get("migration_policy") == 2 else contract.get("project_baseline"))
    if baseline is not None:
        from .bump_project import require_pinned_inputs
        require_pinned_inputs(root, baseline,
                              allowed_new_paths=bump_contract.adopted_output_paths(contract))
    try:
        resolved = worktree.verify_candidate_commit(
            root, candidate["author"], candidate["commit_sha"], allow_unchanged=True,
        )
    except Exception as exc:
        return {"ok": False, "error": f"candidate identity failed: {exc}"}
    diff_result = _git(
        root, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--full-index",
        candidate["base_main_sha"], resolved,
    )
    if diff_result.returncode:
        return {"ok": False, "error": "could not read cumulative candidate diff"}
    exact_diff = diff_result.stdout
    if hashlib.sha256(exact_diff.encode()).hexdigest() != candidate["diff_sha256"]:
        return {"ok": False, "error": "candidate commit no longer matches its submitted diff hash"}
    actual_paths = bump_files.immutable_git_paths(root, candidate["base_main_sha"], resolved)
    blockers = bump_files.validate_candidate_files(current, candidate, **actual_paths)
    if blockers:
        return {"ok": False, "error": "; ".join(row["message"] for row in blockers), "blockers": blockers}
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    if dirty.returncode or dirty.stdout.strip():
        return {"ok": False, "error": "main has tracked changes; refusing candidate merge"}
    before = worktree.main_commit(root)
    before_tree = _checked_tree(root, before)
    receipt = rollback_receipt if rollback_receipt is not None else {}
    branch = _git(root, "symbolic-ref", "--quiet", "HEAD")
    if branch.returncode or not branch.stdout.strip():
        raise ValueError("Candidate integration requires the original named project branch")
    if not _tracked_tree_matches(root, before_tree):
        raise ValueError("Main contains tracked byte/mode changes hidden from Git status; preserving them")
    if exact_diff:
        preview = _preview_candidate_tree(root, before, exact_diff, task["task_id"])
        if preview.returncode:
            return {"ok": False, "error": preview.stderr.strip() or "candidate conflicts with main",
                    "failure_kind": "merge_conflict", "failure_main_sha": before}
        expected_tree = preview.stdout.strip()
    else:
        expected_tree = before_tree
    latest_branch = _git(root, "symbolic-ref", "--quiet", "HEAD")
    if (worktree.main_commit(root) != before or latest_branch.returncode
            or latest_branch.stdout.strip() != branch.stdout.strip()
            or _checked_tree(root) != before_tree or not _tracked_tree_matches(root, before_tree)):
        raise ValueError("Main changed while preparing candidate application; preserving unknown work")
    receipt.update(before_tree=before_tree, applied_tree=expected_tree, branch=branch.stdout.strip())
    if exact_diff:
        applied = bump_jobs.run(
            root,
            ["git", "apply", "--3way", "--index", "-"],
            cwd=root, input=exact_diff, owner="Unity", task_id=task["task_id"],
        )
        if applied.returncode:
            unmerged = _git(root, "ls-files", "--unmerged", "-z")
            conflict = not unmerged.returncode and bool(unmerged.stdout)
            _rollback(root, before, receipt=receipt)
            return {
                "ok": False, "error": applied.stderr.strip() or "candidate conflicts with main",
                **({"failure_kind": "merge_conflict", "failure_main_sha": before} if conflict else {}),
            }
    elif _checked_tree(root, resolved) != before_tree:
        receipt.clear()
        return {"ok": False, "error": "Main differs from this empty candidate; sync_from_main and resubmit."}
    if not _owned_application(root, before, receipt):
        raise ValueError("Main changed during candidate application; preserving unknown edits and commits")
    if baseline is not None:
        try:
            require_pinned_inputs(root, baseline, allowed_new_paths={
                *bump_contract.adopted_output_paths(contract),
                *[row["file"] for row in candidate.get("outputs", [])]})
        except ValueError as exc:
            _rollback(root, before, receipt=receipt)
            return {"ok": False, "error": str(exc)}
    with bump_contract.measure(timings, "workspace_seconds"):
        try:
            layout = (deepcopy(baseline["layout"]) if contract.get("migration_policy") == 2
                      else bump_contract.workspace_layout(root))
            layout = bump_contract.scoped_layout(root, layout, baseline)
        except (OSError, ValueError) as exc:
            _rollback(root, before, receipt=receipt)
            return {"ok": False, "error": "project verification scope rejected candidate: " + str(exc)}
    with bump_contract.measure(timings, "initial_identity_seconds"):
        checked_source = bump_contract.source_identity(root, layout=layout)
    checked_tree = _checked_tree(root)
    no_tree_change = checked_tree == before_tree
    if not exact_diff and not no_tree_change:
        raise ValueError("source tree changed before empty candidate verification")
    if checked_source["main_sha"] != before:
        raise ValueError("main changed before candidate verification")
    build_started = time.monotonic()
    try:
        build = bump_contract.build_sources(
            root, full=True, layout=layout, task_id=task["task_id"], timings=timings,
            **({"baseline": contract["project_baseline"], "tasks": [
                *bump_contract.adopted_output_tasks(contract, root=root),
                *[{"lean_file": row["file"]} for row in candidate.get("outputs", [])],
            ]} if contract.get("project_baseline", {}).get("policy") == "changes-v1" else
               {"baseline": baseline, "tasks": [task]}
               if contract.get("migration_policy") in {1, 2} else {}),
        )
    except OSError as exc:
        build_seconds = time.monotonic() - build_started
        _rollback(root, before, receipt=receipt)
        return {
            "ok": False, "error": f"could not run lake build: {exc}",
            "build": {"returncode": None, "seconds": build_seconds},
        }
    build_seconds = time.monotonic() - build_started
    # Invalidation precedes BOTH build passes, whose complete duration/output is
    # recorded. Default targets alone need not include every inspected module.
    output = build["output"]
    build_record = {"returncode": build["returncode"], "seconds": build_seconds}
    if output:
        record = artifacts.store_text(
            paths.artifacts, output, kind="bump_formal_build",
            source="lake build + explicit source modules", producer="Unity",
            metadata={"candidate_id": candidate["candidate_id"], "task_id": task["task_id"]},
        )
        build_record.update({"artifact_id": record["artifact_id"], "sha256": record["sha256"]})
    if build["returncode"]:
        _rollback(root, before, receipt=receipt)
        return {
            "ok": False,
            "error": "lake build failed: " + artifacts.preview_text(output, 3000),
            "build": build_record,
        }
    if _git(root, "diff", "--quiet").returncode:
        _rollback(root, before, receipt=receipt)
        return {"ok": False, "error": "lake build changed tracked files", "build": build_record}
    staged = _git(root, "diff", "--cached", "--no-ext-diff", before).stdout
    verification_started = time.monotonic()
    verification = _review_new_declaration(
        root, task, staged, contract=contract,
        formal_tasks=list(current["formal_tasks"].values()),
        layout=layout, environment=checked_source["environment"], timings=timings,
        candidate=candidate,
    )
    verification["environment_sha256"] = bump_contract.digest(checked_source["environment"])
    with bump_contract.measure(timings, "postcheck_identity_seconds"):
        reviewed_source = bump_contract.source_identity(root)
    if (reviewed_source != checked_source
            or _checked_tree(root) != checked_tree):
        raise ValueError("source changed during candidate build or kernel inspection")
    verification["seconds"] = time.monotonic() - verification_started
    record = artifacts.store_text(
        paths.artifacts, _verification_payload(verification),
        kind="bump_formal_verification", producer="Unity",
        source=f"formal task {task['task_id']}",
    )
    verification["artifact_id"] = record["artifact_id"]
    verification["verification_artifact"] = {
        "artifact_id": record["artifact_id"], "sha256": record["sha256"],
    }
    if verification.pop("project_declarations", None):
        verification["inventory_artifact"] = {"artifact_id": record["artifact_id"], "sha256": record["sha256"]}
    if verification["status"] != "passed":
        _rollback(root, before, receipt=receipt)
        return {
            "ok": False,
            "error": "; ".join(verification["issues"]),
            "build": build_record,
            "verification": verification,
            "blockers": verification.get("blockers", []),
        }
    require_source_matches(paths, current)
    if not _owned_application(root, before, receipt):
        raise ValueError("Main changed after verification; preserving edits/commits instead of committing an unknown tree")
    if not no_tree_change:
        commit = _git(root, "commit", "-m", f"UNITY: merge {PIPELINE} task {task['task_id']}")
        if commit.returncode:
            _rollback(root, before, receipt=receipt)
            return {"ok": False, "error": commit.stderr.strip() or "could not commit candidate"}
    with bump_contract.measure(timings, "postcommit_identity_seconds"):
        committed_source = bump_contract.source_identity(root)
    expected_source = (checked_source if no_tree_change
                       else {**checked_source, "main_sha": committed_source["main_sha"]})
    if (committed_source != expected_source
            or _checked_tree(root, "HEAD") != checked_tree
            or _checked_tree(root) != checked_tree):
        raise ValueError("commit changed the verified candidate source")
    verification["source_identity"] = committed_source
    return {
        "ok": True,
        "main_sha": committed_source["main_sha"],
        "build": build_record,
        "verification": verification,
    }


def _integrate_checked(paths, candidate: dict, task: dict) -> dict:
    root = paths.project_root
    # Nothing below may discard pre-existing tracked edits.
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    if dirty.returncode or dirty.stdout.strip():
        return {"ok": False, "error": "main has tracked changes; refusing candidate merge"}
    before = worktree.main_commit(root)
    timings = {}
    started = time.monotonic()
    result = {}
    rollback_receipt = {"branch": _git(root, "symbolic-ref", "--quiet", "HEAD").stdout.strip()}
    try:
        current = bump_state.load_state(paths.forum)
        if any(row.get("status") == "failed" and row.get("task_id") == candidate["task_id"]
               and (row.get("failure_context") or {}).get("cacheable")
               for row in current.get("formal_candidates", {}).values()):
            try:
                observation = bump_contract.observe_failure_inputs(
                    root, current["formalization"].get("contract") or {},
                )
                previous = bump_state.matching_failed_candidate(current, candidate, observation)
            except (OSError, ValueError):
                previous = None
            if previous:
                result = {"ok": False, "error": previous["error"],
                          "blockers": previous.get("blockers", []),
                          "failure_context": previous["failure_context"],
                          "verification": previous.get("verification"), "unchanged_failed": True}
                return result
        result = _apply_formal_candidate(paths, candidate, task, timings=timings,
                                        rollback_receipt=rollback_receipt)
        bump_jobs.check_cancelled()
        blockers = result.get("blockers", [])
        if (not result.get("ok") and blockers
                and all(row.get("deterministic") is True for row in blockers)
                and (result.get("verification") or {}).get("mode") != "preflight"):
            # Main has been rolled back. Observe actual source/environment, not
            # the roster's claim or a receipt ID. A failed observation disables
            # reuse; it must not turn an IO failure into a permanent rejection.
            try:
                observation = bump_contract.observe_failure_inputs(
                    root, bump_state.load_state(paths.forum)["formalization"].get("contract") or {},
                )
                if observation["environment_sha256"] == (result.get("verification") or {}).get("environment_sha256"):
                    result["failure_context"] = {**observation, "cacheable": True}
            except (OSError, ValueError):
                pass
    except (OSError, ValueError, KeyError) as exc:
        _rollback(root, before, receipt=rollback_receipt)
        result = {"ok": False, "error": f"candidate verification failed: {exc}",
                  "cancelled": isinstance(exc, bump_jobs.JobCancelled)}
    finally:
        timings["total_seconds"] = time.monotonic() - started
        # Detailed profiling is telemetry, never prompt memory or acceptance
        # evidence. Failure to write it must not undo a verified commit.
        try:
            artifact = artifacts.store_text(
                paths.artifacts, json.dumps(timings, sort_keys=True) + "\n",
                kind="bump_formal_timings", producer="Unity",
                metadata={"candidate_id": candidate["candidate_id"], "task_id": task["task_id"]},
            )
            record = result.get("verification", result.get("build"))
            if isinstance(record, dict):
                record["timing_artifact_id"] = artifact["artifact_id"]
        except (OSError, ValueError):
            pass
    return result


def _integrate_formal_candidate(paths, candidate: dict, task: dict) -> dict:
    """Apply one candidate under the merge lock (also useful for integration tests)."""
    with _merge_lock(paths.project_root):
        return _integrate_checked(paths, candidate, task)


def _integrate_and_record(paths, candidate: dict, task: dict, cancel_event: Event | None = None) -> dict:
    """Serialize Git integration AND state publication under the same lock."""
    def record(result: dict) -> dict:
        if result.get("cancelled") and bump_state.pending_replan(bump_state.load_state(paths.forum)):
            bump_state.defer_formal_merge(paths.forum, candidate["candidate_id"], reason=result["error"])
            return {**result, "deferred": True}
        bump_state.finish_formal_merge(
            paths.forum, candidate["candidate_id"], success=bool(result.get("ok")),
            main_sha=result.get("main_sha", ""), error=result.get("error", ""),
            build=result.get("build"), verification=result.get("verification"),
            failure_kind=result.get("failure_kind", ""),
            failure_main_sha=result.get("failure_main_sha", ""),
            blockers=result.get("blockers"), failure_context=result.get("failure_context"),
        )
        return result

    with bump_jobs.cancellation_scope(cancel_event):
        try:
            with _merge_lock(paths.project_root):
                try:
                    result = _integrate_checked(paths, candidate, task)
                except bump_jobs.JobCancelled as exc:
                    result = {"ok": False, "cancelled": True, "error": str(exc)}
                return record(result)
        except bump_jobs.JobCancelled as exc:
            # Cancellation before acquiring the lock made no source mutation.
            return record({"ok": False, "cancelled": True, "error": str(exc)})


def _blocker_recovery_context(blockers: list[dict]) -> str:
    """Bound launch memory; full diagnostics live in the task/detail artifacts."""
    preview = [{"code": str(row.get("code", ""))[:100],
                "prerequisite_id": str(row.get("prerequisite_id", ""))[:100],
                "task_ids": [str(key)[:100] for key in row.get("task_ids", [])[:6]],
                "message": str(row.get("message", ""))[:300],
                "required_action": str(row.get("required_action", ""))[:400]}
               for row in blockers[:6]]
    return (f"Showing {len(preview)} of {len(blockers)} blockers. Full evidence: "
            "bump_task / bump_requirements.\n"
            + json.dumps(preview, ensure_ascii=False) + "\n")


def _manifest_repair_scope(state: dict, repair: dict) -> set[str]:
    """Pause only the repaired outputs and their declared dependent work."""
    roots = {repair["task_id"]}
    roots.update(key for blocker in repair.get("blockers", []) for key in blocker.get("task_ids", []))
    return _affected_tasks(state, list(roots)) & state.get("formal_tasks", {}).keys()


def _manifest_repair_pending_candidate(state: dict, repair: dict) -> bool:
    scope = _manifest_repair_scope(state, repair)
    return any(candidate.get("task_id") in scope
               and candidate.get("status") in {"submitted", "merging"}
               and bump_state.candidate_is_current(state, candidate)
               for candidate in state.get("formal_candidates", {}).values())


def _select_manifest_repairs(state: dict, repairs: list[dict], active_inputs: set[tuple[str, str]]) -> list[dict]:
    """Serialize overlapping repair scopes without cancelling their current owner."""
    ordered = sorted(repairs, key=lambda row: (
        (row["repair_id"], row["input_sha256"]) not in active_inputs,
        row.get("status") != "open", -len(_manifest_repair_scope(state, row)), row["repair_id"],
    ))
    selected, occupied = [], set()
    for repair in ordered:
        scope = _manifest_repair_scope(state, repair)
        if not scope.intersection(occupied):
            selected.append(repair)
            occupied.update(scope)
    return selected


def _manifest_repair_prompt(repair: dict) -> str:
    return (
        f"MANIFEST REPAIR {repair['repair_id']}: you are the focused repair worker for "
        f"task `{repair['task_id']}`. Preserve existing Lean declarations, dependent proofs, "
        "and private work. Read bump_task and the exact blockers below. If the adopted "
        "output manifest must change, use refine_chunks with reopen_representations once; "
        "do not repeatedly submit the changed manifest against the old representation. "
        "After a refinement refresh the task and strategy generation before submitting. "
        "Reuse existing proof bytes and submit unchanged complete work when appropriate; "
        "correct Lean declarations or proofs when required, but do not rewrite supplied source "
        "or bypass fresh verification and review. "
        "If blocked, publish the precise blocker and yield_task.\n"
        + _blocker_recovery_context(repair.get("blockers", []))
    )


def _critic_repair_prompt(paths, state: dict, task_id: str, *, feedback: dict | None = None) -> str:
    """Deliver exact historical semantic feedback independently of the bounded brief."""
    if feedback is None:
        feedback = bump_state.critic_feedback_for_task(state, task_id)
    direct, upstream = feedback.get("direct", []), feedback.get("upstream", [])
    if not direct and not upstream:
        return ""
    sections = [
        "CRITIC REPAIR GUIDANCE — historical review evidence, not acceptance or a current machine failure. "
        "Each entry identifies its requirement, reviewed task mapping, verdict, snapshot and main commit. "
        "Compare it with the current source/task revision; the recorded failure is not proof that every "
        "current edit still has the defect. New edits and a successful build do not establish source faithfulness."
    ]
    if direct:
        sections.append(
            "DIRECT FAILED REQUIREMENTS FOR YOUR ASSIGNED TASK: read the full rationale, "
            "argument_rationale and repair_steps below. Address the identified gaps before resubmitting; "
            "do not just replay the previous proof or submit because it compiles. Explain how the current "
            "implementation addresses each applicable repair step. A shared task mapping is a diagnosis, "
            "not a claim that every mapped node independently has every defect. task_ids identify the "
            "explicit repair targets; requirement_task_ids identify the requirement's implementing nodes. "
            "An explicitly reopened dependency provider may need repair for another node's requirement. "
            "Preserve correct existing "
            "work and refine the task/representation explicitly if needed. Once the gaps are addressed, "
            "submit the candidate for normal verification and independent review; do not wait for a new "
            "critic approval before submitting."
        )
    if upstream:
        sections.append(
            "UPSTREAM FEEDBACK — NOT A NEW ASSIGNMENT: these failures concern dependencies of your task. "
            "Do not redo your already-correct result or take over a dependency's proof merely because "
            "it appears here. Keep working within your assigned task and actual readiness; if its progress "
            "depends on the upstream repair, record that precise dependency blocker and yield_task. "
            "Only an explicit assignment/refinement changes your work scope."
        )
    # artifact_read pages by byte offset. ASCII JSON escapes preserve exact
    # Unicode evidence without splitting a multibyte character between pages.
    payload = json.dumps({"task_id": task_id, "direct": direct, "upstream": upstream},
                         ensure_ascii=True, sort_keys=True, indent=2)
    compacted = artifacts.compact_text(
        paths.artifacts, payload, kind="bump_critic_feedback", producer="Unity", source=task_id,
        metadata={"run_id": state.get("run_id"), "task_id": task_id,
                  "direct_count": len(direct), "upstream_count": len(upstream)},
    )
    if isinstance(compacted, dict):
        sections.append(
            "The preview below is incomplete. Before editing or submitting, read the FULL feedback "
            f"artifact `{compacted['artifact_id']}` with artifact_read starting at offset 0, then follow "
            "next_offset until null. Read every rationale and repair step; do not treat the preview as "
            "the complete repair checklist."
        )
    sections.append(artifacts.format_compacted(compacted))
    return "\n\n".join(sections)


def _compose_formal_task_prompt(*, recovery: str, resume: str, followup: str,
                               normal: str, representation: str, repair: dict | None = None,
                               critic_repair: str = "", direct_critic_repair: bool = False,
                               work_context: str = "") -> str:
    # Direct semantic failures and mechanical rejection recovery take precedence
    # over opportunistic submission nudges; upstream guidance does not reassign work.
    recovering = bool(recovery) or direct_critic_repair
    return "\n".join(part for part in (
        _manifest_repair_prompt(repair) if repair else "",
        recovery, critic_repair, work_context, "" if recovering else resume,
        normal if recovering else followup or normal, representation,
    ) if part)


def _rejection_recovery_prompt(state: dict, author: str, task_id: str) -> str:
    """A current rejection takes priority over opportunistic submission nudges."""
    relevant = [
        item for item in state.get("formal_candidates", {}).values()
        if item.get("task_id") == task_id
        and bump_state.candidate_is_current(state, item)
        and (item.get("status") == "merged" or (
            item.get("status") == "failed"
            and (bump_state.author_key(item.get("author")) == bump_state.author_key(author)
                 or bump_state.participates(
                     state.get("strategies", {}).get(item.get("strategy_id"), {}), author))
        ))
    ]
    latest = max(relevant, key=lambda item: item.get("updated_at", item.get("created_at", 0)), default=None)
    if not latest or latest["status"] != "failed":
        return ""
    evidence = " ".join(
        f"Read artifact {record['artifact_id']} for full evidence."
        for record in (latest.get("build") or {}, latest.get("verification") or {})
        if record.get("artifact_id")
    )
    return (
        f"RECOVER REJECTED CANDIDATE {latest['candidate_id']} at {latest['commit_sha']}: "
        f"{latest.get('error', '')[:2000]}\n{evidence}\n"
        + ("EXACT BLOCKERS (may belong to other already-complete tasks):\n"
           + _blocker_recovery_context(latest["blockers"])
           + "Follow each blocker's required_action. Prerequisite IDs need evidence-record/witness repairs; "
           "Lean signature or proof errors need the indicated code repair. Do not rewrite an unrelated "
           "proof or add an output merely to change the submission. Agent-reported findings do not "
           "override this checked failure. Preserve your candidate bytes.\n"
           if latest.get("blockers") else "")
        + "Fix the reported rejection before finalizing again; do not resubmit the unchanged failure. "
        "For merge conflicts, commit intended private edits, call sync_from_main, and resolve conflicts "
        "without removing accepted work. Preserve adopted declarations at their exact fully-qualified "
        "names, including namespace scope. A successful private build does not resolve an integration "
        "or declaration-identity failure. Inspect the rejected diff and evidence before new research.\n"
    )


def _formal_launch_retry_key(state: dict, author: str, task_id: str, previous_task: str) -> str:
    """Retry preserved work only when its task, assignment, or accepted base changes."""
    identity = bump_state.author_key(author)
    previous = state.get("worker_tasks", {}).get(identity, previous_task)
    return bump_state.digest({
        "accepted_main": state["formalization"].get("main_sha"),
        "formalization_revision": state["formalization"].get("revision"),
        "intended_previous": previous_task,
        "actual_previous": previous,
        "tasks": {key: {
            "status": state["formal_tasks"][key].get("status"),
            "attempt": bump_state.snapshot_attempt(state, author, key),
        } for key in {previous, task_id} if key in state["formal_tasks"]},
        "previous_lineage": state.get("retired_tasks", {}).get(previous, {}).get("replaced_by"),
        "previous_yielded": bump_state.has_yielded(state, author, previous),
        "unresolved": bump_server.unresolved_formal_tasks(state, author),
        "checkpoints": {key: state.get("worktree_checkpoints", {}).get(identity, {}).get(key)
                        for key in {previous, task_id} if key},
    })


def recover_interrupted_formal_merges(paths) -> None:
    """Reopen clean interrupted merges; never discard ambiguous main changes."""
    with _merge_lock(paths.project_root):
        state = bump_state.load_state(paths.forum)
        formal = state["formalization"]
        interrupted = [
            candidate for candidate in state["formal_candidates"].values()
            if state["phase"] == "formalizing"
            and candidate.get("status") == "merging"
            and bump_state.candidate_is_current(state, candidate)
        ]
        if not interrupted:
            return
        status = _git(
            paths.project_root, "status", "--porcelain", "--untracked-files=no",
        )
        if (
            status.returncode
            or status.stdout.strip()
            or worktree.main_commit(paths.project_root) != formal["main_sha"]
        ):
            raise ValueError(
                "Interrupted formal merge: main is dirty or differs from "
                f"the last accepted commit {formal['main_sha']}. "
                "Inspect and reconcile main before resuming. No changes were discarded."
            )
        for candidate in interrupted:
            bump_state.finish_formal_merge(
                paths.forum, candidate["candidate_id"], success=False,
                error="Merge interrupted; task reopened for resubmission.",
            )


async def run_formalizing_runtime(roster, paths, mcp: dict, base_prompt: str) -> dict:
    """Swarm ready formal tasks and integrate immutable candidates using Forum events."""
    if stop_requested(paths.project_root):
        return bump_state.load_state(paths.forum)
    configure_forum(paths, "formalizing")
    state = bump_state.load_state(paths.forum)
    require_source_matches(paths, state)
    if state.get("migration_global_blocker"):
        raise ValueError("Bump has a saved global machine-review blocker; inspect its exact evidence before retrying")
    tools_prompt = load_prompt(f"{PIPELINE.upper()}_FORMALIZING_TOOLS")
    context = library.library_context()
    subagents = library.library_subagents()
    agents = {agent.name: agent for agent in roster.agents}
    agent_names = {bump_state.author_key(name): name for name in agents}
    worktrees: dict[str, Path] = {}
    tasks: dict[str, asyncio.Task] = {}
    stopping: dict[str, asyncio.Task] = {}
    roles: dict[str, str] = {}
    repair_issues: dict[str, str] = {}
    repair_exhausted: dict[str, set[str]] = {}
    review_inputs: dict[str, tuple[str, str]] = {}
    integration: asyncio.Task | None = None
    integration_candidate: dict = {}
    integration_cancel: Event | None = None
    interrupts: dict[str, asyncio.Event] = {}
    worker_targets: dict[str, str] = {}
    worker_revisions: dict[str, tuple[str, int]] = {}
    worker_attempts: dict[str, dict] = {}
    interrupted_workers: set[str] = set()
    blocked_launches: dict[str, str] = {}
    blocked_launch_keys: dict[tuple[str, str], str] = {}
    submission_nudges: set[tuple[str, str, str]] = set()
    manifest_attempts: dict[str, dict] = {}
    activity = {"worker_launches": 0, "integrations": 0, "review_launches": 0, "source_repairs": 0}
    checked_frontier_sha = state["formalization"].get("main_sha")
    # Persisted states can predate merge-time rejection reconciliation. Repair
    # that bookkeeping before deciding whether any work is dispatchable.
    state = _reconcile_for_scheduler(paths.forum)
    bump_representation.recover_representation_reviews(paths.forum)
    # A prior controller may have stopped between starting an attempt and
    # recording its end. Do not let restarts repeat that author/input forever.
    for repair in bump_state.current_manifest_repairs(state):
        for attempt in repair.get("attempts", []):
            if attempt.get("status") == "started":
                bump_state.finish_manifest_repair_attempt(
                    paths.forum, repair["repair_id"], attempt["author"], "interrupted",
                )
    # Target notifications may arrive while verification runs in another thread.
    # Consume them separately: refreshing assignments must not consume candidates.
    target_events_seen = {event["event_id"] for event in state.get("events", [])}
    worker_targets.update({
        agent_names[key]: target for key, target in state.get("worker_tasks", {}).items()
        if key in agent_names
    })

    for agent in roster.agents:
        tree = _formal_worktree(paths.project_root, agent.name)
        worktree.symlink_lake_cache(tree, paths.project_root)
        worktree.link_runtime_state(tree, paths.project_root)
        worktrees[agent.name] = tree

    def participating_strategy(current: dict, name: str, task_id: str = "") -> dict | None:
        matches = [
            strategy for strategy in current.get("strategies", {}).values()
            if strategy.get("phase") == "formalizing"
            and bump_state.strategy_is_current(current, strategy)
            and strategy.get("status") == "claimed"
            and bump_state.participates(strategy, name)
            and (not task_id or strategy.get("target") == task_id)
        ]
        return next((strategy for strategy in matches
                     if bump_state.author_key(strategy.get("owner")) == bump_state.author_key(name)),
                    matches[0] if matches else None)

    def active_target(name: str) -> str:
        # Keep the actual launch target through stop-job cleanup, even after
        # completion consumes the attempt snapshot or registration changes hints.
        return worker_revisions.get(name, (worker_targets.get(name, ""), 0))[0]

    def refresh_worker_targets(current: dict) -> None:
        for event in bump_state.events_after(current, target_events_seen):
            target_events_seen.add(event["event_id"])
            if event.get("kind") not in {"strategy_registered", "strategy_claimed", "strategy_assisted"}:
                continue
            strategy = current.get("strategies", {}).get(event.get("strategy_id"), {})
            author = agent_names.get(bump_state.author_key(event.get("author")))
            if (author and event.get("phase") == "formalizing"
                    and bump_state.strategy_is_current(current, strategy)):
                worker_targets[author] = event.get("target", "")
        for name in agents:
            # Registering an alternative is not abandoning an owned strategy.
            # Paused participation also pins workers while a candidate is queued.
            unresolved = bump_server.unresolved_formal_tasks(current, name)
            if unresolved and worker_targets.get(name) not in unresolved:
                strategy = participating_strategy(current, name)
                worker_targets[name] = strategy["target"] if strategy else unresolved[0]

    def request_stop(name: str, reason: str) -> None:
        running = tasks.get(name)
        if running is not None and not running.done() and name not in stopping:
            interrupted_workers.add(name)
            stopping[name] = asyncio.create_task(
                _cancel(agents[name], running, interrupts[name], reason, paths.project_root),
                name=f"bump:stop:{name}",
            )

    def manifest_repairs(current: dict) -> list[dict]:
        return _select_manifest_repairs(current, bump_state.current_manifest_repairs(current),
            {(row["repair_id"], row["input_sha256"]) for row in manifest_attempts.values()})

    def reconcile_manifest_workers(current: dict) -> None:
        repairs = manifest_repairs(current)
        for key in list(blocked_launches):
            if key.startswith("manifest:"):
                blocked_launches.pop(key)
        live = {(row["repair_id"], row["input_sha256"]) for row in repairs}
        for name, repair in manifest_attempts.items():
            if (repair["repair_id"], repair["input_sha256"]) not in live:
                request_stop(name, "manifest repair input changed; refresh current task evidence")
        for repair in repairs:
            if _manifest_repair_pending_candidate(current, repair):
                continue  # Exact queued work gets its normal integration first.
            scope = _manifest_repair_scope(current, repair)
            for name in list(tasks):
                if roles.get(name) != "formalizing" or active_target(name) not in scope:
                    continue
                owned = manifest_attempts.get(name, {})
                if (owned.get("repair_id"), owned.get("input_sha256")) != (
                        repair["repair_id"], repair["input_sha256"]):
                    request_stop(name, "focused manifest repair; preserving competing proof work")
            tried = {bump_state.author_key(row["author"]) for row in repair.get("attempts", [])}
            if (repair.get("status") == "open" and set(agent_names) <= tried
                    and not any(row.get("status") == "started" for row in repair.get("attempts", []))):
                bump_state.mark_manifest_repair_exhausted(paths.forum, repair["repair_id"])
                repair["status"] = "exhausted"
            if repair.get("status") == "exhausted":
                blocked_launches[f"manifest:{repair['task_id']}"] = "All configured authors attempted the current manifest repair."

    def launch_manifest_repair(current: dict, name: str, task_id: str) -> tuple[bool, dict | None]:
        relevant = [row for row in manifest_repairs(current)
                    if task_id in _manifest_repair_scope(current, row)]
        if not relevant:
            return True, None
        repair = relevant[0]
        if (repair["task_id"] != task_id or repair.get("status") != "open"
                or _manifest_repair_pending_candidate(current, repair)
                or any(bump_state.author_key(row["author"]) == bump_state.author_key(name)
                       for row in repair.get("attempts", []))):
            return False, None
        scope = _manifest_repair_scope(current, repair)
        if any(active_target(owner) in scope for owner in stopping):
            return False, None
        if any(roles.get(owner) == "formalizing" and not running.done() and active_target(owner) in scope
               for owner, running in tasks.items()):
            return False, None
        return True, repair

    def retire_completed_task(task_id: str) -> None:
        for name, running in list(tasks.items()):
            current = bump_state.load_state(paths.forum)
            refresh_worker_targets(current)
            if worker_targets.get(name) != task_id:
                continue
            if bump_server.unresolved_formal_tasks(current, name):
                continue
            if roles.get(name) == "formalizing":
                request_stop(name, f"formal task {task_id} completed")
        # Retain assignments, claims, and source. Obsolete completed-task work
        # is reset only when a stopped worker is assigned its next task.

    def worktree_changes(name: str) -> tuple[str, str]:
        status = _git(
            worktrees[name], "status", "--porcelain", "--untracked-files=all"
        ).stdout.strip()
        diff = _git(worktrees[name], "diff", "HEAD", "--binary").stdout
        return status, hashlib.sha256((status + "\n" + diff).encode()).hexdigest()

    def forget_blocked_launches(name: str) -> None:
        for pair in list(blocked_launch_keys):
            if pair[0] == name:
                blocked_launch_keys.pop(pair)

    def launch(name: str, task_id: str, followup: str = "", *, context_note: str = "") -> None:
        if integration is not None or name in stopping:
            return  # Worktree preparation takes merge.lock; never block this event loop on a review.
        if name in tasks:
            return
        current = bump_state.load_state(paths.forum)
        formal_task = current["formal_tasks"].get(task_id)
        if formal_task and formal_task.get("migration_module"):
            if formal_task.get("migration_attempts", 0) >= formal_task.get("migration_max_attempts", 5):
                blocked_launches[name] = f"{task_id}: persistent migration attempt budget exhausted"
                return
            if any(not running.done() and (
                    active_target(owner) == task_id or
                    current["formal_tasks"].get(active_target(owner), {}).get("lean_file") == formal_task.get("lean_file"))
                   for owner, running in tasks.items()):
                return
        allowed, repair = launch_manifest_repair(current, name, task_id)
        if not allowed:
            return
        eligible = (bump_state.repair_available_to(
            current, name, task_id, repair["repair_id"], repair["input_sha256"],
        ) if repair else bump_state.task_available_to(current, name, task_id))
        if (not formal_task or not eligible
                or bump_server.has_pending_formal_candidate(current, name)
                or bump_state.source_issues_blocking_task(current, task_id)):
            return
        # Prospective/global accounting is not a command to stop proof search.
        # Focus repair work only after a current candidate actually failed it.
        repair_blockers = [row for row in bump_server.verification_blockers(current, task_id)
                           if row.get("candidate_id") and row.get("prerequisite_id")]
        source_evidence_repair = ""
        if repair_blockers:
            if any(not running.done() and roles.get(owner) == "formalizing" and active_target(owner) == task_id
                   for owner, running in tasks.items()):
                return  # One focused repair, not another full proof swarm.
            source_evidence_repair = (
                "SOURCE EVIDENCE REPAIR: retain existing proof work. Resolve these exact prerequisite "
                "records, including those belonging to other completed tasks. Use refine_chunks and "
                "read their requirements; adding unrelated outputs or resubmitting unchanged bytes "
                "cannot fix a prerequisite record. If you cannot repair it, yield_task with the blocker.\n"
                + _blocker_recovery_context(repair_blockers)
            )
        pair = (name, task_id)
        retry_key = _formal_launch_retry_key(current, name, task_id, worker_targets.get(name, ""))
        if blocked_launch_keys.get(pair) == retry_key:
            return
        prepared = bump_server.prepare_formal_worktree(
            name, previous_task=worker_targets.get(name, ""), next_task=task_id,
            expected_revision=current["formalization"]["revision"],
            **({"repair_id": repair["repair_id"], "repair_input_sha256": repair["input_sha256"]}
               if repair else {}),
        )
        if not prepared["ok"]:
            # Preparation can publish a checkpoint before reporting a block;
            # do not mistake its own bookkeeping for a fresh retry trigger.
            blocked_launch_keys[pair] = _formal_launch_retry_key(
                bump_state.load_state(paths.forum), name, task_id, worker_targets.get(name, ""),
            )
            blocked_launches[name] = prepared.get("error", prepared.get("reason", "worktree unavailable"))
            _console.print(f"[yellow]preserving {name}'s worktree: {prepared.get('reason', '')}[/yellow]")
            return
        forget_blocked_launches(name)
        blocked_launches.pop(name, None)
        # Preparation may synchronize source while Forum events arrive. Capture
        # the actual attempt, not the mutable target inferred from later claims.
        current = bump_state.load_state(paths.forum)
        allowed, refreshed_repair = launch_manifest_repair(current, name, task_id)
        if not allowed or any((repair or {}).get(key) != (refreshed_repair or {}).get(key)
                              for key in ("repair_id", "input_sha256")):
            return  # Preparation can race with a corrected submission or refinement.
        repair = refreshed_repair
        eligible = (bump_state.repair_available_to(
            current, name, task_id, repair["repair_id"], repair["input_sha256"],
        ) if repair else bump_state.task_available_to(current, name, task_id))
        if not eligible:
            return
        if repair:
            started = bump_state.begin_manifest_repair_attempt(
                paths.forum, repair["repair_id"], name, input_sha256=repair["input_sha256"],
            )
            if started.get("status") != "started":
                return
            repair = started["repair"]
            manifest_attempts[name] = repair
        formal_task = current["formal_tasks"][task_id]
        if formal_task.get("migration_module"):
            formal_task = bump_state.begin_migration_attempt(paths.forum, task_id, name)
        worker_targets[name] = task_id
        worker_revisions[name] = (task_id, formal_task.get("revision", 0))
        worker_attempts[name] = bump_state.snapshot_attempt(current, name, task_id)
        worker_attempts[name]["candidate_ids"] = list(current.get("formal_candidates", {}))
        interrupted_workers.discard(name)
        agent = agents[name]
        brief = forum_brief(paths, "formalizing", name, task_id=task_id)
        system = _preamble(agent, roster, icrl_enabled=False)
        if brief:
            system += f"\n{PIPELINE.capitalize()} workspace brief (refresh with bump_brief):\n{brief}\n"
        system += base_prompt + "\n\n" + tools_prompt
        if context:
            system += "\n\n" + context
        strategy = participating_strategy(current, name, task_id)
        dirty, _ = worktree_changes(name)
        critic_feedback = bump_state.critic_feedback_for_task(current, task_id)
        direct_critic_repair = bool(critic_feedback["direct"])
        critic_repair = _critic_repair_prompt(paths, current, task_id, feedback=critic_feedback)
        # Checked prerequisite failures can belong to a different author's
        # candidate. Keep their mandatory repair instructions even when direct
        # semantic feedback suppresses the opportunistic followup prompt.
        recovery = "\n".join(part for part in (
            _rejection_recovery_prompt(current, name, task_id), source_evidence_repair,
        ) if part)
        recovering = bool(recovery) or direct_critic_repair
        resume = ""
        if strategy:
            resume += (
                f"Resume your currently claimed strategy `{strategy['strategy_id']}`. Do not "
                "register or claim a replacement unless you explicitly abandon this strategy. "
            )
        if dirty:
            resume += (
                "Your worktree contains uncommitted or untracked files. Inspect the source diff "
                "and any new Lean files before new research; scratch notes alone are not a candidate. "
                "If the target is complete, call `finalize_formalization` immediately. "
            )
        # Recovery suppresses the old "resume/finalize immediately" nudges, not
        # the worker's ownership, preserved edits or synchronization warnings.
        work_context = [context_note] if context_note else []
        if recovering and strategy:
            work_context.append(
                f"Your currently claimed strategy is `{strategy['strategy_id']}`. Preserve its work "
                "and reassess the approach against the repair guidance. Explicitly abandon it before "
                "claiming a replacement strategy."
            )
        if recovering and dirty:
            work_context.append(
                "Your worktree contains uncommitted or untracked files. Inspect and preserve the source "
                "diff and new Lean files before editing; scratch notes alone are not a candidate."
            )
        if prepared.get("sync_warning"):
            work_context.append(prepared["sync_warning"])
        strategy_instruction = (
            ("Reassess the claimed strategy against the repair guidance before continuing. "
             if recovering else "Continue the claimed strategy for this task. ") if strategy else
            "Claim a suitable existing unclaimed strategy, or register one only when your approach "
            "is materially different. You may investigate or edit before registering, but claim a "
            "strategy before finalizing. "
        )
        if formal_task.get("representation", {}).get("status") == "adopted":
            representation_instruction = (
                "This task's Lean representation is already adopted. Work on its remaining "
                "proof/construction and unfinished prerequisites; do not resubmit the unchanged "
                "representation. Submit `stage='complete'` when the implementation and required "
                "proofs are ready. If the adopted encoding is incorrect, use `refine_chunks` with "
                "`reopen_representations` before revising it. If concretely blocked and ending "
                "the attempt, call `yield_task` with the precise blocker. An `already_adopted` "
                "response queues no candidate or review interrupt: continue useful proof work "
                "or yield, rather than submitting the same representation again. "
            )
        else:
            representation_instruction = (
                "Choose Lean representations as needed. Submit explicit outputs with "
                "`finalize_formalization`; stage='representation' shares checked statements/definitions "
                "before proofs, while stage='complete' implements the whole node "
                "(and can adopt its outputs directly). "
            )
        normal_task_prompt = (
            f"Your current formalization target is task `{task_id}`: "
            f"{formal_task.get('description', '')}. Current adopted outputs: "
            f"{formal_task.get('outputs', [])}. Its formalization source references are "
            f"{formal_task.get('source_components', [])}. Statement prerequisites are available; "
            "proof-only dependencies may still be unfinished. "
            "Refresh bump_brief. " + strategy_instruction +
            "Edit in your worktree using MCP tools while iterating: prefer compatible Axle tools "
            "when enabled over equivalent Lean LSP tools, and Lean LSP for local goals and diagnostics. "
            "Use direct shell checks only as a fallback or when compiled artifacts are needed. "
            "Unity will commit the "
            "exact source and perform the sole authoritative full build in main. Publish useful Lean/API findings "
            "as you work. Supplied documents are read-only. Use report_source_issue for source defects, "
            "and submit_source_repair with evidence when you can repair the issue directly. "
            "Do not change the source or silently bump a different result. "
            "Use refine_chunks for explicit graph/interpretation revisions; use source repair for source defects."
        )
        if formal_task.get("migration_module"):
            representation_instruction = "Submit stage='complete' with the fixed original outputs only. "
            normal_task_prompt = (
                f"Migrate module {formal_task['migration_module']} in {formal_task['lean_file']}. "
                f"Fixed outputs: {formal_task.get('outputs', [])}. "
                f"Controller diagnostics: {formal_task.get('migration_diagnostics', {})}. "
                "Its project imports have passed target-version checks. Refresh bump_brief, claim a strategy, "
                "and repair compatibility in your assigned file only. Preserve every original declaration, "
                "definition meaning, assumptions and trust footprint. Do not edit versions, configuration, "
                "other modules, the original snapshot, or the obligation graph. Check local diagnostics, "
                "then finalize_formalization for controller verification and merge. Publish concrete blockers "
                "and yield if no justified repair is available. " + strategy_instruction
            )
            if (current["formalization"].get("contract") or {}).get("migration_policy") == 2:
                normal_task_prompt += (
                    f" Diagnostic generation: {formal_task.get('diagnostic_generation')}. "
                    "Use bump_task to read declaration subtasks and exact diagnostic artifacts. "
                    "Use refine_migration only to partition current diagnosed work or propose an explicit "
                    "correspondence; proposals do not establish semantic acceptance. Private incomplete "
                    "edits are preserved in this worktree. Submit only a complete module repair, never "
                    "a partial declaration patch, and do not add holes or expand trust."
                )
        task_prompt = _compose_formal_task_prompt(
            recovery=recovery, resume=resume, followup=followup, normal=normal_task_prompt,
            representation=representation_instruction, repair=repair,
            critic_repair=critic_repair, direct_critic_repair=direct_critic_repair,
            work_context="\n".join(work_context),
        )
        checkpoint = prepared.get("checkpoint") or prepared.get("parked_checkpoint")
        if checkpoint:
            task_prompt += (
                f"\nPreserved work for task `{checkpoint['task_id']}` is checkpointed at "
                f"`{checkpoint['ref']}` ({checkpoint['commit_sha']}). Read artifact "
                f"`{checkpoint['manifest_artifact']}` before reusing its source or ignored files. "
                "Reuse only work relevant to the current task."
            )
        event = asyncio.Event()
        interrupts[name] = event
        roles[name] = "formalizing"
        tasks[name] = asyncio.create_task(
            spawn(
                agent, system, task_prompt, worktrees[name], mcp,
                subagents=subagents, interrupt_event=event,
                log_context={
                    "command": PIPELINE, "run_id": current.get("run_id"), "phase": "formalizing",
                    "task_id": task_id, "role": "bumpr",
                },
                env_overrides=_agent_runtime_env(paths, current, name, task_id=task_id),
                own_process_group=True,
                mcp_profile="bump",
            ),
            name=f"{PIPELINE}:formalizing:{name}:{task_id}",
        )
        activity["worker_launches"] += 1

    def launch_idle() -> None:
        if integration is not None:
            return
        # An unrelated merge changes dispatch context, not the rejected
        # semantic input's retry budget. Refresh that context before scheduling
        # or concluding that the round has no runnable work.
        current = _reconcile_for_scheduler(paths.forum)
        if current.get("phase") != "formalizing" or bump_state.pending_replan(current):
            return
        refresh_worker_targets(current)
        reconcile_manifest_workers(current)
        from .bump_repairs import repair_attempt_limit, source_repair_turn
        # Review the newly adopted interface before its dependent proof work.
        # Use idle configured capacity and an independent author when available.
        for review in bump_representation.pending_representation_reviews(current):
            if review["input_sha256"] in {key for _, key in review_inputs.values()}:
                continue
            tried = bump_representation.attempted_reviewers(review)
            available = [name for name in agents if name not in tasks and name not in stopping
                         and bump_state.author_key(name) not in tried
                         and not bump_server.has_pending_formal_candidate(current, name)]
            available.sort(key=lambda name: bump_state.author_key(name)
                           == bump_state.author_key(review.get("representation_author")))
            if not available:
                continue
            name = available[0]
            review_inputs[name] = (review["task_id"], review["input_sha256"])
            roles[name] = "representation_review"
            interrupts[name] = asyncio.Event()
            tasks[name] = asyncio.create_task(bump_representation.representation_review_turn(
                agents[name], roster, paths, review["task_id"], interrupt_event=interrupts[name],
            ), name=f"bump:representation_review:{name}:{review['task_id']}")
            activity["review_launches"] += 1
        issues = bump_state.ready_source_issues(current)
        assigned_issues = set(repair_issues.values())
        for name in agents:
            if name in tasks or name in stopping or bump_server.has_pending_formal_candidate(current, name):
                continue
            issue = next((item for item in issues if item["issue_id"] not in assigned_issues
                          and name not in repair_exhausted.get(item["issue_id"], set())), None)
            if issue is None:
                continue
            issue_id = issue["issue_id"]
            assigned_issues.add(issue_id)
            repair_issues[name] = issue_id
            roles[name] = "source_repair"
            interrupts[name] = asyncio.Event()
            tasks[name] = asyncio.create_task(source_repair_turn(
                agents[name], roster, paths, issue_id, repair_attempt_limit(),
                interrupt_event=interrupts[name],
            ), name=f"bump:source_repair:{name}:{issue_id}")
            activity["source_repairs"] += 1
        # Repair work is a focused target, not another general proof assignment.
        # Trying it here lets a later eligible author take over after a yield;
        # central launch/worktree guards still preserve unresolved private work.
        for repair in manifest_repairs(current):
            if repair.get("status") != "open":
                continue
            # Resume the root's private proof work before moving a dependent's
            # checkpointed worktree back to its rejected prerequisite.
            owners = sorted(agents, key=lambda name: (
                worker_targets.get(name) != repair["task_id"],
                participating_strategy(current, name, repair["task_id"]) is None,
            ))
            for name in owners:
                if name in tasks or name in stopping:
                    continue
                launch(name, repair["task_id"])
                if name in tasks:
                    break
        ready = bump_state.ready_formal_tasks(current)
        if not ready:
            return
        idle = [name for name in agents if name not in tasks and name not in stopping]
        ready_ids = {formal_task["task_id"] for formal_task in ready}
        unassigned = []
        for name in idle:
            if bump_server.has_pending_formal_candidate(current, name):
                continue
            previous = worker_targets.get(name, "")
            successors = current.get("retired_tasks", {}).get(previous, {}).get("replaced_by", [])
            successor = next((key for key in successors if key in ready_ids), None)
            if successor:
                launch(name, successor, context_note="Your prior informal node was replaced. Read bump_task "
                       "for this successor and its lineage. Your worktree was preserved; reuse relevant "
                       "work, then claim a strategy for this node before finalizing.")
                continue
            # Unregistered edits are work too. Keep their target rather than
            # assigning the worker to a different ready task and resetting it.
            if current["formal_tasks"].get(previous, {}).get("status") == "pending":
                if previous in ready_ids and bump_state.task_available_to(current, name, previous):
                    launch(name, previous)
                    continue
                if not bump_server.unresolved_formal_tasks(current, name):
                    prerequisites = bump_server.ready_statement_prerequisites(current, previous)
                    if bump_state.has_yielded(current, name, previous):
                        # The worker may help its declared proof prerequisites,
                        # even though their statements were already available.
                        dependencies = current["formal_tasks"][previous].get("dependencies", [])
                        prerequisites = sorted(ready, key=lambda task: task["task_id"] not in dependencies)
                    active = [active_target(owner) for owner, running in tasks.items()
                              if not running.done() and roles.get(owner) == "formalizing"]
                    for _, prerequisite in _formal_task_assignments(
                        prerequisites, [name], active,
                        available_to=lambda owner, target: bump_state.task_available_to(current, owner, target),
                    ):
                        # prepare_formal_worktree rechecks under locks and permits
                        # this move only with no private edits/commits to discard.
                        launch(name, prerequisite)
                continue
            strategy = participating_strategy(current, name)
            if strategy and strategy.get("target") in ready_ids:
                if not previous:
                    # Existing claims/assistance on runtime entry are resumes,
                    # not a request to abandon unresolved work for a new task.
                    worker_targets[name] = strategy["target"]
                launch(name, strategy["target"])
                continue
            if bump_server.unresolved_formal_tasks(current, name):
                continue  # Includes assistants paused for somebody else's candidate.
            unassigned.append(name)
        active_targets = [
            active_target(name)
            for name, running in tasks.items()
            if not running.done() and roles.get(name) == "formalizing"
        ]
        for name, task_id in _formal_task_assignments(
            ready, unassigned, active_targets,
            available_to=lambda owner, target: bump_state.task_available_to(current, owner, target),
        ):
            launch(name, task_id)

    def candidate_workers(candidate: dict) -> list[str]:
        return [
            name for name, running in tasks.items()
            if not running.done() and roles.get(name) == "formalizing" and (
                active_target(name) == candidate["task_id"]
                or bump_state.author_key(name) == bump_state.author_key(candidate["author"])
            )
        ]

    try:
        launch_idle()
        while not stop_requested(paths.project_root):
            await asyncio.sleep(0.1)
            # Never carry parsed routing observations across scheduling ticks.
            observations = _SchedulerObservations(paths.forum)
            # Keep source I/O and hashing off the transport event loop. JSON's
            # C decoder can still hold the GIL; this is not a latency guarantee.
            # No routing/action proceeds until the complete source check passes.
            state = await asyncio.to_thread(_observe_scheduler_sources, paths, observations)
            refresh_worker_targets(state)
            if state.get("phase") == "formalizing":
                reconcile_manifest_workers(state)
            for name in list(tasks):
                if roles.get(name) == "representation_review":
                    target, input_sha256 = review_inputs[name]
                    latest = bump_representation.representation_review_input(state, target)
                    if not latest or latest["input_sha256"] != input_sha256:
                        request_stop(name, f"representation review input for {target} changed")
                    continue
                if roles.get(name) != "formalizing":
                    continue
                target, revision = worker_revisions.get(name, ("", 0))
                latest = state.get("formal_tasks", {}).get(target)
                if target and (latest is None or latest.get("revision", 0) != revision):
                    request_stop(name, f"informal task {target} was refined; refresh its current interpretation")
            replan = bump_state.pending_replan(state)
            if replan or state.get("phase") != "formalizing":
                if integration_cancel is not None:
                    integration_cancel.set()
                for name in list(tasks):
                    request_stop(name, "formalization replanning" if replan else "formalization phase changed")

            # Cancelling one worker never delays observation of another candidate.
            for name, stopper in list(stopping.items()):
                if stopper.done():
                    stopper.result()
                    stopping.pop(name)
                    forget_blocked_launches(name)
                    if name in tasks and not tasks[name].done():
                        raise ValueError(f"Worker {name} did not stop; source and pending candidates were preserved")

            for name, running in list(tasks.items()):
                if not running.done() or name in stopping:
                    continue
                result = None
                ended_normally = False
                try:
                    result = running.result()
                    ended_normally = True
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    _console.print(f"[red]worker {name} failed: {exc!r}[/red]")
                    if (bump_state.load_state(paths.forum)["formalization"].get("contract") or {}).get("migration_policy") in {1, 2}:
                        raise RuntimeError(f"Bump worker {name} failed; migration preserved") from exc
                tasks.pop(name, None)
                # A consumed worker/stop job may have changed private source.
                # Manual filesystem repairs require resuming the runtime or a
                # relevant state change; this is not a worktree watcher.
                forget_blocked_launches(name)
                interrupts.pop(name, None)
                role = roles.pop(name, "")
                review_inputs.pop(name, None)
                attempt = worker_attempts.pop(name, None)
                interrupted = name in interrupted_workers
                interrupted_workers.discard(name)
                repair = manifest_attempts.pop(name, None)
                if repair:
                    bump_state.finish_manifest_repair_attempt(
                        paths.forum, repair["repair_id"], name,
                        "interrupted" if interrupted else "yielded" if ended_normally else "failed",
                    )
                if role == "formalizing" and ended_normally and not interrupted and attempt:
                    current = bump_state.load_state(paths.forum)
                    submitted = any(
                        key not in attempt["candidate_ids"]
                        and candidate.get("task_id") == attempt["task_id"]
                        and bump_state.author_key(candidate.get("author")) == bump_state.author_key(name)
                        for key, candidate in current.get("formal_candidates", {}).items()
                    )
                    if not submitted and not bump_server.has_pending_formal_candidate(current, name):
                        bump_state.record_worker_yield(
                            paths.forum, name, attempt["task_id"],
                            "Worker ended without submitting a candidate or requesting further work.",
                            snapshot=attempt,
                        )
                if role == "source_repair":
                    issue_id = repair_issues.pop(name, "")
                    if isinstance(result, dict) and result.get("status") == "exhausted":
                        repair_exhausted.setdefault(issue_id, set()).add(name)
                        if repair_exhausted[issue_id] == set(agents):
                            bump_state.mark_source_issue_unresolved(
                                paths.forum, issue_id,
                                "Every configured agent exhausted its source-repair attempts",
                            )

            # Consume all submissions while a separate serial integration owns main.
            state = observations.observe()
            refresh_worker_targets(state)
            for candidate in state.get("formal_candidates", {}).values():
                if (candidate.get("status") in {"submitted", "merging"}
                        and bump_state.candidate_is_current(state, candidate)):
                    for name in candidate_workers(candidate):
                        request_stop(name, f"formal candidate {candidate['candidate_id']} submitted for {candidate['task_id']}")

            if integration is not None and integration.done():
                result = integration.result()
                finished = integration_candidate
                integration = None
                integration_candidate = {}
                integration_cancel = None
                if result.get("ok"):
                    retire_completed_task(finished["task_id"])
                elif not result.get("deferred"):
                    require_source_matches(paths, bump_state.load_state(paths.forum))
                    _console.print(f"[red]candidate {finished['candidate_id']} failed: {result.get('error', '')}[/red]")

            state = observations.observe()
            replan = bump_state.pending_replan(state)
            if replan:
                if integration is None and not tasks and not stopping:
                    state = observations.fresh()
                    replan = bump_state.pending_replan(state)
                    if not replan:
                        continue
                    assignments = {
                        name: {"task_id": task_id,
                               "task_revision": state.get("formal_tasks", {}).get(task_id, {}).get("revision"),
                               "worktree": str(worktrees[name])}
                        for name, task_id in worker_targets.items() if task_id
                    }
                    roots = replan.get("task_ids")
                    affected = _affected_tasks(state, roots)
                    assignments = checkpoint_replan_worktrees(paths, assignments, affected)
                    with _merge_lock(paths.project_root):
                        return bump_state.begin_replan(
                            paths.forum, replan["request_id"], assignments=assignments,
                        )
                continue
            if state.get("phase") != "formalizing":
                if integration is None and not tasks and not stopping:
                    state = observations.fresh()
                    if state.get("phase") != "formalizing":
                        return state
                continue

            if integration is None and any(
                item.get("status") == "submitted"
                for item in state.get("formal_candidates", {}).values()
            ):
                # Candidate admission is a full-state boundary, not a routing
                # cache decision. Integration revalidates again under its lock.
                state = observations.fresh()
                if bump_state.pending_replan(state) or state.get("phase") != "formalizing":
                    continue
                candidates = sorted(
                    (item for item in state.get("formal_candidates", {}).values()
                     if item.get("status") == "submitted"
                     and bump_state.candidate_is_current(state, item)),
                    key=lambda item: item.get("created_at", 0),
                )
                for candidate in candidates:
                    if candidate_workers(candidate):
                        continue
                    # An old worker's stop job can still be reaping owner jobs.
                    if any(active_target(name) == candidate["task_id"]
                           or bump_state.author_key(name) == bump_state.author_key(candidate["author"])
                           for name in stopping):
                        continue
                    started = bump_state.begin_formal_merge(paths.forum, candidate["candidate_id"])
                    if started.get("idempotent") or started.get("conflict"):
                        continue
                    integration_candidate = started["candidate"]
                    integration_cancel = Event()
                    _console.print(f"[cyan]mechanically reviewing {candidate['candidate_id']} for {candidate['task_id']}[/cyan]")
                    integration = asyncio.create_task(asyncio.to_thread(
                        _integrate_and_record, paths, integration_candidate,
                        state["formal_tasks"][candidate["task_id"]], integration_cancel,
                    ), name=f"bump:integration:{candidate['candidate_id']}")
                    activity["integrations"] += 1
                    break

            if integration is None:
                # These source checks/preparations must never wait on a build's merge lock.
                current = bump_state.load_state(paths.forum)
                if ((current["formalization"].get("contract") or {}).get("migration_policy") in {1, 2}
                        and (current["formalization"].get("main_sha") != checked_frontier_sha
                             or not _migration_frontier_is_current(current)
                             or current.get("migration_refresh_required")
                             or any(row.get("status") == "proposed" for row in
                                    current.get("migration_mapping_proposals", {}).values()))):
                    from .bump_bootstrap import check_ready_modules
                    await asyncio.to_thread(check_ready_modules, paths)
                    refreshed = bump_state.load_state(paths.forum)
                    if not _migration_frontier_is_current(refreshed):
                        # Concurrent Forum activity can reject publication. Retry
                        # on the next fresh pass; never return a false blocked
                        # round or dispatch from stale diagnostics.
                        continue
                    checked_frontier_sha = refreshed["formalization"].get("main_sha")
                for name in agents:
                    if name in tasks or name in stopping:
                        continue
                    current = bump_state.load_state(paths.forum)
                    task_id = worker_targets.get(name, "")
                    if (not bump_state.task_available_to(current, name, task_id)
                            or bump_server.has_pending_formal_candidate(current, name)):
                        continue
                    dirty, source_digest = worktree_changes(name)
                    strategy = participating_strategy(current, name, task_id)
                    nudge_key = (name, task_id, source_digest)
                    if dirty and strategy and nudge_key not in submission_nudges:
                        submission_nudges.add(nudge_key)
                        launch(name, task_id,
                               "Submission check only: inspect the existing worktree diff before new research. "
                               "Finalize a completed target, or publish a precise blocker and continue the "
                               "claimed strategy. Do not register a replacement or repeat unchanged searches.")
                launch_idle()

            state = bump_state.load_state(paths.forum)
            if (integration is None and not tasks and not stopping
                    and bump_state.all_formal_tasks_complete(state)):
                if bump_state.open_source_issues(state):
                    raise ValueError("Formal declarations are complete, but source issues remain unresolved; "
                                     "critic acceptance is blocked until a repair is adopted")
                return bump_state.record_round_end(
                    paths.forum, blocked_launches=blocked_launches, activity=activity,
                )
            if (integration is None and not tasks and not stopping
                    and not bump_server.has_pending_formal_candidate(state)):
                bump_state.record_round_end(paths.forum, blocked_launches=blocked_launches, activity=activity)
                return bump_state.load_state(paths.forum)
        return bump_state.load_state(paths.forum)
    finally:
        for name in list(tasks):
            request_stop(name, "formalization runtime ending")
        # Cancelling an asyncio wrapper would leave its thread mutating main.
        # Signal cooperative subprocess cancellation, then drain its rollback.
        interrupted_during_drain = False
        if integration is not None:
            if integration_cancel is not None:
                integration_cancel.set()
            while not integration.done():
                try:
                    await asyncio.shield(integration)
                except asyncio.CancelledError:
                    interrupted_during_drain = True
                except Exception:
                    break  # Retrieve/report below; still finish worker cleanup.
            try:
                integration.result()
            except Exception as exc:
                _console.print(f"[red]integration ended with an error; worktrees preserved: {exc!r}[/red]")
        if stopping:
            await asyncio.gather(*stopping.values(), return_exceptions=True)
        for name, repair in manifest_attempts.items():
            bump_state.finish_manifest_repair_attempt(paths.forum, repair["repair_id"], name, "interrupted")
        await asyncio.to_thread(bump_jobs.terminate, paths.project_root)
        final_state = bump_state.load_state(paths.forum)
        for agent in roster.agents:
            running = tasks.get(agent.name)
            if running is not None and not running.done():
                _console.print(f"[red]worker {agent.name} has not stopped; preserving its worktree[/red]")
                continue
            if (not bump_state.all_formal_tasks_complete(final_state)
                    or final_state.get("phase") == "chunking"
                    or bump_state.pending_replan(final_state)
                    or bump_state.open_source_issues(final_state)):
                continue
            bump_state.release_author_claims(
                paths.forum, agent.name, "formalization runtime ended",
            )
            tree = worktrees.get(agent.name)
            if tree is not None:
                worktree.cleanup_worktree(agent.name, tree, paths.project_root)
        if interrupted_during_drain:
            raise asyncio.CancelledError


def _affected_tasks(state: dict, requested: list[str] | None) -> set[str]:
    tasks = state.get("formal_tasks", {})
    affected = set(tasks) if not requested else set(requested)
    while True:
        expanded = affected | {task_id for task_id, task in tasks.items()
                               if affected.intersection(task.get("dependencies", []))}
        if expanded == affected:
            return affected
        affected = expanded


def checkpoint_replan_worktrees(paths, assignments: dict, affected: set[str]) -> dict:
    """Preserve known obsolete work by commit/ref before a controller may refresh it."""
    from copy import deepcopy
    import uuid

    saved = deepcopy(assignments)
    with _merge_lock(paths.project_root):
        for author, assignment in saved.items():
            if assignment.get("task_id") not in affected:
                continue
            with bump_server._finalization_lock(author):
                tree = _formal_worktree(paths.project_root, author)
                if assignment.get("worktree") != str(tree):
                    raise ValueError(f"Unknown worktree ownership for {author}; source preserved")
                status = _git(tree, "status", "--porcelain")
                if status.returncode:
                    raise ValueError(f"Cannot checkpoint {author}'s worktree; source preserved")
                if status.stdout.strip():
                    added = _git(tree, "add", "-A")
                    if added.returncode:
                        raise ValueError(added.stderr or "could not stage replan checkpoint")
                    committed = _git(tree, "commit", "-m", "UNITY: preserve obsolete bump work before replanning")
                    if committed.returncode:
                        raise ValueError(committed.stderr or "could not checkpoint replan work")
                head = _git(tree, "rev-parse", "HEAD").stdout.strip()
                safe_author = re.sub(r"[^a-zA-Z0-9_-]", "_", author)
                reference = f"refs/unity/bump-checkpoints/{safe_author}/{uuid.uuid4().hex}"
                saved_ref = _git(tree, "update-ref", reference, head)
                if saved_ref.returncode:
                    raise ValueError(saved_ref.stderr or "could not retain replan checkpoint")
                assignment["checkpoint"] = {"ref": reference, "commit_sha": head}
    return saved


def refresh_replanned_worktrees(paths, assignments: dict, state: dict) -> dict:
    """Refresh only stopped, explicitly known assignments whose task revision changed."""
    affected = {
        assignment["task_id"] for assignment in assignments.values()
        if assignment.get("task_id") not in state.get("formal_tasks", {})
        or assignment.get("task_revision") != state["formal_tasks"][assignment["task_id"]].get("revision")
    }
    saved = checkpoint_replan_worktrees(paths, assignments, affected)
    with _merge_lock(paths.project_root):
        for author, assignment in saved.items():
            if assignment.get("task_id") not in affected:
                continue
            with bump_server._finalization_lock(author):
                current = bump_state.load_state(paths.forum)
                if bump_server.has_pending_formal_candidate(current, author):
                    raise ValueError(f"Candidate review still protects {author}'s branch; source preserved")
                if worktree.main_commit(paths.project_root) != current["formalization"]["main_sha"]:
                    raise ValueError("Main changed before replanned worktree refresh; source preserved")
                result = worktree.force_sync_from_main(paths.project_root, author)
                if not result.get("ok"):
                    raise ValueError(result.get("error") or "replanned worktree refresh failed")
    return saved
