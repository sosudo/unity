"""Controller-only migration setup for the directly ported Formalize runtime.

Original native modules replace paper chunking. This module installs the same
source-linked plan/state/contract consumed by Bump's copied persistent scheduler;
it is not another scheduler and never dispatches a model.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid
from datetime import datetime, timezone

from . import artifacts, bump_contract, bump_json, bump_migration_contract, bump_migration_project
from . import bump_project, bump_state, bump_worktree
from .bump_input import bump_paths, require_source_matches, scope_bytes, snapshot_sources
from .config import Paths


def _sha(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Bump configuration must be a regular file: {path.name}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path, value: dict) -> None:
    # Native meaning trees are deep: pretty indentation can dwarf their data.
    # Preserve the exact JSON value while avoiding a second encoded byte copy.
    bump_json.atomic_dump(path, value)


def _preparation_event(paths: Paths, run_id: str, event: str, **fields) -> None:
    """Append bounded controller progress, never acceptance or a reuse cache.

    Callers pass only module IDs, counts, hashes, elapsed times and exception
    class names. Raw compiler/provider diagnostics and runtime configuration do
    not enter this observer-facing file.
    """
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
              "event": event, **fields}
    filename = paths.unity / "bump-preparation.jsonl"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(filename, flags, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as output:
        output.write(json.dumps(record, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())


def active_path(paths: Paths) -> Path:
    return paths.unity / "bump" / "active.json"


def parse_dependency_pins(values: tuple[str, ...] | list[str]) -> dict[str, str]:
    pins = {}
    for value in values:
        name, separator, revision = value.partition("=")
        if not separator or not name or not revision or name != name.strip() or revision != revision.strip():
            raise ValueError("--dependency requires NAME=EXACT_VERSION_OR_COMMIT")
        if name in pins:
            raise ValueError(f"Duplicate dependency pin: {name}")
        pins[name] = revision
    return pins


def _copy_runtime_inputs(source: Paths, target: Path) -> tuple[Paths, dict]:
    paths = bump_paths(Paths.from_unity_dir(target / ".unity"))
    paths.unity.mkdir(parents=True, exist_ok=True)
    for directory in (paths.forum, paths.logs, paths.artifacts, paths.unity / "source"):
        directory.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for original, destination, required in (
            (source.unity_md, paths.unity_md, True),
            (source.agents_yaml, paths.agents_yaml, True),
            (source.env, paths.env, False)):
        if not original.exists() and not required:
            continue
        hashes[original.name] = _sha(original)
        if destination.exists() or destination.is_symlink():
            raise ValueError("Private Bump runtime configuration already exists; refusing overwrite")
        shutil.copyfile(original, destination)
        destination.chmod(0o600)
    return paths, hashes


def freeze_source_bundle(paths: Paths, migration: dict, graph: dict, reports: dict) -> dict:
    """Retain original bytes and native receipts; never ask agents for a paper."""
    original = Path(migration["original_path"])
    source_root = paths.unity / "source"
    if any(source_root.iterdir()):
        raise ValueError("Bump input bundle already exists; refusing to replace original evidence")
    transition = {"version": 2, "migration_occurrence_policy": 1,
                  "original_commit": migration["source_commit"],
                  "original_source_hash": migration["source_hash"],
                  "target_version": migration["target_version"],
                  "dependency_pins": migration["dependency_pins"],
                  "verification_scope": migration["scope"],
                  "scope_note": "Only the sealed module scope receives native migration checks; excluded files remain byte-preserved, not newly kernel-verified.",
                  "occurrence_note": "Each declaration in each original raw module artifact is a distinct obligation, including same-named generated declarations; no other module's evidence discharges its meaning or trust check.",
                  "migration_identity": migration["identity"]}
    _json(source_root / "transition.json", transition)
    for module, entry in sorted(graph.items()):
        destination = source_root / "project" / entry["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = (original / entry["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != migration["source_files"][entry["path"]]:
            raise ValueError(f"Original module changed before input freezing: {module}")
        destination.write_bytes(data)
        _json(source_root / "native" / (module + ".json"), reports[module])
    return snapshot_sources(paths)


def migration_dag(graph: dict, reports: dict, source: dict) -> dict:
    """One source-bound obligation per compiler module, including empty modules."""
    if not graph or set(graph) != set(reports):
        raise ValueError("Original compiler graph and native inventory must cover the same modules")
    refs = {row["ref_id"] for row in source["source_refs"]}
    chunks, requirements, anchors, arguments = [], [], [], []
    transition_ref = "source:transition.json"
    if transition_ref not in refs:
        raise ValueError("Migration source is missing its requested transition")
    anchors.append({"id": "transition", "source_ref": transition_ref,
                    "location": "Exact controller-requested version and dependency transition",
                    "excerpt": "Preserve the original project meaning and per-declaration trusted assumptions under the pinned target environment."})
    accounted = {transition_ref}
    for module, entry in sorted(graph.items()):
        if entry.get("compiler_derived") is not True:
            raise ValueError("Migration graph must come from the original Lean compiler")
        if set(entry["imports"]) - set(graph) or module in entry["imports"]:
            raise ValueError(f"Invalid original compiler dependency graph for {module}")
        report = reports[module]
        if report.get("module") != module or not report.get("verified") or not report.get("complete_inventory"):
            raise ValueError(f"Original module lacks a complete native inventory: {module}")
        native_ref = "source:native/" + module + ".json"
        code_ref = "source:project/" + entry["path"]
        sources = [code_ref, native_ref, transition_ref]
        if set(sources) - refs:
            raise ValueError(f"Missing frozen native/source input for {module}")
        accounted.update(sources)
        native_anchor, code_anchor = module + ":native", module + ":source"
        ids = [native_anchor, code_anchor, "transition"]
        declarations = sorted(report.get("declarations", {}))
        statement = (f"Migrate module {module} at {entry['path']} to the requested pinned environment; "
                     "preserve every original declaration occurrence's meaning, definition behavior, and trusted assumptions independently, "
                     "and compile the complete module/import closure.")
        anchors.extend([
            {"id": native_anchor, "source_ref": native_ref,
             "location": f"Native inventory for {module}; evidence {report.get('evidence_sha256', '')}",
             "excerpt": "Original declarations: " + (", ".join(declarations) or "none; import-only module remains in build scope")},
            {"id": code_anchor, "source_ref": code_ref,
             "location": "Entire original Lean module", "excerpt": statement},
        ])
        requirement = "preserve:" + module
        requirements.append({"id": requirement, "statement": statement, "source_components": sources,
                             "tasks": [module], "anchor_ids": ids})
        chunks.append({"id": module, "title": "Migrate " + module, "predicted_kind": "module",
                       "informal_statement": statement,
                       "informal_proof": "Use the frozen original native declarations and source as the preservation specification; repair target compiler failures without weakening that specification.",
                       "statement_dependencies": sorted(entry["imports"]), "proof_dependencies": [],
                       "source_components": sources, "anchor_ids": ids, "requirement_ids": [requirement],
                       "proposed_formal_statement": None, "proposed_formal_strategy": None})
        arguments.append({"requirement_id": requirement, "anchor_ids": ids,
                          "outline": "Check old and target compiled contexts independently, then compare native semantic and trust closures. A passing build alone does not establish preservation.",
                          "prerequisites": [], "repair_ids": []})
    if accounted != refs:
        raise ValueError("Every frozen original input must be accounted for in the migration plan")
    return {"solution_candidate": source["candidate_id"], "solution_sha256": source["sha256"],
            "chunks": chunks, "requirements": requirements,
            "spec": {"version": 1, "anchors": anchors,
                     "scope": {"targets": [row["id"] for row in anchors], "references": [], "excluded": []},
                     "prerequisites": [], "arguments": arguments}}


def freeze_source_bundle_v2(paths: Paths, migration: dict, index: dict) -> dict:
    """Freeze original source and a slim inventory, never recursive meanings."""
    original = Path(migration["original_path"])
    source_root = paths.unity / "source"
    if any(source_root.iterdir()):
        raise ValueError("Bump input bundle already exists; refusing to replace original evidence")
    _json(source_root / "transition.json", {
        "version": 3, "migration_policy": 2, "migration_occurrence_policy": 1,
        "original_commit": migration["source_commit"], "original_source_hash": migration["source_hash"],
        "target_version": migration["target_version"], "dependency_pins": migration["dependency_pins"],
        "auxiliary_dependencies": migration.get("auxiliary_dependencies", {}),
        "verification_scope": migration["scope"], "migration_identity": migration["identity"],
        "assumption": "Pinned upgraded imports are compatible; recursive upstream structural equality is not claimed.",
        "acceptance": "Complete selected builds, original occurrence coverage, no-new-trust, explicit correspondence and independent semantic review.",
        "excluded": "Excluded files are byte-preserved, not newly compiled or semantically verified.",
    })
    _json(source_root / "original-index.json", index)
    for module, entry in sorted(index["modules"].items()):
        relative = entry["path"]
        data = (original / relative).read_bytes()
        if hashlib.sha256(data).hexdigest() != migration["source_files"][relative]:
            raise ValueError(f"Original source changed during input freezing: {module}")
        destination = source_root / "project" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    return snapshot_sources(paths)


def prepare(paths: Paths, version: str, dependency_pins: dict[str, str], *, project_scope: str = "build",
            architect: str = "auto") -> Paths:
    """Index the working original, build the bumped target, then plan failures.

    There is no pre-worker semantic-closure sweep. Diagnostic planning does not
    certify a migration: the candidate and final native/critic gates still run.
    """
    from . import bump_architect, bump_diagnostics, bump_inventory, bump_planner

    pointer = active_path(paths)
    if pointer.exists() or pointer.is_symlink():
        raise ValueError("A Bump attempt already exists; no work was overwritten. Only a compatible v2 run can continue.")
    if not version:
        raise ValueError("Fresh Bump requires an exact Lean version")
    if architect not in {"auto", "off"}:
        raise ValueError("Bump LeanArchitect mode must be auto or off")
    # Validate required private runtime inputs before creating any worktrees.
    _sha(paths.unity_md)
    _sha(paths.agents_yaml)
    run_id = "bump-" + uuid.uuid4().hex[:12]
    migration = bump_migration_project.prepare(paths.project_root, version, dependency_pins,
                                              run_id=run_id, project_scope=project_scope)
    original, target = bump_migration_project.resolve_paths(paths.project_root, migration)
    record = {"version": 2, "migration_policy": 2, "run_id": run_id, "status": "preparing", "project_root": str(paths.project_root.resolve()),
              "target_path": str(target), "migration": migration}
    _json(pointer, record)
    target_paths, runtime_hashes = _copy_runtime_inputs(paths, target)
    _json(target_paths.unity / "bump-origin.json", {"project_root": str(paths.project_root.resolve()),
                                                   "run_id": run_id, "target_path": str(target)})
    _preparation_event(target_paths, run_id, "original_build_started")
    started = time.monotonic()
    baseline_build = bump_migration_project.build(original)
    _json(target_paths.unity / "original-build.json", baseline_build)
    if not baseline_build["passed"]:
        raise ValueError("Original project must build before migration: " + baseline_build["diagnostics"][-3000:])
    _preparation_event(target_paths, run_id, "original_build_finished", elapsed_seconds=time.monotonic() - started)
    dependency_errors = bump_migration_project.validate_dependencies(original)
    if dependency_errors:
        raise ValueError("Original dependency checkouts failed verification: " + "; ".join(dependency_errors))
    migration = bump_migration_project.capture_build_scope(original, migration)
    record = {**record, "migration": migration}
    _json(pointer, record)
    graph = bump_migration_project.compiler_modules(original, scope=migration["scope"])
    _preparation_event(target_paths, run_id, "original_index_started", module_count=len(graph))
    started = time.monotonic()
    try:
        index_receipt = bump_inventory.capture_original_index(original, migration["scope"], artifact_dir=target_paths.artifacts)
        index = bump_inventory.load_original_index(target_paths.artifacts, index_receipt["index_ref"])
    except BaseException as exc:
        _preparation_event(target_paths, run_id, "original_index_failed", error_type=type(exc).__name__,
                           elapsed_seconds=time.monotonic() - started)
        raise
    _preparation_event(target_paths, run_id, "original_index_finished", module_count=len(index["modules"]),
                       occurrence_count=len(index["occurrences"]), elapsed_seconds=time.monotonic() - started,
                       index_sha256=index["index_sha256"])
    if bump_migration_project.validate_original(paths.project_root, migration):
        raise ValueError("Original project changed during native baseline capture")
    resolution = bump_migration_project.resolve_dependencies(target, migration)
    _json(target_paths.unity / "target-resolution.json", resolution)
    if not resolution["passed"]:
        raise ValueError("Target dependency resolution rejected: " + "; ".join(resolution["errors"]))
    migration, resolution, architect_receipt = bump_architect.prepare_optional_architect(
        target, migration, resolution, mode=architect)
    _json(target_paths.unity / "optional-architect.json", architect_receipt)
    _json(target_paths.unity / "target-resolution.json", resolution)
    migration = bump_migration_project.seal_target(target, migration, resolution=resolution)
    branch = "unity/" + run_id
    bump_migration_project._git(target, "switch", "-c", branch)
    bump_migration_project._git(target, "add", "--", "lean-toolchain", "lake-manifest.json",
                                *[name for name in ("lakefile.toml", "lakefile.lean") if (target / name).is_file()])
    bump_migration_project._git(target, "-c", "user.name=Unity", "-c", "user.email=unity@localhost",
                                "commit", "--allow-empty", "-m", "UNITY: pin Bump target environment")
    baseline = bump_project.capture_baseline_v2(target, migration=migration,
                                               original_index_ref=index_receipt["index_ref"], compiler_modules=graph,
                                               index=index)
    source = freeze_source_bundle_v2(target_paths, migration, index)
    _preparation_event(target_paths, run_id, "target_build_started")
    started = time.monotonic()
    diagnostics = bump_diagnostics.collect_build_diagnostics(target, index, modules=sorted(graph),
        scope=migration["scope"], artifact_dir=target_paths.artifacts)
    _preparation_event(target_paths, run_id, "target_build_finished", passed=diagnostics["passed"],
                       elapsed_seconds=time.monotonic() - started)
    plan = bump_planner.plan_repairs(index, diagnostics)
    bump_state.initialize_source(target_paths.forum, hashlib.sha256(scope_bytes(target_paths)).hexdigest(),
                                 bump_worktree.main_commit(target), source, reset=True, project_baseline=baseline,
                                 return_state=False)
    contract = bump_contract.prepare_migration_contract_v2(target_paths, baseline=baseline, graph=plan,
                                                           source=source, main_sha=bump_worktree.main_commit(target))
    bump_state.initialize_migration_plan(target_paths.forum, index, plan, contract=contract, source=source,
                                         main_sha=bump_worktree.main_commit(target), return_state=False)
    _json(target_paths.forum / "dag.json", plan)
    _preparation_event(target_paths, run_id, "repair_plan_ready", plan_sha256=plan.get("plan_sha256"),
                       module_count=len(graph), occurrence_count=len(index["occurrences"]))
    _json(pointer, {**record, "status": "ready", "migration": migration,
                    "runtime_hashes": runtime_hashes, "branch": branch, "baseline_sha256": baseline["sha256"]})
    return target_paths


def resume(paths: Paths, version: str | None = None, dependency_pins: dict[str, str] | None = None,
           *, project_scope: str | None = None) -> Paths:
    pointer = active_path(paths)
    if pointer.is_symlink() or not pointer.is_file():
        raise ValueError("No new-runtime Bump attempt is available to continue; legacy work was preserved")
    record = json.loads(pointer.read_text())
    if record.get("version") != 2 or record.get("migration_policy") != 2:
        raise ValueError("Old-policy Bump attempts cannot resume under migration-v2; their evidence is preserved")
    if record.get("status") != "ready" or record.get("project_root") != str(paths.project_root.resolve()):
        raise ValueError("Bump preparation did not finish; its evidence is preserved and must not be overwritten or blindly repeated")
    migration = record["migration"]
    if project_scope is not None and project_scope != migration.get("scope", {}).get("mode"):
        raise ValueError("--continue cannot change the sealed project scope")
    _, target = bump_migration_project.resolve_paths(paths.project_root, migration)
    if record.get("target_path") != str(target):
        raise ValueError("Bump target pointer does not match its owned workspace")
    if version is not None:
        wanted = version if ":" in version else "leanprover/lean4:" + version
        if wanted != migration["target_version"]:
            raise ValueError("--continue cannot change the pinned target Lean version")
    if dependency_pins and dependency_pins != migration["dependency_pins"]:
        raise ValueError("--continue cannot change dependency pins")
    errors = bump_migration_project.validate_original(paths.project_root, migration)
    errors.extend(bump_migration_project.validate_target(target, migration))
    if errors:
        raise ValueError("Cannot continue Bump: " + "; ".join(errors))
    target_paths = bump_paths(Paths.from_unity_dir(target / ".unity"))
    for name, expected in record["runtime_hashes"].items():
        if _sha(paths.unity / name) != expected or _sha(target_paths.unity / name) != expected:
            raise ValueError(f"Bump runtime configuration changed: {name}")
    state = bump_state.load_state(target_paths.forum)
    if not state.get("run_id") or (state.get("project_baseline") or {}).get("sha256") != record["baseline_sha256"]:
        raise ValueError("Bump saved state does not match the initialized native baseline")
    bump_contract.validate_migration_state(state)
    bump_project.require_original_branch(target, state["project_baseline"])
    bump_project._require_clean(target)
    if bump_worktree.main_commit(target) != state["formalization"]["main_sha"]:
        raise ValueError("Bump target HEAD changed outside checked candidate integration")
    require_source_matches(target_paths, state)
    return target_paths


def check_ready_modules(paths: Paths) -> dict:
    """Refresh v2 compiler diagnostics under the serialized integration boundary.

    Legacy saved-state fixtures retain the old controller verification path;
    fresh v2 attempts do not run that semantic sweep before dispatching workers.
    """
    # The integration path owns this same lock; never inspect source while a
    # candidate is changing main. Forum activity remains possible and is guarded
    # separately by the state's compare-and-swap revision at publication.
    from .bump_runtime import _merge_lock

    with _merge_lock(paths.project_root):
        state = bump_state.load_state(paths.forum)
        if (state.get("formalization", {}).get("contract") or {}).get("migration_policy") == 2:
            return refresh_target_diagnostics(paths, state=state)
        return _check_ready_modules_locked(paths)


def refresh_target_diagnostics(paths: Paths, *, state: dict | None = None) -> dict:
    """Rebuild the changed target frontier and replace source-bound diagnostics.

    Caller owns the integration lock. No semantic acceptance is inferred from a
    disappearing error, and an unchanged diagnostic generation is never rebuilt
    merely because the scheduler polled again.
    """
    from . import bump_diagnostics, bump_inventory, bump_planner

    state = state or bump_state.load_state(paths.forum)
    contract = state.get("formalization", {}).get("contract") or {}
    if contract.get("migration_policy") != 2:
        raise ValueError("Diagnostic refresh requires migration-v2")
    # Proposals only describe correspondences. The controller validates their
    # exact evidence and owns publication; semantic approval remains separate.
    pending = tuple(key for key, proposal in state.get("migration_mapping_proposals", {}).items()
                    if proposal.get("status") == "proposed")
    for proposal_id in pending:
        state = bump_state.load_state(paths.forum)
        proposal = state.get("migration_mapping_proposals", {}).get(proposal_id)
        if not proposal or proposal.get("status") != "proposed":
            continue
        contract = state["formalization"]["contract"]
        proposed_contract, reason = None, ""
        try:
            current = bump_contract.migration_source_identity_v2(paths.project_root, contract)
            if (proposal.get("source_sha256") != current["source_sha256"]
                    or proposal.get("main_sha") != bump_worktree.main_commit(paths.project_root)
                    or proposal.get("contract_sha256") != contract["sha256"]):
                raise ValueError("mapping proposal is stale against the current source/contract")
            proposed_contract = bump_contract.with_migration_mapping_v2(paths.project_root, contract, proposal["mapping"])
        except (OSError, ValueError, KeyError) as exc:
            reason = str(exc)[:1000] or type(exc).__name__
        bump_state.resolve_migration_mapping(paths.forum, proposal_id,
            expected_revision=state["revision"], proposed_contract=proposed_contract, reason=reason)
    if pending:
        state = bump_state.load_state(paths.forum)
        contract = state["formalization"]["contract"]
    baseline = state["project_baseline"]
    bump_project.require_original_branch(paths.project_root, baseline)
    bump_project.require_pinned_inputs(paths.project_root, baseline)
    main_sha = bump_worktree.main_commit(paths.project_root)
    if main_sha != state["formalization"]["main_sha"]:
        raise ValueError("Target HEAD changed outside checked integration")
    source_sha256 = bump_contract.source_identity(paths.project_root)["source_sha256"]
    prior = state.get("migration_plan") or {}
    if prior.get("source_sha256") == source_sha256 and not state.get("migration_refresh_required"):
        return state
    index = bump_inventory.load_original_index(paths.artifacts, baseline["original_index_ref"])
    diagnostics = bump_diagnostics.collect_build_diagnostics(paths.project_root, index,
        modules=sorted(baseline["compiler_modules"]), scope=baseline["build_scope"], artifact_dir=paths.artifacts)
    plan = bump_planner.plan_repairs(index, diagnostics, prior_plan=prior)
    bump_state.refresh_migration_plan(paths.forum, index, plan, expected_revision=state["revision"],
                                      expected_main_sha=main_sha, return_state=False)
    return bump_state.load_state(paths.forum)


def _archive_controller_verification(paths: Paths, verification: dict) -> dict:
    """Keep full frontier evidence before publishing an artifact-backed view."""
    payload = json.dumps(verification, sort_keys=True, separators=(",", ":"))
    record = artifacts.store_text(paths.artifacts, payload,
                                  kind="bump_formal_verification", producer="Unity")
    digest = hashlib.sha256()
    for offset in range(0, len(payload), 65536):
        digest.update(payload[offset:offset + 65536].encode("utf-8"))
    if record["sha256"] != digest.hexdigest():
        raise ValueError("controller verification artifact has inconsistent bytes")
    return {**verification, "artifact_id": record["artifact_id"],
            "verification_artifact": {"artifact_id": record["artifact_id"], "sha256": record["sha256"]}}


def _frontier_eligible(state: dict, key: str) -> bool:
    task = state["formal_tasks"][key]
    return (task.get("status") == "pending" and bool(task.get("migration_module"))
            and task.get("faithfulness", {}).get("status") != "changes_requested"
            and all(state["formal_tasks"][dependency].get("status") == "complete"
                    for dependency in task.get("dependencies", [])))


def _check_ready_modules_locked(paths: Paths) -> dict:
    while True:
        state = bump_state.load_state(paths.forum)
        progressed = False
        # Routing hints only: every actual check below reloads and rechecks its
        # preconditions. Blocked modules do not each require a giant JSON parse.
        eligible = tuple(key for key in state["formal_tasks"] if _frontier_eligible(state, key))
        for key in eligible:
            state = bump_state.load_state(paths.forum)
            task = state["formal_tasks"][key]
            if not _frontier_eligible(state, key):
                continue
            # A failed check stays visible as compiler diagnostics; do not build
            # it repeatedly without a source/context change.
            current = bump_contract.source_identity(paths.project_root)
            main_sha = bump_worktree.main_commit(paths.project_root)
            if (task.get("migration_check") or {}).get("source_sha256") == current["source_sha256"]:
                continue
            receipt = bump_contract.check_migration_module(paths, state, key)
            receipt = dict(receipt)
            expected_contract = state["formalization"]["contract"]["sha256"]
            if receipt.setdefault("contract_sha256", expected_contract) != expected_contract:
                raise ValueError("controller module check has an unexpected contract identity")
            if receipt.get("passed"):
                verification = {**receipt, "status": "passed",
                                "policy_sha256": bump_contract.policy_hash(),
                                "contract_sha256": state["formalization"]["contract"]["sha256"]}
                verification = _archive_controller_verification(paths, verification)
                published = bump_state.record_migration_module_check(
                    paths.forum, key, verification, main_sha=main_sha,
                    expected_revision=state["revision"])
                if published is not False:
                    progressed = True
            else:
                diagnostic = _archive_controller_verification(
                    paths, {**receipt, "source_sha256": current["source_sha256"]})
                bump_state.record_migration_diagnostic(paths.forum, key,
                    diagnostic,
                    expected_revision=state["revision"])
        if not progressed:
            current = bump_state.load_state(paths.forum)
            # Do not miss a concurrently unblocked module that the initial
            # routing snapshot excluded. No native result is cached here.
            if any(key not in eligible and _frontier_eligible(current, key)
                   for key in current["formal_tasks"]):
                continue
            return current
