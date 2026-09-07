"""Autoformalize-only formal contracts and exact-source mechanical review snapshots.

These checks preserve a formal specification. They do not establish that its
English interpretation is correct. The semantic critic remains responsible for
that judgment. No printed-expression or textual-discovery fallback is allowed.

This is not an adversarial Lean sandbox or an external proof checker. It assumes
a trusted toolchain/dependency installation; arbitrary elaborator I/O outside the
recorded project inputs is not isolated. Structural identity is intentionally
conservative and may reject harmless refactors, which require re-chunking.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import artifacts, autoformalize_jobs, autoformalize_native, autoformalize_workspace
from .autoformalize_spec import library_declarations, normalize_requirements, normalize_spec, task_spec_hash


AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})
_EXCLUDED = {".git", ".lake", ".unity", ".worktrees", "lake-packages", "__pycache__"}
_CONFIGS = {"lean-toolchain", "lakefile.lean", "lakefile.toml", "lake-manifest.json"}


def _native_axioms(axioms: list[str]) -> set[str]:
    # Older Lean uses shared reduction axioms. Newer Lean's nativeEqTrue
    # (native_decide, bv_decide, and callers) emits <owner>._native.<tactic>.ax_N.
    # Inspect the actual axiom closure, never tactic words in source/comments.
    legacy = {"Lean.ofReduceBool", "Lean.ofReduceNat", "Lean.trustCompiler"}
    return {name for name in axioms if name in legacy or
            re.search(r"(?:^|\.)_native(?:\.[^.]+)*\.ax(?:_[0-9]+)+$", name)}


class ContractEnvironmentError(ValueError):
    """Dependency inspection failed; another chunking attempt cannot repair it."""


@contextmanager
def measure(timings: dict | None, stage: str):
    """Artifact-only elapsed time, including failed checks; never contract input."""
    started = time.monotonic() if timings is not None else 0.0
    try:
        yield
    finally:
        if timings is not None:
            timings[stage] = time.monotonic() - started


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "git inspection failed")
    return result.stdout.strip()


def source_files(root: Path, *, build_dir: str | None = None) -> list[Path]:
    """Include untracked Lean sources too; Git HEAD alone is not a build identity."""
    output = (root / build_dir).resolve() if build_dir else None
    if output is not None and (output == root.resolve() or not output.is_relative_to(root.resolve())):
        raise ValueError("project build directory must be strictly inside the project")
    result = []
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _EXCLUDED
                         and (output is None or (Path(directory) / d).resolve() != output))
        if any((Path(directory) / d).is_symlink() for d in dirs):
            raise ValueError("project source directories must not be symlinks")
        for name in sorted(names):
            if name in _EXCLUDED:
                continue
            path = Path(directory) / name
            if path.suffix == ".lean" or (path.parent == root and name in _CONFIGS):
                if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError(f"contract source must not be a symlink: {path}")
                result.append(path)
    return sorted(result)


def _file_hashes(
    root: Path, *, build_dir: str | None = None,
    allow_internal_file_symlinks: bool = False,
) -> dict:
    # Lean elaboration can read non-Lean inputs (e.g. include_str). Include all
    # local non-runtime inputs, even when untracked or ignored by Git.
    result = {}
    output = (root / build_dir).resolve() if build_dir else None
    if output is not None and (output == root.resolve() or not output.is_relative_to(root.resolve())):
        raise ValueError("project build directory must be strictly inside the project")
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _EXCLUDED
                         and (output is None or (Path(directory) / d).resolve() != output))
        if any((Path(directory) / d).is_symlink() for d in dirs):
            raise ValueError("contract input directories must not be symlinks")
        for name in sorted(names):
            if name in _EXCLUDED:
                continue
            path = Path(directory) / name
            source = path
            link = None
            if path.is_symlink():
                if not allow_internal_file_symlinks:
                    raise ValueError(f"contract input must not be a symlink: {path}")
                link = os.readlink(path)
                source = path.resolve(strict=True)
                if not source.is_relative_to(root.resolve()):
                    raise ValueError(f"dependency symlink escapes its package: {path}")
            if not source.is_file():
                raise ValueError(f"contract input must be a regular file: {path}")
            hashed = hashlib.sha256()
            with source.open("rb") as file:
                for block in iter(lambda: file.read(1024 * 1024), b""):
                    hashed.update(block)
            result[str(path.relative_to(root))] = (
                {"symlink": link, "sha256": hashed.hexdigest()}
                if link is not None else hashed.hexdigest()
            )
    return result


def _dependencies(root: Path) -> dict:
    """Pin actual dependency source bytes, including local/path dependencies.

    Build output is excluded. Do not trust the manifest revision alone when a
    shared package checkout can have local edits.
    """
    manifest_path = root / "lake-manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        raise ContractEnvironmentError("cannot inspect Lake dependency manifest") from exc
    package_dir = root / manifest.get("packagesDir", ".lake/packages")
    result = {}
    for package in manifest.get("packages", []):
        name = package["name"]
        if package.get("type") == "path":
            directory = root / package["dir"]
        else:
            directory = package_dir / name
        try:
            directory = directory.resolve()
            if not directory.is_dir():
                raise ValueError(f"missing dependency source: {name}")
            hashes = _file_hashes(directory, allow_internal_file_symlinks=True)
        except (OSError, ValueError, RuntimeError) as exc:
            raise ContractEnvironmentError(
                f"Cannot fingerprint dependency {name}: {exc}"
            ) from exc
        result[name] = {"path": str(directory), "sources": digest(hashes)}
    return result


def environment_identity(root: Path) -> dict:
    version = autoformalize_jobs.run(root, ["lake", "env", "lean", "--version"], cwd=root,
                             owner="Unity", task_id="contract", serialize_build=True)
    if version.returncode:
        raise ValueError("cannot determine Lean toolchain identity")
    return {
        "lean_version": version.stdout.strip(),
        "config": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                   for name in sorted(_CONFIGS) if (root / name).exists()},
        "dependencies": _dependencies(root),
    }


def source_identity(root: Path, *, layout: dict | None = None) -> dict:
    """Bind source bytes AND Lake ownership; only reuse layout within a candidate.

    Fresh boundary calls rediscover the layout and fingerprint dependencies. The
    versioned hash also invalidates pre-layout-bound verification receipts.
    """
    layout = workspace_layout(root) if layout is None else layout
    return {"main_sha": _git(root, "rev-parse", "HEAD"),
            "source_sha256": digest({"version": 2, "workspace": layout,
                                     "files": _file_hashes(root, build_dir=layout["build_dir"])}),
            "environment": environment_identity(root)}


def workspace_layout(root: Path) -> dict:
    executable = autoformalize_workspace._executable(root)
    layout = autoformalize_workspace.discover(root, ["--layout-only"], executable=executable)
    files = [str(path.relative_to(root))
             for path in source_files(root, build_dir=layout["build_dir"])
             if path.suffix == ".lean" and path.name != "lakefile.lean"]
    data = autoformalize_workspace.discover(root, files, executable=executable)
    if data.get("issues") or not isinstance(data.get("modules"), dict):
        raise ValueError("Lake module inspection returned errors")
    return data


def workspace_modules(root: Path) -> dict[str, str]:
    return workspace_layout(root)["modules"]


def module_for_file(root: Path, filename: str, modules: dict | None = None) -> str:
    path = Path(filename)
    if (path.is_absolute() or path.suffix != ".lean" or ".." in path.parts
            or any(part in _EXCLUDED for part in path.parts)
            or any(part.startswith("-") for part in path.parts)
            or path.name == "lakefile.lean"):
        raise ValueError(f"invalid formalization source file: {filename}")
    if not (root / path).is_file():
        raise ValueError(f"missing formalization scaffold: {filename}")
    modules = workspace_modules(root) if modules is None else modules
    if filename not in modules:
        raise ValueError(f"source file is not owned by a configured Lake library/executable: {filename}")
    return modules[filename]


def inspect_environment(root: Path, tasks: list[dict], *, layout: dict | None = None,
                        timings: dict | None = None,
                        external_declarations: list[str] | None = None) -> dict:
    # Import every project module: generated/private dependencies are inspected by
    # the Lean helper, not filtered through the web blueprint presentation model.
    modules = sorted(set((workspace_modules(root) if layout is None else layout["modules"]).values()))
    names = [task["lean_decl"] for task in tasks]
    requested_externals = external_declarations or []
    if (not isinstance(requested_externals, list)
            or any(not isinstance(name, str) or not name.strip() or name.startswith("-") for name in requested_externals)):
        raise ValueError("external declarations require exact nonempty names")
    externals = sorted(set(requested_externals))
    if not names or not modules:
        raise ValueError("formal contract has no declarations/modules")
    native_timings = {} if timings is not None else None
    job_timings = {} if timings is not None else None
    if timings is not None:
        timings["native_helper"] = native_timings
        timings["inspector_job"] = job_timings
    executable = autoformalize_native.executable(
        root, Path(__file__).with_suffix(".lean"), name="contract", timings=native_timings,
    )
    with measure(timings, "inspector_seconds"):
        result = autoformalize_jobs.run(
            root, ["lake", "env", str(executable), *modules, "--", *names,
                   *(["--external", *externals] if externals else [])],
            cwd=root, owner="Unity", task_id="contract", serialize_build=True,
            timings=job_timings,
        )
    if result.returncode:
        raise ValueError("formal contract inspection failed: " +
                         artifacts.preview_text(result.stderr or result.stdout, 2000))
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise ValueError("formal contract inspector did not return valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("formal contract inspector did not return a JSON object")
    if data.get("issues") or set(data.get("targets", {})) != set(names):
        raise ValueError("formal contract inspection incomplete: " + str(data.get("issues", [])))
    external_records = data.get("external_declarations", {})
    if not isinstance(external_records, dict) or set(external_records) != set(externals):
        raise ValueError("formal contract inspection omitted requested external declarations")
    for name, row in external_records.items():
        if (not isinstance(row, dict) or row.get("name") != name
                or not isinstance(row.get("module"), str) or not row["module"]
                or row["module"] in modules or not isinstance(row.get("type"), list)
                or row.get("target_kind") not in {"theorem", "def", "opaque", "inductive", "constructor", "recursor", "quot", "axiom"}
                or not isinstance(row.get("level_params"), list)
                or not isinstance(row.get("signature"), str) or not row["signature"].strip()
                or not isinstance(row.get("axioms"), list)):
            raise ValueError(f"incomplete kernel evidence for external declaration {name}")
    data["external_declarations"] = external_records
    if any(not isinstance(data.get(key), list)
           for key in ("project_axioms", "project_sorries", "project_used_axioms")):
        raise ValueError("formal contract inspector omitted project-wide axiom/placeholder audit")
    if timings is not None:
        timings["kernel_ms"] = data.get("timings_ms", {})
    return data


def inspect_declarations(root: Path, tasks: list[dict]) -> dict:
    return inspect_environment(root, tasks)["targets"]


def _semantic_record(record: dict) -> dict:
    return {key: value for key, value in record.items() if key not in {"axioms", "signature"}}


def _external_records(records: dict) -> dict:
    """Freeze kernel identities; human-readable signatures are display evidence only."""
    result = {}
    for name, row in records.items():
        if row.get("target_kind") == "axiom" or set(row["axioms"]) - AXIOMS:
            raise ValueError(f"external prerequisite {name} depends on forbidden axioms")
        result[name] = {**row, "fingerprint": digest(_semantic_record(row))}
    return result


def build_sources(root: Path, *, full: bool = False, layout: dict | None = None,
                  task_id: str = "contract", timings: dict | None = None) -> dict:
    layout = workspace_layout(root) if layout is None else layout
    modules = sorted(set(layout["modules"].values()))
    auxiliary = digest({name: value for name, value in _file_hashes(root, build_dir=layout["build_dir"]).items()
                        if Path(name).suffix != ".lean"})
    receipt_path = root / ".unity" / "autoformalize-build-inputs.json"
    receipt = {"auxiliary": auxiliary, "modules": layout["modules"]}
    try:
        previous = json.loads(receipt_path.read_text())
    except (OSError, ValueError):
        previous = None
    if previous != receipt:
        # include_str/custom elaboration inputs are not necessarily Lake trace
        # dependencies. Invalidate only the exact root-project module traces;
        # never clean shared Mathlib/dependency build directories.
        directory = (root / layout["build_dir"]).resolve()
        if directory == root.resolve() or not directory.is_relative_to(root.resolve()):
            raise ValueError("project build directory must be strictly inside the project")
        for filename in layout["traces"].values():
            trace = root / filename
            if trace.suffix != ".trace" or not trace.resolve().is_relative_to(directory):
                raise ValueError("unsafe project module trace path")
            trace.unlink(missing_ok=True)
    # Named module facets guarantee freshness even if a lakefile's default target
    # does not include the theorem module. The full default build is optional.
    commands = ([["lake", "build"]] if full else []) + (
        [["lake", "--rehash", "build", *[f"+{module}" for module in modules]]] if modules else [])
    outputs = []
    for command in commands:
        job_timings = {} if timings is not None else None
        stage = "default_build" if command == ["lake", "build"] else "module_build"
        if timings is not None:
            timings[stage] = job_timings
        result = autoformalize_jobs.run(root, command, cwd=root, owner="Unity", task_id=task_id,
                                serialize_build=True, timings=job_timings)
        outputs.append(" ".join(command) + "\n" + result.stdout + "\n" + result.stderr)
        if result.returncode:
            return {"returncode": result.returncode, "output": "\n".join(outputs)}
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = receipt_path.with_name(f".autoformalize-build-inputs-{uuid.uuid4().hex}.json")
    temporary.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    os.replace(temporary, receipt_path)
    return {"returncode": 0, "output": "\n".join(outputs)}


def freeze_formal_contract(paths, dag: dict) -> dict:
    """Build the chunker's scaffold and freeze its meaning before proof search."""
    root = paths.project_root
    chunks = dag["chunks"]
    from . import autoformalize_state
    source = autoformalize_state.formal_source(autoformalize_state.load_state(paths.forum))
    if (not source or dag.get("solution_candidate") != source["candidate_id"]
            or dag.get("solution_sha256") != source["sha256"]):
        raise ValueError("scaffold must target the current supplied-source snapshot")
    requirements = normalize_requirements(dag["requirements"], chunks,
                                         {row["ref_id"] for row in source["source_refs"]})
    spec = normalize_spec(dag.get("spec"), source=source, requirements=requirements, tasks=chunks)
    modules = workspace_modules(root)
    for chunk in chunks:
        module_for_file(root, chunk.get("lean_file", ""), modules)
    before = source_identity(root)
    build = build_sources(root)
    if build["returncode"]:
        raise ValueError("Lean specification scaffold failed to build: " +
                         artifacts.preview_text(build["output"], 3000))
    inspection = inspect_environment(root, chunks, external_declarations=library_declarations(spec))
    targets = inspection["targets"]
    externals = _external_records(inspection["external_declarations"])
    if source_identity(root) != before:
        raise ValueError("source or dependencies changed while freezing the formal contract")
    for chunk in chunks:
        row = targets[chunk["lean_decl"]]
        if row["module"] != module_for_file(root, chunk["lean_file"], modules):
            raise ValueError(f"scaffold declaration is not in {chunk['lean_file']}")
        if row["target_kind"] == "axiom":
            raise ValueError("use theorem proof holes in the scaffold, not axiom declarations")
        semantic = _semantic_record(row)
        if '"sorryAx"' in json.dumps(semantic):
            raise ValueError("specification types and meaning-bearing definitions must not contain sorry")
        if set(row["axioms"]) - AXIOMS - {"sorryAx"}:
            raise ValueError("specification uses an unexpected axiom")
    body = {
        "version": 2,
        "solution_candidate": dag["solution_candidate"],
        "solution_sha256": dag["solution_sha256"],
        "requirements": requirements,
        "spec": spec,
        "spec_sha256": digest(spec),
        "external_declarations": externals,
        "environment": before["environment"],
        "scaffold_source_sha256": before["source_sha256"],
        "targets": {name: {"target_kind": row["target_kind"], "module": row["module"],
                           "fingerprint": digest(_semantic_record(row))}
                    for name, row in targets.items()},
    }
    # Commit only source/build configuration, never run state or arbitrary files.
    filenames = [str(path.relative_to(root)) for path in source_files(
        root, build_dir=workspace_layout(root)["build_dir"],
    )]
    unrelated_staged = set(_git(root, "diff", "--cached", "--name-only").splitlines()) - set(filenames)
    if unrelated_staged:
        raise ValueError("cannot checkpoint scaffold with unrelated staged files: " +
                         ", ".join(sorted(unrelated_staged)))
    unrelated_dirty = set(_git(root, "diff", "--name-only").splitlines()) - set(filenames)
    if unrelated_dirty:
        raise ValueError("cannot checkpoint scaffold with unrelated tracked changes: " +
                         ", ".join(sorted(unrelated_dirty)))
    _git(root, "add", "--", *filenames)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root).returncode:
        _git(root, "commit", "-m", "UNITY: freeze autoformalize formal specification")
    after = source_identity(root)
    if after != {**before, "main_sha": after["main_sha"]}:
        raise ValueError("source changed while committing the formal specification")
    body["scaffold_main_sha"] = after["main_sha"]
    contract = {**body, "sha256": digest(body)}
    record = artifacts.store_text(paths.artifacts, json.dumps({"contract": contract,
                                  "declarations": targets}, sort_keys=True) + "\n",
                                  kind="autoformalize_formal_contract", producer="Unity")
    contract["artifact_id"] = record["artifact_id"]
    return contract


