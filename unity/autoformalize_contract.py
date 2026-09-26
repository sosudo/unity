"""Autoformalize-only formal contracts and exact-source mechanical review snapshots.

These checks preserve a formal specification. They do not establish that its
English interpretation is correct. The semantic critic remains responsible for
that judgment. No printed-expression or textual-discovery fallback is allowed.

This is not an adversarial Lean sandbox or an external proof checker. It assumes
a trusted toolchain/dependency installation; arbitrary elaborator I/O outside the
recorded project inputs is not isolated. Structural identity is intentionally
conservative and may reject harmless refactors, which require an explicit
representation revision rather than silently changing an adopted target.
"""

from __future__ import annotations

import hashlib
import copy
import json
import os
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import artifacts, autoformalize_cache, autoformalize_jobs, autoformalize_native, autoformalize_workspace
from .autoformalize_spec import library_declarations, normalize_outputs, normalize_requirements, normalize_spec, task_spec_hash


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


class ContractInspectionError(ValueError):
    """Kernel diagnostic with exact failed witness names, not parsed prose."""

    def __init__(self, message: str, declaration_errors: list[dict] | None = None,
                 project_declarations: list[dict] | None = None):
        super().__init__(message)
        self.declaration_errors = declaration_errors or []
        self.project_declarations = project_declarations or []


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


def policy_hash() -> str:
    """Bind reuse to the current autoformalize checking implementation."""
    directory = Path(__file__).parent
    policy = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in (
        "autoformalize_contract.py", "autoformalize_contract.lean",
        "autoformalize_spec.py", "autoformalize_runtime.py", "autoformalize_state.py",
        "autoformalize_workspace.py", "autoformalize_workspace.lean",
        "autoformalize_native.py", "autoformalize_jobs.py", "autoformalize_cache.py",
    )}
    return digest(policy)


def observe_failure_inputs(root: Path, contract: dict) -> dict:
    """Read current retry inputs only when a known rejection needs comparison."""
    identity = source_identity(root)
    return {"main_sha": identity["main_sha"], "source_sha256": identity["source_sha256"],
            "contract_sha256": contract.get("sha256"),
            "environment_sha256": digest(identity["environment"]), "policy_sha256": policy_hash()}


def _prerequisite_blocker(row: dict, code: str, message: str, required_action: str) -> dict:
    return {"code": code, "prerequisite_id": row.get("id", ""),
            "task_ids": sorted(set(row.get("needed_by", []))), "message": message,
            "required_action": required_action, "deterministic": True}


def prerequisite_blockers(contract: dict, *, completed: set[str], final: bool) -> list[dict]:
    """Cheap known source-accounting failures; never invent kernel verification.

    ``completed`` may include the proposed complete candidate. Declaration
    existence and semantic correspondence are not knowable from this metadata.
    Draft unresolved/inline evidence remains permissible before the final gate.
    """
    blockers = []
    if not final:
        return blockers
    spec = contract.get("spec") or {}
    for row in spec.get("prerequisites", []):
        resolution = row.get("resolution") or {}
        kind = resolution.get("kind")
        identifier = row.get("id", "<unnamed>")
        if kind == "unresolved":
            blockers.append(_prerequisite_blocker(row, "prerequisite_unresolved",
                f"Source prerequisite {identifier} remains unresolved (consumers: {', '.join(row['needed_by'])}).",
                "Use refine_chunks prerequisite_resolutions with an exact declaration witness, "
                "a separate provider task, or argument evidence explaining its discharge; retain source citations."))
        elif kind == "argument":
            requirements = {item["id"]: item for item in contract.get("requirements", [])}
            implementing = set(row.get("needed_by", []))
            for argument in spec.get("arguments", []):
                if identifier in argument.get("prerequisites", []):
                    implementing.update(requirements.get(argument["requirement_id"], {}).get("tasks", []))
            missing = implementing - completed
            if not str(resolution.get("rationale", "")).strip() or not implementing:
                blockers.append(_prerequisite_blocker(row, "prerequisite_argument_missing",
                    f"Source prerequisite {identifier} lacks explicit argument evidence.",
                    "Provide a nonempty rationale and retain its consuming argument/requirement mapping."))
            elif missing:
                blockers.append(_prerequisite_blocker(row, "prerequisite_argument_incomplete",
                    f"Source prerequisite {identifier} needs complete proof evidence from: {', '.join(sorted(missing))}.",
                    "Complete the mapped consumer tasks; an inline-evidence rationale does not prove them."))
        elif kind == "task" and resolution.get("task_id") not in completed:
            blockers.append(_prerequisite_blocker(row, "prerequisite_task_incomplete",
                f"Source prerequisite {identifier} provider task {resolution.get('task_id')} is not complete.",
                "Complete the declared provider task, or correct the resolution with actual evidence."))
        elif kind == "library" and resolution.get("declaration") in contract.get("targets", {}):
            blockers.append(_prerequisite_blocker(row, "prerequisite_project_owned",
                f"Source prerequisite {identifier} cites project-owned {resolution['declaration']} as a library.",
                "Use kind=declaration for this exact project witness; do not invent a helper task or self-edge."))
        elif kind not in {"task", "library", "declaration"}:
            blockers.append(_prerequisite_blocker(row, "prerequisite_invalid_resolution",
                f"Source prerequisite {identifier} has an invalid resolution.",
                "Correct its resolution with refine_chunks; no resolution status is model-trusted evidence."))
    return blockers


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


