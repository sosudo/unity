"""Prepare a target migration and seed the copied workflow with declaration tasks."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
import uuid

from . import artifacts, bump_contract, bump_diagnostics, bump_inventory, bump_planner
from . import bump_preparation, bump_project, bump_state, bump_worktree
from .bump_input import bump_paths, require_source_matches, scope_bytes, snapshot_sources
from .config import Paths

parse_dependency_pins = bump_preparation.parse_dependency_pins


def _json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    artifacts._atomic_write(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


def active_path(paths: Paths) -> Path:
    return paths.unity / "bump" / "active.json"


def _event(paths: Paths, event: str, **details) -> None:
    from datetime import datetime, timezone
    path = paths.logs / "bump-preparation.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **details}) + "\n")


def _runtime_inputs(source: Paths, target: Path) -> tuple[Paths, dict]:
    paths = bump_paths(Paths.from_unity_dir(target / ".unity"))
    for directory in (paths.forum, paths.logs, paths.artifacts, paths.unity / "source"):
        directory.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for before, after, required in ((source.unity_md, paths.unity_md, True),
                                    (source.agents_yaml, paths.agents_yaml, True),
                                    (source.env, paths.env, False)):
        if not before.is_file() and not required:
            continue
        if before.is_symlink() or not before.is_file() or after.exists() or after.is_symlink():
            raise ValueError("invalid or already existing Bump runtime input: " + before.name)
        data = before.read_bytes()
        after.write_bytes(data)
        after.chmod(0o600)
        hashes[before.name] = hashlib.sha256(data).hexdigest()
    return paths, hashes


def freeze_source_bundle(paths: Paths, migration: dict, index: dict) -> dict:
    root = paths.unity / "source"
    if any(root.iterdir()):
        raise ValueError("Bump original source bundle already exists")
    original = Path(migration["original_root"])
    for module, relative in migration["selected_modules"].items():
        data = (original / relative).read_bytes()
        if hashlib.sha256(data).hexdigest() != migration["original_files"][relative]:
            raise ValueError("original source changed while freezing migration input: " + module)
        destination = root / "project" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    _json(root / "original-index.json", index)
    _json(root / "transition.json", {"original_commit": migration["original_commit"],
        "target_version": migration["target_version"], "dependency_pins": migration["dependency_pins"],
        "scope": migration["scope"],
        "assumption": "The pinned upgraded imports are compatible; recursive upstream structural equivalence is not asserted.",
        "acceptance": "Preserve every selected original declaration and its trust; require the complete selected build and independent review."})
    return {**snapshot_sources(paths), "migration": True}


def _prepare_plan(paths: Paths, migration: dict, index: dict, source: dict, *, state: dict,
                  build: dict | None = None) -> tuple[dict, dict, dict]:
    if build is None:
        diagnostics = bump_diagnostics.collect_build_diagnostics(paths.project_root, index,
            artifact_dir=paths.artifacts, original_root=Path(migration["original_root"]), scope=migration["scope"])
    else:
        output = build.get("output", build.get("stdout", "") + "\n" + build.get("stderr", ""))
        diagnostics = bump_diagnostics.diagnostics_from_output(paths.project_root, index, output,
            build["returncode"], original_root=Path(migration["original_root"]),
            build_dir=migration["scope"]["build_dir"])
        log = artifacts.store_text(paths.artifacts, output, kind="bump_build_diagnostics", producer="Unity")
        diagnostics["log_ref"] = {"artifact_id": log["artifact_id"], "sha256": log["sha256"]}
    # Missing downstream diagnostics do not prove that a declaration was
    # repaired when Lake could not build its current import prerequisites.
    diagnostics.update(bump_diagnostics.scheduling_imports(paths.project_root, index))
    main_sha = bump_worktree.main_commit(paths.project_root)
    diagnostics["main_sha"] = main_sha
    _json(paths.unity / "bump" / "diagnostics.json", diagnostics)
    dag = bump_planner.plan_repairs(index, diagnostics, source, state)
    contract = bump_contract.prepare_source_contract(paths, dag, state=state, main_sha=main_sha)
    old = state.get("formalization", {}).get("contract") or {}
    # Source obligations and the original index remain immutable. Successful
    # candidate evidence survives a refresh of compiler-discovered work.
    for key in ("bindings", "targets", "external_declarations", "prerequisite_declarations", "adopted_outputs"):
        if key in old:
            contract[key] = deepcopy(old[key])
    contract.pop("representation_review_policy", None)
    contract["sha256"] = bump_contract.digest({k: v for k, v in contract.items() if k not in {"sha256", "artifact_id"}})
    return diagnostics, dag, contract


def prepare(paths: Paths, version: str, dependency_pins: dict[str, str], *,
            project_scope: str = "build", architect: str = "auto") -> Paths:
    if active_path(paths).exists() or active_path(paths).is_symlink():
        raise ValueError("an existing Bump workspace is preserved; use --continue for a ready migration")
    if not paths.unity_md.is_file() or not paths.agents_yaml.is_file():
        raise ValueError("initialize the existing project before Bump")
    run_id = "bump-" + uuid.uuid4().hex[:12]
    migration = bump_preparation.prepare(paths.project_root, version, dependency_pins,
                                          run_id=run_id, project_scope=project_scope)
    original, target = Path(migration["original_root"]), Path(migration["target_root"])
    target_paths, config = _runtime_inputs(paths, target)
    pointer = {"version": 1, "run_id": run_id, "project_root": str(paths.project_root.resolve()),
               "source_root": str(paths.project_root.resolve()), "target_path": str(target), "status": "preparing"}
    _json(active_path(paths), pointer)
    _json(target_paths.unity / "bump-origin.json", {key: pointer[key] for key in ("project_root", "run_id", "target_path")})
    _event(target_paths, "original_build_started")
    started = time.monotonic()
    original_build = bump_preparation.build(original, task_id="original-build")
    log = artifacts.store_text(target_paths.artifacts, original_build.stdout + "\n" + original_build.stderr,
                               kind="bump_original_build", producer="Unity")
    if original_build.returncode:
        raise ValueError("original project build failed; build log artifact: " + log["artifact_id"])
    _event(target_paths, "original_build_finished", elapsed_seconds=time.monotonic() - started)
    migration["scope"] = bump_preparation.capture_scope(original, migration)
    migration["selected_modules"] = migration["scope"]["selected_modules"]
    migration["excluded_files"] = migration["scope"]["excluded_files"]
    if project_scope == "all":
        selected_build = bump_preparation.build(original, task_id="original-selected-build",
                                                  modules=sorted(migration["selected_modules"]))
        selected_log = artifacts.store_text(target_paths.artifacts, selected_build.stdout + "\n" + selected_build.stderr,
                                           kind="bump_original_selected_build", producer="Unity")
        if selected_build.returncode:
            raise ValueError("selected original source build failed; build log artifact: " + selected_log["artifact_id"])
    _event(target_paths, "original_index_started", module_count=len(migration["selected_modules"]))
    started = time.monotonic()
    indexed = bump_inventory.capture_original_index(original, migration["scope"], artifact_dir=target_paths.artifacts)
    index = indexed["index"]
    migration["original_environment"] = index["environment"]
    migration["index_ref"] = indexed["index_ref"]
    _event(target_paths, "original_index_finished", module_count=indexed["module_count"],
           occurrence_count=indexed["occurrence_count"], elapsed_seconds=time.monotonic() - started)
    _event(target_paths, "target_dependencies_started")
    migration["target_manifest"] = bump_preparation.resolve_dependencies(target, migration)
    migration["architect_mode"] = architect
    migration["architect"] = bump_preparation.optional_architect(target, mode=architect,
        version=migration["target_version"], build_dir=migration["scope"]["build_dir"])
    migration["target_config"] = bump_preparation.config_hashes(target)
    migration["target_environment"] = bump_contract.environment_identity(target)
    _event(target_paths, "target_dependencies_finished", architect=migration["architect"]["status"])
    bump_preparation.git(target, "switch", "-c", "unity/" + run_id)
    bump_preparation.git(target, "add", "--", *[name for name in bump_preparation._CONFIG if (target / name).is_file()])
    bump_preparation.git(target, "-c", "user.name=Unity", "-c", "user.email=unity@localhost",
                         "commit", "--allow-empty", "-m", "UNITY: pin Bump target environment")
    _json(target_paths.unity / "bump" / "migration.json", migration)
    baseline = bump_project.capture_migration_baseline(target, migration=migration, original_index=index)
    source = freeze_source_bundle(target_paths, migration, index)
    main_sha = bump_worktree.main_commit(target)
    state = bump_state.initialize_source(target_paths.forum, hashlib.sha256(scope_bytes(target_paths)).hexdigest(),
                                         main_sha, source, reset=True, project_baseline=baseline)
    _event(target_paths, "target_build_started")
    started = time.monotonic()
    diagnostics, dag, contract = _prepare_plan(target_paths, migration, index, source, state=state)
    _event(target_paths, "target_build_finished", elapsed_seconds=time.monotonic() - started,
           passed=diagnostics["passed"], declaration_tasks=len(dag["chunks"]))
    bump_state.initialize_migration_plan(target_paths.forum, dag, main_sha=main_sha, contract=contract)
    _json(target_paths.forum / "dag.json", dag)
    _json(active_path(paths), {**pointer, "status": "ready", "runtime_hashes": config,
                             "baseline_sha256": baseline["sha256"]})
    _event(target_paths, "repair_plan_ready", declaration_tasks=len(dag["chunks"]),
           original_occurrences=len(index["occurrences"]))
    return target_paths


def resume(paths: Paths, version: str | None = None, dependency_pins: dict | None = None,
           *, project_scope: str | None = None, architect: str | None = None) -> Paths:
    pointer = json.loads(active_path(paths).read_text())
    source = paths.project_root.resolve()
    if not isinstance(pointer.get("run_id"), str) or not re.fullmatch(r"bump-[0-9a-f]{12}", pointer["run_id"]):
        raise ValueError("saved Bump workspace identifier is invalid")
    target = source / ".unity" / "bump" / pointer["run_id"] / "target"
    if (pointer.get("version") != 1 or pointer.get("status") != "ready"
            or pointer.get("project_root") != str(source) or pointer.get("target_path") != str(target)):
        raise ValueError("saved Bump workspace is not a ready owned declaration migration")
    if target.is_symlink() or target.resolve() != target:
        raise ValueError("saved Bump target is not its exact owned path")
    target_paths = bump_paths(Paths.from_unity_dir(target / ".unity"))
    origin = json.loads((target_paths.unity / "bump-origin.json").read_text())
    if origin != {key: pointer[key] for key in ("project_root", "run_id", "target_path")}:
        raise ValueError("saved Bump target origin disagrees with its source pointer")
    migration = json.loads((target_paths.unity / "bump" / "migration.json").read_text())
    requested = version if not version or ":" in version else "leanprover/lean4:" + version
    if requested and requested != migration["target_version"]:
        raise ValueError("--continue cannot change the saved Lean version")
    if dependency_pins and dependency_pins != migration["dependency_pins"]:
        raise ValueError("--continue cannot change dependency pins")
    if project_scope and project_scope != migration["scope"]["mode"]:
        raise ValueError("--continue cannot change the saved scope")
    if architect and architect != migration.get("architect_mode"):
        raise ValueError("--continue cannot change the saved Architect policy")
    for name, sha in pointer["runtime_hashes"].items():
        if any(hashlib.sha256((root / name).read_bytes()).hexdigest() != sha for root in (paths.unity, target_paths.unity)):
            raise ValueError("Bump runtime configuration changed: " + name)
    state = bump_state.load_state(target_paths.forum)
    if state.get("project_baseline", {}).get("sha256") != pointer["baseline_sha256"]:
        raise ValueError("saved Bump baseline does not match the workspace")
    if not bump_contract._baseline_matches(state, state.get("formalization", {}).get("contract") or {}):
        raise ValueError("saved Bump contract does not match the original migration baseline")
    bump_project.require_pinned_inputs(target, state["project_baseline"])
    bump_project._require_clean(target)
    if bump_worktree.main_commit(target) != state["formalization"]["main_sha"]:
        raise ValueError("target HEAD changed outside the Bump controller")
    require_source_matches(target_paths, state)
    return target_paths


def refresh_diagnostics(paths: Paths, state: dict | None = None, *, build: dict | None = None) -> dict:
    state = bump_state.load_state(paths.forum) if state is None else state
    baseline = state["project_baseline"]
    migration = baseline["migration"]
    index = migration["original_index"]
    diagnostics, dag, contract = _prepare_plan(paths, migration, index, state["input_source"], state=state, build=build)
    return {"index": index, "diagnostics": diagnostics, "dag": dag, "contract": contract}


def refresh(paths: Paths, *, build: dict | None = None) -> dict:
    prepared = refresh_diagnostics(paths, build=build)
    state = bump_state.refresh_migration_plan(paths.forum, prepared["dag"],
        main_sha=bump_worktree.main_commit(paths.project_root), contract=prepared["contract"])
    _json(paths.forum / "dag.json", prepared["dag"])
    return state