def check_formal_contract(root: Path, contract: dict, tasks: list[dict],
                          *, completed: set[str], layout: dict | None = None,
                          environment: dict | None = None, timings: dict | None = None) -> dict:
    """Identity is conservative: harmless signature refactors require re-chunking."""
    body = {key: value for key, value in contract.items() if key not in {"sha256", "artifact_id"}}
    if not contract or digest(body) != contract.get("sha256"):
        return {"passed": False, "issues": ["formal contract is missing or corrupt"], "targets": {}}
    if contract.get("version") != 2 or not isinstance(contract.get("spec"), dict):
        return {"passed": False, "issues": ["formal contract lacks source-evidence metadata; re-chunk it"], "targets": {}}
    if digest(contract["spec"]) != contract.get("spec_sha256"):
        return {"passed": False, "issues": ["formal contract spec digest is inconsistent"], "targets": {}}
    issues = []
    environment = environment_identity(root) if environment is None else environment
    if environment != contract["environment"]:
        issues.append("toolchain or dependency environment changed from the formal contract")
    try:
        expected_externals = library_declarations(contract["spec"])
        if set(contract.get("external_declarations", {})) != set(expected_externals):
            raise ValueError("formal contract has inconsistent external prerequisite evidence")
        inspection = inspect_environment(root, tasks, layout=layout, timings=timings,
                                         external_declarations=expected_externals)
        targets = inspection["targets"]
        external_records = _external_records(inspection["external_declarations"])
    except autoformalize_jobs.JobCancelled:
        raise
    except ValueError as exc:
        return {"passed": False, "issues": [*issues, str(exc)], "targets": {}}
    native = _native_axioms(inspection["project_used_axioms"])
    if native:
        issues.append("project uses native evaluation axioms: " + ", ".join(sorted(native)))
    for name, row in external_records.items():
        if row["fingerprint"] != contract["external_declarations"][name].get("fingerprint"):
            issues.append(f"external prerequisite signature changed: {name}")
    if completed == {task.get("task_id", task.get("id")) for task in tasks}:
        if inspection["project_axioms"]:
            issues.append("project retains custom axioms: " + ", ".join(inspection["project_axioms"]))
        if inspection["project_sorries"]:
            issues.append("project retains proof holes: " + ", ".join(inspection["project_sorries"]))
    for task in tasks:
        name = task["lean_decl"]
        row = targets[name]
        if digest(_semantic_record(row)) != contract["targets"].get(name, {}).get("fingerprint"):
            issues.append(f"contract changed for {name}: type, definition, or declaration identity differs")
        if task.get("task_id", task.get("id")) in completed:
            unexpected = set(row["axioms"]) - AXIOMS
            if unexpected:
                issues.append(f"{name} depends on forbidden axioms: {', '.join(sorted(unexpected))}")
    return {"passed": not issues, "issues": issues, "targets": targets,
            "external_declarations": external_records}