def _dependency_file_hashes(root: Path) -> dict:
    """Exclude conventional Lake hash caches, not their actual source inputs."""
    hashes = _file_hashes(root, allow_internal_file_symlinks=True)

    # Without package-local Git metadata, retain every input.
    if not (root / ".git").exists():
        return hashes

    candidates = {
        name for name, value in hashes.items()
        if name.endswith(".hash")
        and name[:-5] in hashes
        and isinstance(value, str)  # Regular file, not a symlink.
    }
    if not candidates:
        return hashes

    try:
        result = subprocess.run(
            ["git", "check-ignore", "--stdin", "-z"],
            cwd=root,
            input=b"".join(
                os.fsencode(name) + b"\0" for name in sorted(candidates)
            ),
            capture_output=True,
            check=False,
        )
    except OSError:
        return hashes

    if result.returncode not in (0, 1):
        return hashes

    ignored = {
        os.fsdecode(name)
        for name in result.stdout.split(b"\0") if name
    } & candidates

    for name in ignored:
        # Keep the underlying input; handle nested .hash files conservatively.
        if name[:-5] not in ignored:
            del hashes[name]

    return hashes


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
            hashes = _dependency_file_hashes(directory)
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
                        external_declarations: list[str] | None = None,
                        prerequisite_declarations: list[str] | None = None,
                        _compiled_before: dict | None = None) -> dict:
    # Import every project module: generated/private dependencies are inspected by
    # the Lean helper, not filtered through the web blueprint presentation model.
    modules = sorted(set((workspace_modules(root) if layout is None else layout["modules"]).values()))
    names = [task["lean_decl"] for task in tasks]
    requested_externals = external_declarations or []
    if (not isinstance(requested_externals, list)
            or any(not isinstance(name, str) or not name.strip() or name.startswith("-") for name in requested_externals)):
        raise ValueError("external declarations require exact nonempty names")
    externals = sorted(set(requested_externals))
    witnesses = prerequisite_declarations or []
    if (not isinstance(witnesses, list)
            or any(not isinstance(name, str) or not name.strip() or name.startswith("-") for name in witnesses)):
        raise ValueError("prerequisite declarations require exact nonempty names")
    witnesses = sorted(set(witnesses))
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
    cache_key, cache_identity, cache_before, cached = None, None, None, None
    # Real projects have Git identity. Incomplete environments and unavailable
    # cache observations always use the normal inspector, never fail open.
    if (root / ".git").exists():
        try:
            cache_identity = source_identity(root, layout=layout)
            cache_key = digest({"version": 1, "source": cache_identity["source_sha256"],
                "environment": cache_identity["environment"], "policy": policy_hash(),
                "executable": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "modules": modules, "targets": sorted(names), "externals": externals, "witnesses": witnesses})
            cached, cache_before = autoformalize_cache.lookup(root, cache_key)
            if _compiled_before is not None:
                cached, cache_before = None, _compiled_before
        except (OSError, ValueError, KeyError, TypeError):
            cache_key = None
    if timings is not None:
        timings["inspection_cache_hit"] = cached is not None
    with measure(timings, "inspector_seconds"):
        result = (subprocess.CompletedProcess([], 0, json.dumps(cached), "") if cached is not None else autoformalize_jobs.run(
            root, ["lake", "env", str(executable), *modules, "--", *names,
                   *(["--external", *externals] if externals else []),
                   *(["--prerequisite", *witnesses] if witnesses else [])],
            cwd=root, owner="Unity", task_id="contract", serialize_build=True,
            timings=job_timings,
        ))
    inventory = []
    def failure(message: str, declaration_errors: list[dict] | None = None) -> ValueError:
        record = artifacts.store_text(
            root / ".unity" / "artifacts",
            json.dumps({"returncode": result.returncode, "stdout": result.stdout,
                        "stderr": result.stderr}, ensure_ascii=False),
            kind="autoformalize_inspection", producer="Unity", source="formal contract inspector",
        )
        return ContractInspectionError(f"{message}; full output: artifact {record['artifact_id']}",
                                       declaration_errors, inventory)

    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        message = "formal contract inspector did not return valid JSON"
        if result.returncode:
            message = "formal contract inspection failed: " + artifacts.preview_text(
                result.stderr or result.stdout or f"inspector exited {result.returncode}", 2000)
        raise failure(message) from exc
    if not isinstance(data, dict):
        raise failure("formal contract inspector did not return a JSON object")
    inventory = data.get("project_declarations", [])
    if (not isinstance(inventory, list) or any(not isinstance(row, dict)
            or set(row) != {"name", "module", "kind"}
            or not isinstance(row["name"], str) or not row["name"]
            or not isinstance(row["module"], str) or not isinstance(row["kind"], str)
            or row["module"] not in modules
            or row["kind"] not in {"theorem", "def", "opaque", "inductive", "constructor", "recursor", "quot", "axiom"}
            for row in inventory)
            or len({row["name"] for row in inventory}) != len(inventory)):
        inventory = []
        raise failure("formal contract inspector returned invalid declaration inventory")
    issues = data.get("issues", [])
    if not isinstance(issues, list) or any(not isinstance(issue, str) for issue in issues):
        raise failure("formal contract inspector returned invalid issues")
    declaration_errors = data.get("declaration_errors", [])
    if (not isinstance(declaration_errors, list) or any(
            not isinstance(row, dict) or set(row) != {"declaration", "code"}
            or row["declaration"] not in set(names) | set(externals) | set(witnesses)
            or row["code"] not in {"not_found", "project_owned", "not_project_owned"} for row in declaration_errors)):
        raise failure("formal contract inspector returned invalid prerequisite diagnostics")
    if issues:
        raise failure("formal contract inspection failed: " + artifacts.preview_text("; ".join(issues), 2000),
                      declaration_errors)
    if result.returncode:
        raise failure("formal contract inspection failed: " + artifacts.preview_text(
            result.stderr or f"inspector exited {result.returncode} without reporting issues", 2000))
    if set(data.get("targets", {})) != set(names):
        raise failure("formal contract inspection incomplete: target declarations do not match")
    external_records = data.get("external_declarations", {})
    if not isinstance(external_records, dict) or set(external_records) != set(externals):
        raise failure("formal contract inspection omitted requested external declarations")
    for name, row in external_records.items():
        if (not isinstance(row, dict) or row.get("name") != name
                or not isinstance(row.get("module"), str) or not row["module"]
                or row["module"] in modules or not isinstance(row.get("type"), list)
                or row.get("target_kind") not in {"theorem", "def", "opaque", "inductive", "constructor", "recursor", "quot", "axiom"}
                or not isinstance(row.get("level_params"), list)
                or not isinstance(row.get("signature"), str) or not row["signature"].strip()
                or not isinstance(row.get("axioms"), list)):
            raise failure(f"incomplete kernel evidence for external declaration {name}")
    data["external_declarations"] = external_records
    witness_records = data.get("prerequisite_declarations", {})
    if not isinstance(witness_records, dict) or set(witness_records) != set(witnesses):
        raise failure("formal contract inspection omitted requested prerequisite declarations")
    for name, row in witness_records.items():
        if (not isinstance(row, dict) or row.get("name") != name
                or not isinstance(row.get("module"), str) or not row["module"]
                or not isinstance(row.get("type"), list)
                or row.get("target_kind") not in {"theorem", "def", "opaque", "inductive", "constructor", "recursor", "quot", "axiom"}
                or not isinstance(row.get("level_params"), list)
                or not isinstance(row.get("signature"), str) or not row["signature"].strip()
                or not isinstance(row.get("meanings"), dict)
                or not isinstance(row.get("proof_dependencies"), list)
                or not isinstance(row.get("axioms"), list)):
            raise failure(f"incomplete kernel evidence for prerequisite declaration {name}")
    data["prerequisite_declarations"] = witness_records
    if any(not isinstance(data.get(key), list)
           for key in ("project_axioms", "project_sorries", "project_used_axioms")):
        raise failure("formal contract inspector omitted project-wide axiom/placeholder audit")
    if timings is not None:
        timings["kernel_ms"] = data.get("timings_ms", {})
    compiled = cache_before if cached is not None else None
    if cache_key is not None and cached is None:
        try:
            if source_identity(root) == cache_identity:
                compiled = autoformalize_cache.publish(root, cache_key, data, cache_before)
        except autoformalize_cache.CacheUnavailable as exc:
            if _compiled_before is None:
                # Cache storage is optional. Only when its path hint cannot be
                # saved, import once more with this known closure hashed first.
                if timings is not None:
                    timings["cache_unavailable_reinspection"] = True
                return inspect_environment(root, tasks, layout=layout, timings=timings,
                    external_declarations=external_declarations,
                    prerequisite_declarations=prerequisite_declarations, _compiled_before=exc.identity)
        except (OSError, ValueError):
            pass
    data["compiled_receipt"] = autoformalize_cache.compiled_receipt(root, compiled)
    return data


def inspect_declarations(root: Path, tasks: list[dict]) -> dict:
    return inspect_environment(root, tasks)["targets"]


def _alpha_binders(value):
    if isinstance(value, dict):
        return {key: _alpha_binders(item) for key, item in value.items()}
    if not isinstance(value, list):
        return value
    result = [_alpha_binders(item) for item in value]
    if (result and isinstance(result[0], str)
            and len(result) == {"lam": 5, "forallE": 5, "letE": 6}.get(result[0])
            and isinstance(result[1], list) and result[1]
            and isinstance(result[1][0], str)
            and result[1][0] in {"anonymous", "str", "num"}):
        result[1] = ["anonymous"]
    return result


def _semantic_record(record: dict, *, fingerprint_version: int = 1) -> dict:
    if fingerprint_version not in {1, 2}:
        raise ValueError("unsupported semantic fingerprint version")
    result = {key: value for key, value in record.items()
              if key not in {"axioms", "signature", "proof_dependencies"}}
    return _alpha_binders(result) if fingerprint_version == 2 else result


def _external_records(records: dict, *, fingerprint_version: int = 1) -> dict:
    """Freeze kernel identities; human-readable signatures are display evidence only."""
    result = {}
    for name, row in records.items():
        if row.get("target_kind") == "axiom" or set(row["axioms"]) - AXIOMS:
            raise ContractInspectionError(f"external prerequisite {name} depends on forbidden axioms",
                                          [{"declaration": name, "code": "forbidden_axioms"}])
        result[name] = {**row, "fingerprint": digest(_semantic_record(row, fingerprint_version=fingerprint_version))}
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


def _seal_contract(contract: dict) -> dict:
    body = {key: copy.deepcopy(value) for key, value in contract.items()
            if key not in {"sha256", "artifact_id"}}
    return {**body, "sha256": digest(body)}


def prepare_source_contract(paths, dag: dict, *, state: dict | None = None,
                            environment: dict | None = None, main_sha: str | None = None) -> dict:
    """Pin an informal plan without generating, building, or inspecting Lean.

    A plan records obligations, not trusted declarations. Only successful exact
    candidate checks can extend ``bindings`` and ``targets`` later.
    """
    from . import autoformalize_state

    state = autoformalize_state.load_state(paths.forum) if state is None else state
    source = autoformalize_state.formal_source(state)
    if (not source or dag.get("solution_candidate") != source["candidate_id"]
            or dag.get("solution_sha256") != source["sha256"]):
        raise ValueError("informal plan must target the current supplied-source snapshot")
    chunks = dag["chunks"]
    requirements = normalize_requirements(dag["requirements"], chunks,
                                         {row["ref_id"] for row in source["source_refs"]})
    spec = normalize_spec(dag.get("spec"), source=source, requirements=requirements,
                          tasks=chunks, allow_unresolved=True)
    previous_contract = state.get("formalization", {}).get("contract") or {}
    contract = _seal_contract({
        "version": 3,
        "fingerprint_version": previous_contract.get("fingerprint_version", 1) if previous_contract else 2,
        **({"representation_review_policy": previous_contract.get("representation_review_policy", 1)}
           if not previous_contract or "representation_review_policy" in previous_contract else {}),
        "solution_candidate": source["candidate_id"], "solution_sha256": source["sha256"],
        "requirements": requirements, "spec": spec, "spec_sha256": digest(spec),
        "environment": environment_identity(paths.project_root) if environment is None else environment,
        "source_main_sha": _git(paths.project_root, "rev-parse", "HEAD") if main_sha is None else main_sha,
        "obligation_ids": sorted(row.get("task_id", row.get("id")) for row in chunks),
        "bindings": {}, "targets": {}, "external_declarations": {}, "prerequisite_declarations": {},
    })
    return contract