def _problem_matches(paths, state: dict) -> bool:
    try:
        return hashlib.sha256(paths.unity_md.read_bytes()).hexdigest() == state["problem_sha256"]
    except OSError:
        return False


def carry_forward_revalidation(paths, new_contract: dict, old_state: dict,
                               new_chunks: list[dict], new_requirements: list[dict]) -> dict:
    """Fresh kernel evidence for unchanged completed tasks in a replanned contract.

    A failed check preserves nothing; it does not prevent a valid new plan from
    proceeding. Historical candidates and their old verification are never edited.
    """
    try:
        old_formal = old_state["formalization"]
        old_contract = old_formal.get("contract") or {}
        if old_contract.get("version") != 2 or not old_formal.get("spec"):
            return {}
        new_tasks = {row.get("task_id", row.get("id")): row for row in new_chunks}
        reusable = []
        for task_id, old_task in old_state["formal_tasks"].items():
            if old_task.get("status") != "complete" or task_id not in new_tasks:
                continue
            if task_spec_hash(old_task, old_formal["requirements"], old_formal["spec"], old_contract) == task_spec_hash(
                    new_tasks[task_id], new_requirements, new_contract["spec"], new_contract):
                reusable.append(task_id)
        if not reusable:
            return {}
        before = source_identity(paths.project_root)
        if before["main_sha"] != new_contract.get("scaffold_main_sha"):
            return {}
        check = check_formal_contract(paths.project_root, new_contract, new_chunks, completed=set(reusable))
        if not check["passed"] or source_identity(paths.project_root) != before:
            return {}
        return {"status": "passed", "contract_sha256": new_contract["sha256"],
                "spec_sha256": new_contract["spec_sha256"], "task_ids": sorted(reusable),
                "main_sha": before["main_sha"], "source_sha256": before["source_sha256"]}
    except autoformalize_jobs.JobCancelled:
        raise
    except (KeyError, OSError, ValueError):
        return {}


def _external_evidence(contract: dict) -> dict:
    """Compact critic-facing signatures; full structural records remain in artifacts."""
    return {name: {key: row[key] for key in ("fingerprint", "module", "signature", "axioms")}
            for name, row in contract.get("external_declarations", {}).items()}


def snapshot_is_current(paths, state: dict, snapshot: dict) -> bool:
    from .autoformalize_input import source_matches
    from .autoformalize_state import repair_digest

    if not snapshot:
        return False
    current = source_identity(paths.project_root)
    formal = state["formalization"]
    contract = formal.get("contract") or {}
    source = state.get("input_source") or {}
    return (
        current["main_sha"] == snapshot.get("main_sha") == formal.get("main_sha")
        and current["source_sha256"] == snapshot.get("source_sha256")
        and current["environment"] == snapshot.get("environment")
        and contract.get("sha256") == snapshot.get("contract_sha256")
        and contract.get("version") == 2
        and contract.get("spec_sha256") == snapshot.get("spec_sha256")
        == digest(formal.get("spec")) == digest(contract.get("spec"))
        and snapshot.get("repairs_sha256") == repair_digest(state)
        and snapshot.get("external_declarations") == _external_evidence(contract)
        and formal.get("revision") == snapshot.get("formalization_revision")
        and formal.get("solution_candidate") == snapshot.get("solution_candidate")
        == contract.get("solution_candidate") == source.get("candidate_id")
        and formal.get("solution_sha256") == snapshot.get("solution_sha256")
        == contract.get("solution_sha256") == source.get("sha256")
        and {key: task.get("accepted_candidate") for key, task in state["formal_tasks"].items()}
        == snapshot.get("accepted_candidates")
        and _problem_matches(paths, state)
        and source_matches(paths, state)
    )