def initialize_source_contract(paths, dag: dict) -> dict:
    """Publish a contract artifact, separate from read-only plan preparation."""
    contract = prepare_source_contract(paths, dag)
    record = artifacts.store_text(paths.artifacts, json.dumps({"contract": contract}, sort_keys=True) + "\n",
                                  kind="autoformalize_source_contract", producer="Unity")
    return {**contract, "artifact_id": record["artifact_id"]}


def invalidate_bindings(contract: dict, task_ids: set[str]) -> tuple[dict, set[str]]:
    """Explicit revisions invalidate actual meaning dependencies, not just DAG hints.

    Return new current state; never edit historical contracts or candidates. A
    removed binding can only be adopted again by another checked candidate.
    """
    result = copy.deepcopy(contract)
    affected = set(task_ids)
    bindings = result.get("bindings", {})
    targets = result.get("targets", {})
    while True:
        names = {output["declaration"] for key in affected for output in bindings.get(key, [])}
        added = {key for key, outputs in bindings.items()
                 if any(names.intersection(
                     targets.get(output["declaration"], {}).get("meaning_dependencies", [])
                     + targets.get(output["declaration"], {}).get("verification_dependencies", []))
                     for output in outputs)} - affected
        if not added:
            break
        affected.update(added)
    removed_names = {output["declaration"] for key in affected for output in bindings.get(key, [])}
    result["bindings"] = {key: outputs for key, outputs in bindings.items() if key not in affected}
    result["targets"] = {name: row for name, row in targets.items() if name not in removed_names}
    if "prerequisite_declarations" in result:
        reopened_witnesses = {row["resolution"]["declaration"]
                              for row in result.get("spec", {}).get("prerequisites", [])
                              if row["resolution"]["kind"] == "declaration"
                              and set(row.get("needed_by", [])).intersection(affected)}
        result["prerequisite_declarations"] = {
            name: row for name, row in result["prerequisite_declarations"].items()
            if name not in reopened_witnesses
            and not removed_names.intersection({name} | set(row.get("meanings", {}))
                                                | set(row.get("proof_dependencies", [])))
        }
    return _seal_contract(result), affected


def _binding_tasks(contract: dict) -> list[dict]:
    return [{"task_id": task_id, "lean_decl": output["declaration"], "lean_file": output["file"]}
            for task_id, outputs in contract.get("bindings", {}).items() for output in outputs]


def output_manifest_blockers(contract: dict, *, task_ids: set[str], task_id: str,
                             proposed_outputs: list[dict]) -> list[dict]:
    """Cheap declaration-binding checks, shared by submission and verification.

    This does not establish declaration existence, mathematical coverage or proof
    correctness. Changing adopted outputs always requires explicit refinement.
    """
    if contract.get("version") != 3:
        return []

    def blocked(code: str, message: str, action: str, **details) -> list[dict]:
        return [{"code": code, "prerequisite_id": "", "task_ids": [task_id] if task_id in task_ids else [],
                 "message": message, "required_action": action, "deterministic": True, **details}]

    if task_id not in task_ids or not isinstance(proposed_outputs, list) or not proposed_outputs:
        return blocked("output_manifest_invalid", "candidate outputs require a current task and nonempty declaration/file list",
                       "Provide exact current-task declaration/file outputs.")
    names = []
    for row in proposed_outputs:
        if (not isinstance(row, dict) or set(row) != {"declaration", "file"}
                or any(not isinstance(value, str) or not value.strip() or value != value.strip()
                       for value in row.values()) or row["declaration"].startswith("-")):
            return blocked("output_manifest_invalid", "candidate outputs require exact declaration and file names",
                           "Correct each output to an exact declaration/file pair.")
        names.append(row["declaration"])
    if len(set(names)) != len(names):
        return blocked("output_manifest_duplicate", "candidate outputs repeat declarations",
                       "List each declaration once in the output manifest.")
    try:
        outputs = normalize_outputs(proposed_outputs)
    except ValueError as exc:
        return blocked("output_manifest_invalid", str(exc), "Provide normalized project-relative Lean output files.")
    bindings = contract.get("bindings", {})
    try:
        if (not isinstance(bindings, dict) or set(bindings) - task_ids
                or not isinstance(contract.get("targets"), dict)):
            raise ValueError("invalid binding owners")
        adopted = {owner: normalize_outputs(rows) for owner, rows in bindings.items()}
        all_names = [row["declaration"] for rows in adopted.values() for row in rows]
        if (any(not rows for rows in adopted.values()) or len(set(all_names)) != len(all_names)
                or set(all_names) != set(contract.get("targets", {}))):
            raise ValueError("invalid binding identities")
    except (ValueError, TypeError, KeyError):
        return blocked("contract_bindings_invalid", "source contract has inconsistent adopted output bindings",
                       "Restore the consistent controller-owned contract snapshot; do not rewrite verification records.")
    owners = {row["declaration"]: owner for owner, rows in adopted.items() for row in rows}
    if any(owners.get(name, task_id) != task_id for name in names):
        return blocked("output_ownership_conflict", "candidate output already belongs to another source obligation",
                       "Reference existing outputs as prerequisites; do not claim another source obligation's output.")
    if task_id in adopted and adopted[task_id] != outputs:
        return blocked("output_manifest_changed", "adopted output manifest changed; explicitly revise the node before replacing it",
                       "Restore the adopted manifest or use refine_chunks with reopen_representations before replacing it.",
                       expected_outputs=adopted[task_id], proposed_outputs=outputs)
    return []