def verify_final_project(paths, state: dict) -> dict:
    from .autoformalize_input import source_matches
    from .autoformalize_state import repair_digest

    root = paths.project_root
    formal = state["formalization"]
    contract = formal.get("contract") or {}
    before = source_identity(root)
    tasks = list(state["formal_tasks"].values())
    last = next((candidate for candidate in reversed(list(state["formal_candidates"].values()))
                 if candidate.get("status") == "merged" and candidate.get("main_sha") == before["main_sha"]), {})
    verification = last.get("verification") or {}
    complete_ids = set(state["formal_tasks"])
    reusable = (verification.get("status") == "passed"
                and verification.get("source_identity") == before
                and verification.get("contract_sha256") == formal.get("contract", {}).get("sha256")
                and set(verification.get("verified_tasks", [])) == complete_ids
                and last.get("build", {}).get("returncode") == 0)
    if reusable:
        build = {"returncode": 0, "reused_candidate": last["candidate_id"]}
        check = {"passed": True, "issues": [], "targets": {},
                 "reused_verification": verification.get("artifact_id")}
    else:
        build = build_sources(root, full=True)
        check = (check_formal_contract(root, formal.get("contract", {}), tasks, completed=complete_ids)
                 if not build["returncode"] else
                 {"passed": False, "issues": ["final project build failed"], "targets": {}})
    issues = list(check["issues"])
    if (contract.get("version") != 2 or not isinstance(formal.get("spec"), dict)
            or digest(formal.get("spec")) != contract.get("spec_sha256")
            or digest(contract.get("spec")) != contract.get("spec_sha256")):
        issues.append("source-evidence spec is missing or inconsistent; re-chunk it")
    if any(task.get("status") != "complete" for task in tasks) or not tasks:
        issues.append("formal tasks are incomplete")
    if source_identity(root) != before:
        issues.append("source changed during final mechanical verification")
    if before["main_sha"] != formal.get("main_sha"):
        issues.append("main differs from the recorded accepted candidate revision")
    if not _problem_matches(paths, state):
        issues.append("original problem changed or is missing; start a fresh run")
    if not source_matches(paths, state):
        issues.append("supplied source bytes changed or are missing")
    report = {
        **before,
        "passed": not issues,
        "issues": issues,
        "solution_candidate": formal["solution_candidate"],
        "solution_sha256": formal["solution_sha256"],
        "formalization_revision": formal["revision"],
        "contract_sha256": formal.get("contract", {}).get("sha256"),
        "spec_sha256": digest(formal.get("spec")),
        "repairs_sha256": repair_digest(state),
        "external_declarations": _external_evidence(contract),
        "accepted_candidates": {task["task_id"]: task.get("accepted_candidate") for task in tasks},
        "declarations": {task["lean_decl"]: task["task_id"] for task in tasks},
        "build": build,
        "targets": check["targets"],
    }
    report["snapshot_id"] = "review-" + uuid.uuid4().hex
    artifact = artifacts.store_text(paths.artifacts, json.dumps(report, sort_keys=True) + "\n",
                                    kind="autoformalize_machine_review", producer="Unity")
    # Keep telemetry out of shared prompt memory. Detail remains in the artifact.
    return {key: value for key, value in report.items() if key not in {"build", "targets"}} | {
        "artifact_id": artifact["artifact_id"],
    }