def _check_incremental_contract(root: Path, contract: dict, tasks: list[dict], *, completed: set[str],
                                proposed_outputs: list[dict] | None, task_id: str | None,
                                stage: str, final: bool, layout: dict | None,
                                environment: dict | None, timings: dict | None) -> dict:
    fingerprint_version = contract.get("fingerprint_version", 1)
    checked_complete = set(completed)
    if task_id is not None:
        if stage == "complete":
            checked_complete.add(task_id)
        else:
            checked_complete.discard(task_id)
    blockers = prerequisite_blockers(contract, completed=checked_complete, final=final)
    issues = [row["message"] for row in blockers]
    def checked_issue(message: str, code: str, affected: set[str], action: str) -> None:
        issues.append(message)
        blockers.append({"code": code, "prerequisite_id": "", "task_ids": sorted(affected),
                         "message": message, "required_action": action, "deterministic": True})
    def reject(message: str, code: str, affected: set[str], action: str) -> None:
        checked_issue(message, code, affected, action)
        raise ValueError(message)
    proposed = copy.deepcopy(contract)
    task_ids = {task.get("task_id", task.get("id")) for task in tasks}
    if stage not in {"representation", "complete"}:
        return {"passed": False, "issues": ["invalid candidate formalization stage"], "targets": {}}
    if set(contract.get("obligation_ids", [])) != task_ids:
        issues.append("source contract obligations differ from the current task graph")
    if set(completed) - task_ids:
        issues.append("completion references unknown formalization tasks")
    try:
        bindings = proposed.setdefault("bindings", {})
        if not isinstance(bindings, dict) or not isinstance(proposed.get("targets"), dict):
            reject("source contract has invalid adopted output bindings", "contract_bindings_invalid", set(),
                   "Restore controller-owned contract state; do not rewrite verification records manually.")
        old_names = [row["lean_decl"] for row in _binding_tasks(contract)]
        if len(set(old_names)) != len(old_names) or set(old_names) != set(contract["targets"]):
            reject("source contract target identities do not match adopted bindings", "contract_bindings_invalid", set(),
                   "Restore the consistent controller-owned contract snapshot.")
        if set(bindings) - task_ids or any(not rows for rows in bindings.values()):
            reject("source contract contains invalid task bindings", "contract_bindings_invalid", set(),
                   "Restore the consistent controller-owned task/output snapshot.")
        if proposed_outputs is not None:
            manifest_issues = output_manifest_blockers(contract, task_ids=task_ids, task_id=task_id,
                                                       proposed_outputs=proposed_outputs)
            if manifest_issues:
                blockers.extend(manifest_issues)
                issues.extend(row["message"] for row in manifest_issues)
                raise ValueError(manifest_issues[0]["message"])
            bindings[task_id] = normalize_outputs(proposed_outputs)
        actual_tasks = _binding_tasks(proposed)
        if not actual_tasks:
            reject("no Lean representations have been adopted or proposed", "output_manifest_missing", set(),
                   "Submit the current task's actual Lean declaration/file outputs.")
        layout = workspace_layout(root) if layout is None else layout
        for row in actual_tasks:
            try:
                module_for_file(root, row["lean_file"], layout["modules"])
            except ValueError as exc:
                reject(str(exc), "output_file_invalid", {row["task_id"]},
                       "Use an existing project-owned Lean source file in the output manifest.")
        expected = sorted({row["resolution"]["declaration"] for row in contract["spec"]["prerequisites"]
                           if row["resolution"]["kind"] == "library"
                           and (final or set(row["needed_by"]).intersection(bindings))})
        # Retain already inspected prerequisite identities across unrelated node
        # additions. Future, merely predicted prerequisites need not be imported.
        expected = sorted(set(expected) | set(contract.get("external_declarations", {})))
        witnesses = sorted({row["resolution"]["declaration"] for row in contract["spec"]["prerequisites"]
                            if row["resolution"]["kind"] == "declaration"
                            and (final or set(row["needed_by"]).intersection(checked_complete))}
                           | set(contract.get("prerequisite_declarations", {})))
        inspection = inspect_environment(root, actual_tasks, layout=layout, timings=timings,
                                         external_declarations=expected,
                                         **({"prerequisite_declarations": witnesses} if witnesses else {}))
        external_records = _external_records(inspection["external_declarations"], fingerprint_version=fingerprint_version)
        witness_records = inspection.get("prerequisite_declarations", {})
        if set(witness_records) != set(witnesses):
            raise ValueError("formal contract inspection omitted prerequisite declaration evidence")
        for name, row in witness_records.items():
            if row.get("target_kind") == "axiom" or set(row["axioms"]) - AXIOMS:
                for prerequisite in contract["spec"]["prerequisites"]:
                    if prerequisite["resolution"].get("declaration") == name:
                        blocker = _prerequisite_blocker(prerequisite, "prerequisite_forbidden_axioms",
                            f"Source prerequisite {prerequisite['id']} witness {name} depends on forbidden axioms.",
                            "Finish or replace this witness with a checked proof; a declaration name is not sufficient evidence.")
                        blockers.append(blocker)
                        issues.append(blocker["message"])
        witness_records = {name: {**row, "fingerprint": digest(_semantic_record(row, fingerprint_version=fingerprint_version))}
                           for name, row in witness_records.items()}
    except autoformalize_jobs.JobCancelled:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        for diagnostic in getattr(exc, "declaration_errors", []):
            matched = False
            for prerequisite in contract["spec"]["prerequisites"]:
                if prerequisite["resolution"].get("declaration") == diagnostic["declaration"]:
                    matched = True
                    project_owned = diagnostic["code"] == "project_owned"
                    forbidden = diagnostic["code"] == "forbidden_axioms"
                    blocker = _prerequisite_blocker(prerequisite, "prerequisite_" + diagnostic["code"],
                        f"Source prerequisite {prerequisite['id']}: {diagnostic['declaration']} "
                        + ("is project-owned, not a library declaration." if project_owned else
                           "depends on forbidden axioms." if forbidden else "was not found in the built environment."),
                        "Use kind=declaration for this project witness; do not invent a self-edge." if project_owned else
                        "Replace forbidden dependencies with checked proofs, or correct the witness." if forbidden else
                        "Correct the exact witness name/import or implement it; retain the cited source obligation.")
                    if blocker not in blockers:
                        blockers.append(blocker)
                        issues.append(blocker["message"])
            if not matched and diagnostic["code"] in {"not_found", "not_project_owned"}:
                owners = {owner for owner, outputs in proposed.get("bindings", {}).items()
                          if any(output["declaration"] == diagnostic["declaration"] for output in outputs)}
                if owners:
                    checked_issue(f"Target {diagnostic['declaration']} " + (
                        "was not found in the built environment." if diagnostic["code"] == "not_found" else
                        "is not owned by a project source module."), "target_" + diagnostic["code"], owners,
                        "Correct the exact project output declaration/file; do not submit an imported library declaration as a project target.")
        return {"passed": False, "issues": list(dict.fromkeys([*issues, str(exc)])), "blockers": blockers, "targets": {},
                "project_declarations": getattr(exc, "project_declarations", [])}
    environment = environment_identity(root) if environment is None else environment
    if environment != contract["environment"]:
        checked_issue("toolchain or dependency environment changed from the formal contract", "contract_environment_changed",
                      set(), "Restore the bound toolchain/dependencies or explicitly revise the environment and recheck.")
    for name, previous in contract.get("external_declarations", {}).items():
        if external_records[name]["fingerprint"] != previous.get("fingerprint"):
            checked_issue(f"external prerequisite signature changed: {name}", "external_witness_changed",
                          {owner for row in contract["spec"]["prerequisites"]
                           if row["resolution"].get("declaration") == name for owner in row["needed_by"]},
                          "Restore the pinned library identity or explicitly revise the prerequisite evidence and recheck.")
    for name, previous in contract.get("prerequisite_declarations", {}).items():
        if witness_records[name]["fingerprint"] != previous.get("fingerprint"):
            for prerequisite in contract["spec"]["prerequisites"]:
                if prerequisite["resolution"].get("declaration") == name:
                    blocker = _prerequisite_blocker(prerequisite, "prerequisite_witness_changed",
                        f"Source prerequisite {prerequisite['id']} witness {name} changed its protected meaning.",
                        "Explicitly revise the affected representation or prerequisite resolution, then recheck its source correspondence.")
                    blockers.append(blocker)
                    issues.append(blocker["message"])
    native = _native_axioms(inspection["project_used_axioms"])
    if native:
        checked_issue("project uses native evaluation axioms: " + ", ".join(sorted(native)), "project_native_axioms",
                      set(), "Replace native-evaluation dependencies with kernel-checked proofs.")
    targets = inspection["targets"]
    if checked_complete - set(bindings):
        issues.append("completed tasks have no adopted output manifest")
    if final:
        if set(bindings) != task_ids or checked_complete != task_ids:
            checked_issue("not every source obligation has a complete Lean representation", "formal_tasks_incomplete",
                          task_ids - (set(bindings) & checked_complete), "Complete the missing source-obligation outputs.")
        if inspection["project_axioms"]:
            checked_issue("project retains custom axioms: " + ", ".join(inspection["project_axioms"]), "project_custom_axioms",
                          set(), "Replace the named project axioms with proved declarations.")
        if inspection["project_sorries"]:
            checked_issue("project retains proof holes: " + ", ".join(inspection["project_sorries"]), "project_proof_holes",
                          set(), "Complete the named proof holes or remove genuinely obsolete drafts without dropping source coverage.")
    verified_targets = {}
    for task in actual_tasks:
        name, owner = task["lean_decl"], task["task_id"]
        row = targets[name]
        fingerprint = digest(_semantic_record(row, fingerprint_version=fingerprint_version))
        if row["module"] != module_for_file(root, task["lean_file"], layout["modules"]):
            checked_issue(f"candidate declaration {name} is not in {task['lean_file']}", "target_wrong_file", {owner},
                          "Correct the output file mapping; explicitly reopen adopted representations before relocating them.")
        if name in contract["targets"] and fingerprint != contract["targets"][name].get("fingerprint"):
            checked_issue(f"contract changed for {name}: type, definition, or declaration identity differs",
                          "target_meaning_changed", {owner},
                          "Restore the protected meaning or explicitly reopen the representation and review its source correspondence.")
        if row["target_kind"] == "axiom":
            checked_issue(f"candidate declaration {name} is an axiom", "target_is_axiom", {owner}, "Prove the declaration.")
        if '"sorryAx"' in json.dumps(_semantic_record(row)):
            checked_issue(f"{name} has a proof hole in its type or meaning-bearing definitions", "target_meaning_hole", {owner},
                          "Implement the type/definition completely; representation-stage proof holes are only permitted in theorem proofs.")
        allowed = AXIOMS | ({"sorryAx"} if owner not in checked_complete and row["target_kind"] == "theorem" else set())
        unexpected = set(row["axioms"]) - allowed
        if unexpected:
            checked_issue(f"{name} depends on forbidden axioms: {', '.join(sorted(unexpected))}", "target_forbidden_axioms", {owner},
                          "Complete the proof and replace forbidden dependencies with kernel-checked proofs.")
        proposed["targets"][name] = {"target_kind": row["target_kind"], "module": row["module"],
                                     "fingerprint": fingerprint,
                                     "meaning_dependencies": sorted(row.get("meanings", {})),
                                     "verification_dependencies": sorted(row.get("proof_dependencies", []))}
        if owner in checked_complete:
            verified_targets[name] = fingerprint
    proposed["external_declarations"] = external_records
    if witnesses or "prerequisite_declarations" in contract:
        proposed["prerequisite_declarations"] = witness_records
    return {"passed": not issues, "issues": issues, "blockers": blockers, "targets": targets,
            "project_declarations": inspection.get("project_declarations", []),
            "compiled_receipt": inspection.get("compiled_receipt"),
            "external_declarations": external_records,
            "prerequisite_declarations": witness_records,
            "verified_tasks": sorted(checked_complete), "verified_targets": verified_targets,
            "final": final,
            **({"proposed_contract": _seal_contract(proposed)} if not issues else {})}


def check_formal_contract(root: Path, contract: dict, tasks: list[dict],
                          *, completed: set[str], layout: dict | None = None,
                          environment: dict | None = None, timings: dict | None = None,
                          proposed_outputs: list[dict] | None = None, task_id: str | None = None,
                          stage: str = "complete", final: bool = False) -> dict:
    """Check exact adopted meaning; new representations extend it only on success."""
    body = {key: value for key, value in contract.items() if key not in {"sha256", "artifact_id"}}
    if not contract or digest(body) != contract.get("sha256"):
        return {"passed": False, "issues": ["formal contract is missing or corrupt"], "targets": {}}
    if contract.get("fingerprint_version", 1) not in {1, 2}:
        return {"passed": False, "issues": ["unsupported semantic fingerprint version"], "targets": {}}
    if contract.get("version") not in {2, 3} or not isinstance(contract.get("spec"), dict):
        return {"passed": False, "issues": ["formal contract lacks source-evidence metadata; re-chunk it"], "targets": {}}
    if digest(contract["spec"]) != contract.get("spec_sha256"):
        return {"passed": False, "issues": ["formal contract spec digest is inconsistent"], "targets": {}}
    if contract.get("version") == 3:
        return _check_incremental_contract(
            root, contract, tasks, completed=completed, proposed_outputs=proposed_outputs,
            task_id=task_id, stage=stage, final=final, layout=layout,
            environment=environment, timings=timings,
        )
    if any(row["resolution"]["kind"] in {"declaration", "argument"}
           for row in contract["spec"]["prerequisites"]):
        return {"passed": False, "issues": ["declaration/argument evidence requires a version-3 source contract"],
                "blockers": [], "targets": {}}
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
            "compiled_receipt": inspection.get("compiled_receipt"),
            "project_declarations": inspection.get("project_declarations", []),
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


def _prerequisite_evidence(contract: dict) -> dict:
    return {name: {key: row[key] for key in ("fingerprint", "module", "signature", "axioms")}
            for name, row in contract.get("prerequisite_declarations", {}).items()}


def snapshot_is_current(paths, state: dict, snapshot: dict, *, require_complete: bool = True) -> bool:
    from .autoformalize_input import source_matches
    from .autoformalize_representation import snapshot as representation_snapshot
    from .autoformalize_state import repair_digest

    if not snapshot:
        return False
    current = source_identity(paths.project_root)
    formal = state["formalization"]
    contract = formal.get("contract") or {}
    source = state.get("input_source") or {}
    return (
        snapshot.get("policy_sha256") == policy_hash()
        and (snapshot.get("passed") is not True
             or autoformalize_cache.compiled_receipt_current(paths.project_root, snapshot.get("compiled_receipt")))
        and current["main_sha"] == snapshot.get("main_sha") == formal.get("main_sha")
        and current["source_sha256"] == snapshot.get("source_sha256")
        and current["environment"] == snapshot.get("environment")
        and contract.get("sha256") == snapshot.get("contract_sha256")
        and contract.get("version") in {2, 3}
        and (not require_complete or (bool(state["formal_tasks"])
             and all(task.get("status") == "complete" for task in state["formal_tasks"].values())))
        and snapshot.get("task_statuses") == {
            key: task.get("status") for key, task in state["formal_tasks"].items()}
        and (contract.get("version") != 3
             or (contract.get("sha256") == _seal_contract(contract)["sha256"]
                 and {row["lean_decl"]: row["task_id"] for row in _binding_tasks(contract)}
                 == snapshot.get("declarations")))
        and contract.get("spec_sha256") == snapshot.get("spec_sha256")
        == digest(formal.get("spec")) == digest(contract.get("spec"))
        and snapshot.get("repairs_sha256") == repair_digest(state)
        and snapshot.get("external_declarations") == _external_evidence(contract)
        and snapshot.get("prerequisite_declarations", {}) == _prerequisite_evidence(contract)
        and snapshot.get("representation_reviews", {}) == representation_snapshot(state)
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
    from .autoformalize_representation import snapshot as representation_snapshot
    from .autoformalize_state import interface_available, repair_digest

    root = paths.project_root
    formal = state["formalization"]
    contract = formal.get("contract") or {}
    before = source_identity(root)
    tasks = list(state["formal_tasks"].values())
    last = next((candidate for candidate in reversed(list(state["formal_candidates"].values()))
                 if candidate.get("status") == "merged" and candidate.get("main_sha") == before["main_sha"]), {})
    verification = last.get("verification") or {}
    complete_ids = set(state["formal_tasks"])
    build_reusable = (verification.get("status") == "passed"
                      and verification.get("policy_sha256") == policy_hash()
                      and verification.get("source_identity") == before
                      and last.get("build", {}).get("returncode") == 0)
    reusable = (build_reusable
                and autoformalize_cache.compiled_receipt_current(root, verification.get("compiled_receipt"))
                and verification.get("contract_sha256") == formal.get("contract", {}).get("sha256")
                and set(verification.get("verified_tasks", [])) == complete_ids)
    if contract.get("version") == 3:
        expected_targets = {name: row["fingerprint"] for name, row in contract.get("targets", {}).items()}
        reusable = (reusable and verification.get("final") is True
                    and verification.get("verified_targets") == expected_targets
                    and set(contract.get("bindings", {})) == complete_ids)
    if reusable:
        build = {"returncode": 0, "reused_candidate": last["candidate_id"]}
        check = {"passed": True, "issues": [], "targets": {},
                 "compiled_receipt": verification["compiled_receipt"],
                 "reused_verification": verification.get("artifact_id")}
    else:
        # Even if a plan's evidence metadata changed, exactly unchanged checked
        # source needs no second build. Its current source contract is inspected
        # afresh, including global holes and coverage, before semantic review.
        build = ({"returncode": 0, "reused_candidate": last["candidate_id"]}
                 if contract.get("version") == 3 and build_reusable else build_sources(root, full=True))
        check = (check_formal_contract(root, formal.get("contract", {}), tasks,
                                       completed=complete_ids, final=True)
                 if not build["returncode"] else
                 {"passed": False, "issues": ["final project build failed"], "targets": {}})
    blockers = list(check.get("blockers", []))
    for blocker in prerequisite_blockers(contract, completed={task["task_id"] for task in tasks
                                                               if task.get("status") == "complete"}, final=True):
        if blocker not in blockers:
            blockers.append(blocker)
    issues = list(dict.fromkeys([*check["issues"], *(row["message"] for row in blockers)]))
    if (contract.get("version") not in {2, 3} or not isinstance(formal.get("spec"), dict)
            or digest(formal.get("spec")) != contract.get("spec_sha256")
            or digest(contract.get("spec")) != contract.get("spec_sha256")):
        issues.append("source-evidence spec is missing or inconsistent; re-chunk it")
    if any(task.get("status") != "complete" for task in tasks) or not tasks:
        issues.append("formal tasks are incomplete")
    if contract.get("representation_review_policy") == 1:
        unreviewed = sorted(task["task_id"] for task in tasks if not interface_available(state, task))
        if unreviewed:
            issues.append("representation review is not aligned for tasks: " + ", ".join(unreviewed))
    if contract.get("version") == 3:
        if contract.get("sha256") != _seal_contract(contract)["sha256"]:
            issues.append("source contract is corrupt")
        if (set(contract.get("obligation_ids", [])) != complete_ids
                or set(contract.get("bindings", {})) != complete_ids
                or any(not outputs for outputs in contract.get("bindings", {}).values())):
            issues.append("source obligations lack adopted Lean outputs")
    if source_identity(root) != before:
        issues.append("source changed during final mechanical verification")
    if before["main_sha"] != formal.get("main_sha"):
        issues.append("main differs from the recorded accepted candidate revision")
    if not _problem_matches(paths, state):
        issues.append("original problem changed or is missing; start a fresh run")
    if not source_matches(paths, state):
        issues.append("supplied source bytes changed or are missing")
    # Resolution-only graph refinements do not change Lean statements/proofs.
    # The final fresh check can bind their new external evidence without making
    # a worker submit an artificial source diff. The state publisher must CAS
    # against this base digest and the exact source identity before adopting it.
    review_contract = check.get("proposed_contract", contract) if not issues else contract
    extension = ({"proposed_contract": review_contract, "base_contract_sha256": contract["sha256"]}
                 if not issues and review_contract.get("sha256") != contract.get("sha256") else {})
    report = {
        **before,
        "policy_sha256": policy_hash(),
        "compiled_receipt": check.get("compiled_receipt"),
        "passed": not issues,
        "issues": issues,
        "blockers": blockers,
        "solution_candidate": formal["solution_candidate"],
        "solution_sha256": formal["solution_sha256"],
        "formalization_revision": formal["revision"],
        "contract_sha256": review_contract.get("sha256"),
        "spec_sha256": digest(formal.get("spec")),
        "repairs_sha256": repair_digest(state),
        "external_declarations": _external_evidence(review_contract),
        "prerequisite_declarations": _prerequisite_evidence(review_contract),
        "representation_reviews": representation_snapshot(state),
        "accepted_candidates": {task["task_id"]: task.get("accepted_candidate") for task in tasks},
        "task_statuses": {task["task_id"]: task.get("status") for task in tasks},
        "declarations": {task["lean_decl"]: task["task_id"] for task in
                         (_binding_tasks(contract) if contract.get("version") == 3 else tasks)},
        "build": build,
        "targets": check["targets"],
        **extension,
    }
    report["snapshot_id"] = "review-" + uuid.uuid4().hex
    artifact = artifacts.store_text(paths.artifacts, json.dumps(report, sort_keys=True) + "\n",
                                    kind="autoformalize_machine_review", producer="Unity")
    # Keep telemetry out of shared prompt memory. Detail remains in the artifact.
    return {key: value for key, value in report.items() if key not in {"build", "targets"}} | {
        "artifact_id": artifact["artifact_id"],
    }
