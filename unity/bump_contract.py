"""Bump-only formal contracts and exact-source mechanical review snapshots.

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

from . import artifacts, bump_cache, bump_jobs, bump_native, bump_workspace
from .bump_spec import library_declarations, normalize_outputs, normalize_requirements, normalize_spec, task_spec_hash


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
    """Bind reuse to the current bump checking implementation."""
    directory = Path(__file__).parent
    policy = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in (
        "bump_contract.py", "bump_contract.lean",
        "bump_spec.py", "bump_runtime.py", "bump_state.py",
        "bump_workspace.py", "bump_workspace.lean",
        "bump_native.py", "bump_jobs.py", "bump_cache.py",
        "bump_project.py", "bump_delta.py", "bump_scope.py", "bump_files.py", "bump_input.py",
        "bump_migration_contract.py", "bump_inspect.lean", "bump_migration_project.py", "bump_bootstrap.py",
        "bump_planner.py", "bump_diagnostics.py",
        "bump_migration_defaults.lean",
        "bump_checker_v2.py", "bump_inventory.py", "bump_inventory.lean",
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


def _dependency_directory_name(serialized: str) -> str:
    """Decode Lake's JSON Name into its materialized directory name, without Lake.

    Lake's PackageEntry.fromJson? uses Lean's String.toName; dirName then uses
    Name.toString (escape := false). In particular, quoted dots are literal
    dots, not directory separators. Keep this pure: pinned-input checks run
    before it is safe to execute a project's Lake configuration.
    """
    if not isinstance(serialized, str) or not serialized or serialized == "[anonymous]":
        raise ValueError("invalid or anonymous Lake package name")

    def letter_like(char: str) -> bool:
        code = ord(char)
        return (
            (0x3B1 <= code <= 0x3C9 and code != 0x3BB)
            or (0x391 <= code <= 0x3A9 and code not in (0x3A0, 0x3A3))
            or any(low <= code <= high for low, high in (
                (0x3CA, 0x3FB), (0x1F00, 0x1FFE), (0x2100, 0x214F),
                (0x1D49C, 0x1D59F), (0x0100, 0x017F),
            ))
            or (0xC0 <= code <= 0xFF and code not in (0xD7, 0xF7))
        )

    def first(char: str) -> bool:
        return "a" <= char <= "z" or "A" <= char <= "Z" or char == "_" or letter_like(char)

    def rest(char: str) -> bool:
        code = ord(char)
        return (first(char) or "0" <= char <= "9" or char in "'!?"
                or any(low <= code <= high for low, high in (
                    (0x2080, 0x2089), (0x2090, 0x209C), (0x1D62, 0x1D6A),
                )) or code == 0x2C7C)

    parts = []
    index = 0
    while index < len(serialized):
        start = index
        if serialized[index] == "«":
            end = serialized.find("»", index + 1)
            if end < 0:
                raise ValueError("unterminated escaped Lake name component")
            parts.append(serialized[index + 1:end])
            index = end + 1
        elif "0" <= serialized[index] <= "9":
            while index < len(serialized) and "0" <= serialized[index] <= "9":
                index += 1
            parts.append(serialized[start:index].lstrip("0") or "0")
        elif first(serialized[index]):
            index += 1
            while index < len(serialized) and rest(serialized[index]):
                index += 1
            parts.append(serialized[start:index])
        else:
            raise ValueError("invalid Lake name component")
        if index == len(serialized):
            break
        if serialized[index] != "." or index + 1 == len(serialized):
            raise ValueError("invalid Lake name separator")
        index += 1

    directory = ".".join(parts)
    if (not directory or directory in {".", ".."} or directory in _EXCLUDED
            or any(char in "/\\" or ord(char) < 32 or 0x7F <= ord(char) <= 0x9F
                   or 0xD800 <= ord(char) <= 0xDFFF for char in directory)):
        raise ValueError("unsafe Lake package directory name")
    return directory


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
    decoded = []
    directories = set()
    for package in manifest.get("packages", []):
        try:
            name = package["name"]
            directory_name = _dependency_directory_name(name)
            if directory_name in directories:
                raise ValueError("ambiguous duplicate Lake package directory name")
            directories.add(directory_name)
            decoded.append((package, name, directory_name))
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractEnvironmentError(f"Cannot decode Lake dependency name: {exc}") from exc
    result = {}
    for package, name, directory_name in decoded:
        if package.get("type") == "path":
            directory = root / package["dir"]
        else:
            directory = package_dir / directory_name
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
    from . import bump_migration_project
    version = bump_migration_project._run(root, ["lake", "env", "lean", "--version"])
    prefix = bump_migration_project._run(root, ["lake", "env", "lean", "--print-prefix"])
    sysroot = Path(prefix.stdout.strip())
    if version.returncode or prefix.returncode or not sysroot.is_absolute() or not (sysroot / "bin" / "lean").is_file():
        raise ValueError("cannot determine Lean toolchain identity")
    return {
        "lean_version": version.stdout.strip(),
        "lean_sysroot": str(sysroot.resolve()),
        "lean_binary_sha256": hashlib.sha256((sysroot / "bin" / "lean").read_bytes()).hexdigest(),
        "config": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                   for name in sorted(_CONFIGS) if (root / name).exists()},
        # Worktrees have distinct physical dependency paths, not distinct source identities.
        "dependencies": {name: {k: v for k, v in row.items() if k != "path"}
                         for name, row in _dependencies(root).items()},
    }


def source_identity(root: Path, *, layout: dict | None = None) -> dict:
    """Bind source bytes AND Lake ownership; only reuse layout within a candidate.

    Fresh boundary calls rediscover the layout and fingerprint dependencies. The
    versioned hash also invalidates pre-layout-bound verification receipts.
    """
    layout = workspace_layout(root) if layout is None else layout
    # This identity always covers the WHOLE owned workspace and all input
    # bytes. Scope is independently sealed into the baseline/contract and the
    # build/inspection receipts, not inferred from a caller's layout hint.
    ownership = {key: value for key, value in layout.items() if key not in {
        "verification_modules", "editable_modules", "project_scope", "scope_sha256"}}
    return {"main_sha": _git(root, "rev-parse", "HEAD"),
            "source_sha256": digest({"version": 2, "workspace": ownership,
                                     "files": _file_hashes(root, build_dir=layout["build_dir"])}),
            "environment": environment_identity(root)}


def workspace_layout(root: Path) -> dict:
    executable = bump_workspace._executable(root)
    layout = bump_workspace.discover(root, ["--layout-only"], executable=executable)
    files = [str(path.relative_to(root))
             for path in source_files(root, build_dir=layout["build_dir"])
             if path.suffix == ".lean" and path.name != "lakefile.lean"]
    data = bump_workspace.discover(root, files, executable=executable)
    if data.get("issues") or not isinstance(data.get("modules"), dict):
        raise ValueError("Lake module inspection returned errors")
    return data


def workspace_modules(root: Path) -> dict[str, str]:
    return workspace_layout(root)["modules"]


def scoped_layout(root: Path, layout: dict, baseline: dict | None) -> dict:
    """Apply only controller-sealed scope; keep full ownership discoverable."""
    if baseline and baseline.get("project_scope") == "changes":
        from . import bump_delta
        return bump_delta.apply(root, layout, baseline)
    if not baseline or "verification_scope" not in baseline:
        return layout
    from . import bump_scope
    return bump_scope.apply(root, layout, baseline["verification_scope"])


def _verification_modules(layout: dict) -> dict[str, str]:
    return layout.get("verification_modules", layout["modules"])


def _editable_modules(layout: dict) -> dict[str, str]:
    return layout.get("editable_modules", layout["modules"])


def _migration_project_verification(baseline: dict) -> dict:
    """Pure exact coverage shape; callers separately validate baseline authority."""
    migration = baseline["migration"]
    scope = baseline["build_scope"]
    return {"mode": "migration", "policy": "migration-v1", "inspection_policy": 4, "occurrence_policy": 1,
                "scope_policy": 1, "scope_sha256": scope["sha256"], "project_scope": scope["mode"],
                "baseline_sha256": baseline["sha256"],
                "original_source_commit": migration["source_commit"],
                "original_source_sha256": migration["source_hash"],
                "target_version": migration["target_version"],
                "verification_modules": {row["path"]: name for name, row in baseline["compiler_modules"].items()},
                "byte_only_modules": {path: name for name, path in scope["excluded_modules"].items()},
                "byte_preserved_files": copy.deepcopy(scope["excluded_files"]),
                "contexts": sorted(baseline["compiler_modules"]),
                "declaration_occurrences": {key: {"module": row["module"],
                    "declaration": row["declaration"], "native_name": copy.deepcopy(row["native_name"])}
                    for key, row in baseline["declarations"].items()},
                "normal_default_build": True}


def project_verification(root: Path, baseline: dict, *, tasks: list[dict] | None = None) -> dict | None:
    if baseline.get("policy") == "migration-v2":
        return {"mode": "migration", "migration_policy": 2, "inspection_policy": 5,
            "scope_policy": 1, "occurrence_policy": 1, "baseline_sha256": baseline["sha256"],
            "scope_sha256": baseline["build_scope"]["sha256"],
            "original_index_ref": baseline["original_index_ref"],
            "original_index_sha256": baseline["original_index_sha256"],
            "occurrence_count": baseline["occurrence_count"],
            "selected_modules": {name: row["path"] for name, row in baseline["compiler_modules"].items()},
            "excluded_files": baseline["build_scope"]["excluded_files"],
            "upstream_compatibility_assumption": True,
            "recursive_upstream_ast_equality": False}
    """Current controller-derived coverage, never an agent's coverage claim."""
    if baseline.get("policy") == "migration-v1":
        from . import bump_project
        if not bump_project.baseline_is_valid(baseline):
            raise ValueError("migration coverage requires a sealed scope-aware baseline")
        return _migration_project_verification(baseline)
    if baseline.get("project_scope") == "changes":
        layout = workspace_layout(root)
        if tasks:
            from . import bump_delta
            layout = bump_delta.apply(root, layout, baseline, tasks=tasks)
        else:
            layout = scoped_layout(root, layout, baseline)
        verified = _verification_modules(layout)
        return {"mode": "changes", "policy": "changes-v1", "inspection_policy": 2,
                "baseline_sha256": baseline["sha256"],
                "verification_modules": dict(verified),
                "byte_only_modules": {path: module for path, module in layout["modules"].items()
                                      if path not in verified},
                "contexts": sorted(set(verified.values())), "normal_default_build": True}
    if "verification_scope" not in baseline:
        return None
    layout = scoped_layout(root, workspace_layout(root), baseline)
    verified, editable = _verification_modules(layout), _editable_modules(layout)
    return {
        "mode": "libraries", "scope_sha256": baseline["verification_scope"]["sha256"],
        "selected_libraries": list(baseline["verification_scope"]["selected_libraries"]),
        "verification_modules": dict(verified), "editable_modules": dict(editable),
        "readonly_imported_modules": {path: module for path, module in verified.items() if path not in editable},
        "byte_only_modules": {path: module for path, module in layout["modules"].items() if path not in verified},
    }


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


def inspect_module_context(root: Path, module: str, *, layout: dict,
                           tasks: list[dict] | None = None, inventory_only: bool = True) -> dict:
    """Inspect one real import environment, never unrelated sibling modules."""
    return inspect_environment(root, tasks or [], layout=layout, module_context=[module],
                               _inventory_only=inventory_only)


def _inspect_contexts(root: Path, tasks: list[dict], *, layout: dict, timings: dict | None,
                      external_declarations: list[str] | None,
                      prerequisite_declarations: list[str] | None, inventory_only: bool) -> dict:
    grouped = {}
    for task in tasks:
        module = module_for_file(root, task["lean_file"], layout["modules"])
        grouped.setdefault(module, []).append(task)
    # Downstream elaboration can change without changing its source bytes.
    # Its actual compiled context therefore belongs in the same receipt as
    # submitted outputs, even when it has no output declaration of its own.
    for module in _verification_modules(layout).values():
        grouped.setdefault(module, [])
    if not grouped:
        raise ValueError("change-focused inspection has no requested module contexts")
    result = {"targets": {}, "external_declarations": {}, "prerequisite_declarations": {},
              "contexts": {}, "project_declarations": [], "project_axioms": [],
              "project_sorries": [], "project_used_axioms": [], "compiled_modules": [],
              "imported_modules": [], "issues": [], "inspection_policy": 2}
    receipts = []
    for module, selected in sorted(grouped.items()):
        context_timings = {} if timings is not None else None
        if timings is not None:
            timings.setdefault("contexts", {})[module] = context_timings
        data = inspect_environment(root, selected, layout=layout, timings=context_timings,
            external_declarations=external_declarations,
            prerequisite_declarations=prerequisite_declarations, module_context=[module],
            _inventory_only=inventory_only or not selected, _optional_evidence=True)
        result["contexts"][module] = data
        # Targets remain unique manifest identities. Preservation inventories
        # never use this name-only merge: each stays in its original context.
        for field in ("targets", "external_declarations", "prerequisite_declarations"):
            for name, row in data[field].items():
                if name in result[field] and result[field][name] != row:
                    raise ContractInspectionError(f"ambiguous {field} witness {name} across module contexts")
                result[field][name] = row
        result["project_declarations"].extend({**row, "context": module}
                                               for row in data["project_declarations"])
        for field in ("project_axioms", "project_sorries", "project_used_axioms",
                      "compiled_modules", "imported_modules"):
            result[field] = sorted(set(result[field]) | set(data[field]))
        receipts.append(data.get("compiled_receipt"))
    for field, requested in (("external_declarations", external_declarations or []),
                             ("prerequisite_declarations", prerequisite_declarations or [])):
        missing = sorted(set(requested) - set(result[field]))
        if missing:
            raise ContractInspectionError("requested evidence not found in any actual output context: "
                + ", ".join(missing), [{"declaration": name, "code": "not_found"} for name in missing])
    # Union only independently verified compiled input receipts, not unrelated
    # declaration environments. Missing receipts disable reuse, not inspection.
    combined = {}
    for receipt in receipts:
        if not bump_cache.compiled_receipt_current(root, receipt):
            combined = {}
            break
        payload = artifacts.artifact_bytes(root / ".unity" / "artifacts", receipt["artifact_id"])
        for path, record in json.loads(payload).items():
            if path in combined and combined[path] != record:
                raise ContractInspectionError("compiled input changed between module contexts")
            combined[path] = record
    result["compiled_receipt"] = bump_cache.compiled_receipt(root, combined or None)
    return result


def inspect_environment(root: Path, tasks: list[dict], *, layout: dict | None = None,
                        timings: dict | None = None,
                        external_declarations: list[str] | None = None,
                        prerequisite_declarations: list[str] | None = None,
                        _compiled_before: dict | None = None,
                        _inventory_only: bool = False,
                        module_context: list[str] | None = None,
                        _optional_evidence: bool = False) -> dict:
    # New change-focused baselines inspect actual contexts independently;
    # explicit legacy all/library scopes retain their saved import semantics.
    layout = workspace_layout(root) if layout is None else layout
    if layout.get("project_scope") == "changes" and module_context is None:
        return _inspect_contexts(root, tasks, layout=layout, timings=timings,
            external_declarations=external_declarations,
            prerequisite_declarations=prerequisite_declarations, inventory_only=_inventory_only)
    if module_context is not None and (len(module_context) != 1
            or module_context[0] not in layout["modules"].values()):
        raise ValueError("inspection requires one actual project-owned module context")
    modules = sorted(set(module_context if module_context is not None else _verification_modules(layout).values()))
    owned = sorted(set(layout["modules"].values())) if module_context is not None else modules
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
    if (not names and not _inventory_only) or not modules:
        raise ValueError("formal contract has no declarations/modules")
    native_timings = {} if timings is not None else None
    job_timings = {} if timings is not None else None
    if timings is not None:
        timings["native_helper"] = native_timings
        timings["inspector_job"] = job_timings
    executable = bump_native.executable(
        root, Path(__file__).with_suffix(".lean"), name="contract", timings=native_timings,
    )
    cache_key, cache_identity, cache_before, cached = None, None, None, None
    # Real projects have Git identity. Incomplete environments and unavailable
    # cache observations always use the normal inspector, never fail open.
    if (root / ".git").exists():
        try:
            cache_identity = source_identity(root, layout=layout)
            cache_key = digest({"version": 2, "source": cache_identity["source_sha256"],
                "environment": cache_identity["environment"], "policy": policy_hash(),
                "executable": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "modules": modules, "targets": sorted(names), "externals": externals, "witnesses": witnesses,
                "owned": owned, "optional_evidence": _optional_evidence,
                "scope_sha256": layout.get("scope_sha256"),
                "inventory_only": _inventory_only})
            cached, cache_before = bump_cache.lookup(root, cache_key)
            if _compiled_before is not None:
                cached, cache_before = None, _compiled_before
        except (OSError, ValueError, KeyError, TypeError):
            cache_key = None
    if timings is not None:
        timings["inspection_cache_hit"] = cached is not None
    with measure(timings, "inspector_seconds"):
        result = (subprocess.CompletedProcess([], 0, json.dumps(cached), "") if cached is not None else bump_jobs.run(
            root, ["lake", "env", str(executable), *modules, "--", *names,
                   *(["--external", *externals] if externals else []),
                   *(["--prerequisite", *witnesses] if witnesses else []),
                   *(["--owned", *owned] if module_context is not None else []),
                   *(["--optional-evidence"] if _optional_evidence else []),
                   *(["--inventory-only"] if _inventory_only else [])],
            cwd=root, owner="Unity", task_id="contract", serialize_build=True,
            timings=job_timings,
        ))
    inventory = []
    def failure(message: str, declaration_errors: list[dict] | None = None) -> ValueError:
        record = artifacts.store_text(
            root / ".unity" / "artifacts",
            json.dumps({"returncode": result.returncode, "stdout": result.stdout,
                        "stderr": result.stderr}, ensure_ascii=False),
            kind="bump_inspection", producer="Unity", source="formal contract inspector",
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
    # Failed native imports deliberately return a small error envelope. Show
    # that cause before validating fields which only successful imports have.
    issues = data.get("issues", [])
    if not isinstance(issues, list) or any(not isinstance(issue, str) for issue in issues):
        raise failure("formal contract inspector returned invalid issues")
    declaration_errors = data.get("declaration_errors", [])
    if (not isinstance(declaration_errors, list) or any(
            not isinstance(row, dict) or set(row) != {"declaration", "code"}
            or row["declaration"] not in set(names) | set(externals) | set(witnesses)
            or row["code"] not in {"not_found", "project_owned", "not_project_owned"} for row in declaration_errors)):
        raise failure("formal contract inspector returned invalid prerequisite diagnostics")
    if issues or result.returncode:
        inventory = data.get("project_declarations", [])
        if not isinstance(inventory, list):
            inventory = []
        raise failure("formal contract inspection failed: " + artifacts.preview_text(
            "; ".join(issues) or result.stderr or f"inspector exited {result.returncode} without reporting issues", 2000),
            declaration_errors)
    if layout.get("project_scope") in {"libraries", "changes"}:
        # Check actual kernel imports too: a prebuilt artifact must not smuggle
        # an excluded owned module into the native environment as a dependency.
        imported = data.get("imported_modules")
        if (not isinstance(imported, list)
                or any(not isinstance(name, str) for name in imported)
                or not set(modules) <= set(imported)):
            raise failure("scoped inspection omitted the actual imported module identities")
        allowed_owned = owned if module_context is not None else modules
        outside = set(imported).intersection(layout["modules"].values()) - set(allowed_owned)
        if outside:
            raise failure("native imports cross the frozen project scope: " + ", ".join(sorted(outside)))
        compiled = data.get("compiled_modules")
        if (not isinstance(compiled, list) or len(compiled) != len(imported)
                or len(set(imported)) != len(imported)
                or any(not isinstance(path, str) or not Path(path).is_absolute()
                       or not path.endswith(".olean") for path in compiled)):
            raise failure("scoped inspection omitted native imported-module provenance")
        # Names alone cannot classify unmatched project files or stale local
        # artifacts as external dependencies. Use the native resolver's actual
        # paths, exempting only manifest-pinned dependency directories.
        dependencies = (cache_identity["environment"]["dependencies"] if cache_identity is not None
                        else _dependencies(root))
        dependency_roots = [Path(row["path"]).resolve() for row in dependencies.values()]
        project_root, build_root = root.resolve(), (root / layout["build_dir"]).resolve()
        for module, filename in zip(imported, compiled):
            path = Path(filename).resolve()
            in_build = path.is_relative_to(build_root)
            in_project = path.is_relative_to(project_root) and not any(
                path.is_relative_to(directory) for directory in dependency_roots)
            if (module not in allowed_owned and (in_build or in_project)) or (module in allowed_owned and not in_build):
                raise failure("native module provenance crosses the frozen project scope: " + module)
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
    if set(data.get("targets", {})) != set(names):
        raise failure("formal contract inspection incomplete: target declarations do not match")
    external_records = data.get("external_declarations", {})
    if (not isinstance(external_records, dict)
            or (set(external_records) - set(externals) if _optional_evidence
                else set(external_records) != set(externals))):
        raise failure("formal contract inspection omitted requested external declarations")
    for name, row in external_records.items():
        if (not isinstance(row, dict) or row.get("name") != name
                or not isinstance(row.get("module"), str) or not row["module"]
                or row["module"] in owned or not isinstance(row.get("type"), list)
                or row.get("target_kind") not in {"theorem", "def", "opaque", "inductive", "constructor", "recursor", "quot", "axiom"}
                or not isinstance(row.get("level_params"), list)
                or not isinstance(row.get("signature"), str) or not row["signature"].strip()
                or not isinstance(row.get("axioms"), list)):
            raise failure(f"incomplete kernel evidence for external declaration {name}")
    data["external_declarations"] = external_records
    witness_records = data.get("prerequisite_declarations", {})
    if (not isinstance(witness_records, dict)
            or (set(witness_records) - set(witnesses) if _optional_evidence
                else set(witness_records) != set(witnesses))):
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
    if _inventory_only:
        records = data.get("project_records")
        if (not isinstance(records, dict) or set(records) != {row["name"] for row in inventory}
                or any(not isinstance(row, dict) or row.get("name") != name
                       or not isinstance(row.get("type"), list)
                       or not isinstance(row.get("level_params"), list)
                       or not isinstance(row.get("is_internal_detail"), bool)
                       or not isinstance(row.get("direct_dependencies"), list)
                       or any(not isinstance(dep, str) for dep in row.get("direct_dependencies", []))
                       or not isinstance(row.get("declaration_meaning"), dict)
                       or "proof_body" not in row
                       or (row["proof_body"] is not None and not isinstance(row["proof_body"], list))
                       or row.get("module") not in modules
                       or not any(item["name"] == name and item["module"] == row.get("module")
                                  and item["kind"] == row.get("target_kind") for item in inventory)
                       or row.get("target_kind") not in {
                           "theorem", "def", "opaque", "inductive", "constructor", "recursor", "quot", "axiom"}
                       for name, row in records.items())):
            raise failure("formal contract inspector omitted complete project preservation records")
    if timings is not None:
        timings["kernel_ms"] = data.get("timings_ms", {})
    compiled = cache_before if cached is not None else None
    if cache_key is not None and cached is None:
        try:
            if source_identity(root) == cache_identity:
                compiled = bump_cache.publish(root, cache_key, data, cache_before)
        except bump_cache.CacheUnavailable as exc:
            if _compiled_before is None:
                # Cache storage is optional. Only when its path hint cannot be
                # saved, import once more with this known closure hashed first.
                if timings is not None:
                    timings["cache_unavailable_reinspection"] = True
                return inspect_environment(root, tasks, layout=layout, timings=timings,
                    external_declarations=external_declarations,
                    prerequisite_declarations=prerequisite_declarations, _compiled_before=exc.identity,
                    _inventory_only=_inventory_only, module_context=module_context,
                    _optional_evidence=_optional_evidence)
        except (OSError, ValueError):
            pass
    if module_context is not None and compiled is None and _compiled_before is None and cache_key is not None:
        # The shared import hint may describe a different sibling context. Pin
        # this actual closure and inspect once more so multi-context checks can
        # obtain receipts without requiring a later candidate to warm a cache.
        try:
            observed = bump_cache.compiled_identity(data["compiled_modules"])
        except (OSError, ValueError, KeyError, TypeError):
            observed = None
        if observed is not None:
            return inspect_environment(root, tasks, layout=layout, timings=timings,
                external_declarations=external_declarations,
                prerequisite_declarations=prerequisite_declarations, _compiled_before=observed,
                _inventory_only=_inventory_only, module_context=module_context,
                _optional_evidence=_optional_evidence)
    data["compiled_receipt"] = bump_cache.compiled_receipt(root, compiled)
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
                  task_id: str = "contract", timings: dict | None = None,
                  baseline: dict | None = None, tasks: list[dict] | None = None) -> dict:
    if baseline and baseline.get("policy") in {"migration-v1", "migration-v2"}:
        from . import bump_project, bump_migration_project
        bump_project.require_pinned_inputs(root, baseline)
        graph = baseline["compiler_modules"]
        selected = sorted({t.get("migration_module", t.get("task_id", t.get("id"))) for t in (tasks or [])})
        if not selected and task_id in graph:
            selected = [task_id]
        if set(selected) - set(graph):
            raise ValueError("candidate build requested a module outside the migration contract")
        commands = ([None, sorted(graph)] if full and not selected else [selected or sorted(graph)])
        outputs = []
        for modules in commands:
            result = bump_migration_project.build(root, modules, scope=baseline["build_scope"])
            outputs.append(result["diagnostics"])
            if not result["passed"]:
                return {"returncode": result["returncode"] or 1, "output": "\n".join(outputs)}
        return {"returncode": 0, "output": "\n".join(outputs)}
    layout = workspace_layout(root) if layout is None else layout
    if baseline and baseline.get("project_scope") == "changes" and tasks:
        from . import bump_delta
        layout = bump_delta.apply(root, layout, baseline, tasks=tasks)
    else:
        layout = scoped_layout(root, layout, baseline)
    selected = _verification_modules(layout)
    modules = sorted(set(selected.values()))
    auxiliary = digest({name: value for name, value in _file_hashes(root, build_dir=layout["build_dir"]).items()
                        if Path(name).suffix != ".lean"})
    receipt_path = root / ".unity" / "bump-build-inputs.json"
    receipt = {"auxiliary": auxiliary, "modules": selected,
               **({"scope_sha256": layout["scope_sha256"]} if "scope_sha256" in layout else {})}
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
        for module, filename in layout["traces"].items():
            if module not in modules:
                continue
            trace = root / filename
            if trace.suffix != ".trace" or not trace.resolve().is_relative_to(directory):
                raise ValueError("unsafe project module trace path")
            trace.unlink(missing_ok=True)
    # Named module facets guarantee freshness even if a lakefile's default target
    # does not include the theorem module. The full default build is optional.
    # An unqualified default build can include explicitly excluded executables.
    # Module facets use Lake's normal dependency graph, so a required broken
    # build tool is still a real failure, never an ignored optional failure.
    if layout.get("project_scope") == "libraries":
        libraries = layout.get("libraries", [])
        if not libraries or not modules:
            raise ValueError("library-scoped build has no verified library modules")
        defaults = [["lake", "build", *libraries]] if full else []
    else:
        defaults = [["lake", "build"]] if full else []
    commands = defaults + (
        [["lake", "--rehash", "build", *[f"+{module}" for module in modules]]] if modules else [])
    outputs = []
    for command in commands:
        job_timings = {} if timings is not None else None
        stage = "default_build" if command in defaults else "module_build"
        if timings is not None:
            timings[stage] = job_timings
        result = bump_jobs.run(root, command, cwd=root, owner="Unity", task_id=task_id,
                                serialize_build=True, timings=job_timings)
        outputs.append(" ".join(command) + "\n" + result.stdout + "\n" + result.stderr)
        if result.returncode:
            return {"returncode": result.returncode, "output": "\n".join(outputs)}
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = receipt_path.with_name(f".bump-build-inputs-{uuid.uuid4().hex}.json")
    temporary.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    os.replace(temporary, receipt_path)
    return {"returncode": 0, "output": "\n".join(outputs)}


def freeze_formal_contract(paths, dag: dict) -> dict:
    """The legacy scaffold freezer must not replace an existing project."""
    raise ValueError("bump requires a version-3 source plan and existing-project baseline; "
                     "scaffold creation/checkpointing is not supported")


def _seal_contract(contract: dict) -> dict:
    body = {key: copy.deepcopy(value) for key, value in contract.items()
            if key not in {"sha256", "artifact_id"}}
    return {**body, "sha256": digest(body)}


def _contract_digest(contract: dict) -> str:
    """Recompute the exact contract seal without an unused evidence deep copy.

    Publication keeps its independent copy; validation reads every current
    nested value and never trusts a memoized seal or prior validation result.
    """
    return digest({key: value for key, value in contract.items()
                   if key not in {"sha256", "artifact_id"}})


def _baseline_matches(state: dict, contract: dict) -> bool:
    """Bind the accepted contract to the originally captured project context.

    Scope binding may add a one-time selected-target interpretation to the
    baseline, but may never silently capture a new original project.
    """
    from . import bump_project

    original = state.get("project_baseline") or {}
    if contract.get("migration_policy") == 2 or original.get("policy") == "migration-v2":
        return (contract.get("version") == 4 and contract.get("migration_policy") == 2
            and contract.get("inspection_policy") == 5 and bump_project.baseline_is_valid(original)
            and original.get("policy") == "migration-v2" and "project_baseline" not in contract
            and original["sha256"] == contract.get("project_baseline_sha256")
            and original["original_index_ref"] == contract.get("original_index_ref")
            and original["original_index_sha256"] == contract.get("original_index_sha256")
            and original["environment"] == contract.get("environment")
            and original["artifact_root"] == contract.get("artifact_root"))
    bound = contract.get("project_baseline") or {}
    if original.get("policy") == "migration-v1" or bound.get("policy") == "migration-v1":
        return (contract.get("migration_policy") == 1 and contract.get("migration_scope_policy") == 1
                and contract.get("migration_occurrence_policy") == 1
                and contract.get("inspection_policy") == 4 and bump_project.baseline_is_valid(original)
                and original == bound)
    if original.get("version") == 2 or bound.get("version") == 2:
        from . import bump_delta
        return (bump_project.baseline_is_valid(original)
                and bump_project.baseline_is_valid(bound)
                and bound.get("scope", {}).get("bound") is True
                and contract.get("inspection_policy") == 2
                and bump_delta.baseline_matches(original, bound))
    if not (bump_project.baseline_is_valid(original)
            and bump_project.baseline_is_valid(bound)
            and bound.get("scope", {}).get("bound") is True
            and bound.get("origin_sha256", bound.get("sha256"))
            == original.get("origin_sha256", original.get("sha256"))):
        return False
    # An origin label is provenance, not authority: resealing a modified record
    # must not make changed original declarations/files into a new baseline.
    mutable = {"scope", "sha256", "origin_sha256"}
    if ({key: value for key, value in original.items() if key not in mutable}
            != {key: value for key, value in bound.items() if key not in mutable}):
        return False
    before, after = original["scope"], bound["scope"]
    if before.get("bound"):
        return before == after
    return (before.get("mode") == after.get("mode") == "natural"
            and set(after) == set(before)
            and set(after.get("existing_targets", []))
            <= set(original["project_axioms"]) | set(original["project_sorries"]))


def _project_context_issues(root: Path, contract: dict, actual_tasks: list[dict], *,
                            completed: set[str], final: bool) -> list[str]:
    """Preserve unrelated existing code while allowing scoped proof progress."""
    from . import bump_project

    baseline = contract.get("project_baseline")
    if baseline is None:
        # Lower-level historical contract fixtures retain the original strict
        # whole-project policy. No accepted pipeline boundary permits this.
        return []
    errors = bump_project.baseline_errors(baseline)
    if errors:
        return errors
    if baseline.get("scope", {}).get("bound") is not True:
        return ["existing-project scope has not been bound to exact original targets"]
    retained = adopted_output_tasks(contract)
    allowed_incomplete = {row["lean_decl"] for row in retained
                          if row["task_id"] not in completed}
    incomplete_files = {}
    for row in retained:
        if row["task_id"] not in completed:
            incomplete_files.setdefault(row["lean_decl"], set()).add(row["lean_file"])
    issues = bump_project.validate_baseline(
        root, baseline, final=final,
        allowed_new_paths=adopted_output_paths(contract),
        allowed_incomplete_declarations=allowed_incomplete if not final else set(),
        allowed_incomplete_files=incomplete_files if not final else {},
        **({"claimed_declarations": {row["lean_decl"]: row["lean_file"] for row in actual_tasks}}
           if baseline.get("version") == 2 else {}),
    )
    if final:
        represented = {row["lean_decl"] for row in actual_tasks if row["task_id"] in completed}
        missing = set(baseline.get("scope", {}).get("existing_targets", [])) - represented
        if missing:
            issues.append("original project targets lack completed adopted outputs: " + ", ".join(sorted(missing)))
    return issues


def prepare_source_contract(paths, dag: dict, *, state: dict | None = None,
                            environment: dict | None = None, main_sha: str | None = None) -> dict:
    """Pin an informal plan without generating, building, or inspecting Lean.

    A plan records obligations, not trusted declarations. Only successful exact
    candidate checks can extend ``bindings`` and ``targets`` later.
    """
    from . import bump_project, bump_state

    state = bump_state.load_state(paths.forum) if state is None else state
    if (state.get("project_baseline") or {}).get("policy") == "migration-v2":
        return prepare_migration_contract_v2(paths, baseline=state["project_baseline"], graph=dag,
            source=bump_state.formal_source(state), main_sha=main_sha)
    source = bump_state.formal_source(state)
    if (not source or dag.get("solution_candidate") != source["candidate_id"]
            or dag.get("solution_sha256") != source["sha256"]):
        raise ValueError("informal plan must target the current supplied-source snapshot")
    chunks = dag["chunks"]
    requirements = normalize_requirements(dag["requirements"], chunks,
                                         {row["ref_id"] for row in source["source_refs"]})
    spec = normalize_spec(dag.get("spec"), source=source, requirements=requirements,
                          tasks=chunks, allow_unresolved=True)
    previous_contract = state.get("formalization", {}).get("contract") or {}
    baseline = state.get("project_baseline") or {}
    if not bump_project.baseline_is_valid(baseline):
        raise ValueError("bump requires a verified existing-project baseline before chunking")
    if previous_contract:
        if not _baseline_matches(state, previous_contract):
            raise ValueError("bump project preservation context changed during plan refinement")
        baseline = previous_contract["project_baseline"]
    bound_baseline = bump_project.bind_scope(baseline, dag,
        **({"root": paths.project_root} if baseline.get("version") == 2 else {}))
    if not bump_project.baseline_is_valid(bound_baseline):
        raise ValueError("bump could not bind the target scope to the project baseline")
    if bound_baseline.get("policy") == "migration-v1":
        bump_project.require_pinned_inputs(paths.project_root, bound_baseline)
        bindings, targets = _migration_bindings(bound_baseline)
        value = {"version": 3, "migration_policy": 1, "migration_scope_policy": 1,
                 "migration_occurrence_policy": 1, "inspection_policy": 4,
                 "fingerprint_version": 2, "solution_candidate": source["candidate_id"],
                 "solution_sha256": source["sha256"], "requirements": requirements, "spec": spec,
                 "spec_sha256": digest(spec), "project_baseline": bound_baseline,
                 "environment": bound_baseline["environment"],
                 "source_main_sha": _git(paths.project_root, "rev-parse", "HEAD") if main_sha is None else main_sha,
                 "obligation_ids": sorted(bindings), "bindings": bindings, "targets": targets,
                 "external_declarations": {}, "prerequisite_declarations": {}}
        value["adopted_outputs"] = adopted_output_records(value)
        result = _seal_contract(value)
        if previous_contract and previous_contract != result:
            raise ValueError("migration module contract is fixed and cannot be replanned")
        return result
    if environment is None:
        # Authoritative publication must reject changed Lake/config/dependency
        # inputs before invoking any introspection/build command. Draft checks
        # with an explicitly pinned environment remain read-only pure checks.
        bump_project.require_pinned_inputs(
            paths.project_root, bound_baseline,
            allowed_new_paths=adopted_output_paths(previous_contract)
            if previous_contract else set(),
        )
    contract = _seal_contract({
        "version": 3,
        **({"inspection_policy": 2} if bound_baseline.get("version") == 2 else {}),
        "fingerprint_version": previous_contract.get("fingerprint_version", 1) if previous_contract else 2,
        **({"representation_review_policy": previous_contract.get("representation_review_policy", 1)}
           if not previous_contract or "representation_review_policy" in previous_contract else {}),
        "solution_candidate": source["candidate_id"], "solution_sha256": source["sha256"],
        "requirements": requirements, "spec": spec, "spec_sha256": digest(spec),
        "project_baseline": bound_baseline,
        "environment": environment_identity(paths.project_root) if environment is None else environment,
        "source_main_sha": _git(paths.project_root, "rev-parse", "HEAD") if main_sha is None else main_sha,
        "obligation_ids": sorted(row.get("task_id", row.get("id")) for row in chunks),
        "bindings": {}, "targets": {}, "external_declarations": {}, "prerequisite_declarations": {},
        "adopted_outputs": adopted_output_records(previous_contract),
    })
    return contract


def initialize_source_contract(paths, dag: dict) -> dict:
    """Publish a contract artifact, separate from read-only plan preparation."""
    contract = prepare_source_contract(paths, dag)
    record = artifacts.store_text(paths.artifacts, json.dumps({"contract": contract}, sort_keys=True) + "\n",
                                  kind="bump_source_contract", producer="Unity")
    return {**contract, "artifact_id": record["artifact_id"]}


def invalidate_bindings(contract: dict, task_ids: set[str]) -> tuple[dict, set[str]]:
    """Explicit revisions invalidate actual meaning dependencies, not just DAG hints.

    Return new current state; never edit historical contracts or candidates. A
    removed binding can only be adopted again by another checked candidate.
    """
    result = copy.deepcopy(contract)
    # Reopening withdraws semantic acceptance, not the fact that the controller
    # already admitted these exact outputs to the project. Never derive this
    # provenance from submissions, reservations, or model-written history.
    result["adopted_outputs"] = adopted_output_records(contract)
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


def output_target_key(contract: dict, task_id: str, declaration: str) -> str:
    """Resolve a task-local display reference to its sealed occurrence target."""
    if contract.get("migration_policy") == 2:
        from .bump_checker_v2 import output_target_key as resolve
        return resolve(contract, task_id, declaration)
    if contract.get("migration_policy") != 1:
        return declaration
    from . import bump_project
    baseline = contract.get("project_baseline") or {}
    if (contract.get("migration_occurrence_policy") != 1 or baseline.get("version") != 5
            or baseline.get("occurrence_policy") != 1):
        raise ValueError("migration requires module-occurrence evidence; start a fresh run")
    try:
        original = baseline["original_reports"][task_id]
        if declaration not in original["declarations"]:
            raise ValueError("output is absent from its original module inventory")
        native_name = original["meanings"][declaration]["meaning"]["name"]
        occurrence = bump_project.migration_occurrence_id(task_id, native_name)
        target = contract["targets"][occurrence]
        if (target.get("module") != task_id or target.get("declaration") != declaration
                or target.get("native_name") != native_name):
            raise ValueError("migration target changed its module or kernel-name identity")
        return occurrence
    except (KeyError, TypeError) as exc:
        raise ValueError("migration output lacks exact original occurrence evidence") from exc


def output_fingerprints(contract: dict, task_id: str, outputs: list[dict] | None = None) -> dict:
    """Every output fingerprint, keyed without collapsing same-named modules."""
    if contract.get("migration_policy") == 2:
        from .bump_checker_v2 import output_fingerprints as fingerprints
        return fingerprints(contract, task_id, outputs)
    rows = contract.get("bindings", {}).get(task_id, []) if outputs is None else outputs
    rows = normalize_outputs(rows)
    if contract.get("migration_policy") == 1 and rows != contract.get("bindings", {}).get(task_id):
        raise ValueError("migration outputs changed the fixed module occurrence inventory")
    return {key: contract["targets"][key]["fingerprint"] for key in
            (output_target_key(contract, task_id, row["declaration"]) for row in rows)}


def snapshot_declarations(contract: dict) -> dict:
    """Exact obligation lookup for snapshots; migration keys are occurrence IDs."""
    if contract.get("migration_policy") == 2:
        return {key: task for task, group in contract["task_bindings"].items() for key in group["obligation_ids"]}
    return {output_target_key(contract, row["task_id"], row["lean_decl"]): row["task_id"]
            for row in _binding_tasks(contract)}


def declaration_occurrences(contract: dict) -> dict:
    """Readable metadata lets critics cite exact occurrences without guessing IDs."""
    if contract.get("migration_policy") == 2:
        from .bump_checker_v2 import declaration_occurrences as occurrences
        return occurrences(contract)
    if contract.get("migration_policy") != 1:
        return {}
    return {output_target_key(contract, row["task_id"], row["lean_decl"]): {
                "task_id": row["task_id"], "module": row["task_id"],
                "declaration": row["lean_decl"], "file": row["lean_file"],
                "native_name": copy.deepcopy(contract["targets"][output_target_key(
                    contract, row["task_id"], row["lean_decl"])]["native_name"])}
            for row in _binding_tasks(contract)}


def resolve_review_declarations(contract: dict, task_ids: list[str], references: list[str]) -> list[str]:
    """Accept names only within an unambiguous requirement's explicit modules."""
    if contract.get("migration_policy") not in {1, 2}:
        return references
    occurrences = declaration_occurrences(contract)
    eligible = {key: row for key, row in occurrences.items() if row["task_id"] in task_ids}
    resolved = []
    for reference in references:
        if reference in eligible:
            matches = [reference]
        else:
            matches = [key for key, row in eligible.items() if row["declaration"] == reference]
        if len(matches) != 1:
            raise ValueError("review must reference an unambiguous declaration occurrence: " + reference)
        resolved.append(matches[0])
    if len(resolved) != len(set(resolved)):
        raise ValueError("requirement review has duplicate declaration occurrences")
    return resolved


def adopted_output_records(contract: dict, *, task_id: str | None = None,
                           outputs: list[dict] | None = None) -> list[dict]:
    """Exact controller-adopted provenance; not current meaning or completion.

    Legacy contracts can establish provenance only from their current sealed
    bindings. A present ledger must be canonical and contain every live binding.
    The optional extension is used only by checked candidate publication.
    """
    live = [{"task_id": owner, **row} for owner, rows in contract.get("bindings", {}).items()
            for row in normalize_outputs(rows)]
    raw = contract.get("adopted_outputs", live)
    if not isinstance(raw, list):
        raise ValueError("invalid adopted output provenance")
    normalized = []
    for row in raw:
        if (not isinstance(row, dict) or set(row) != {"task_id", "declaration", "file"}
                or not isinstance(row["task_id"], str) or not row["task_id"].strip()
                or row["task_id"] != row["task_id"].strip()):
            raise ValueError("invalid adopted output provenance")
        output = normalize_outputs([{key: row[key] for key in ("declaration", "file")}])[0]
        normalized.append({"task_id": row["task_id"], **output})
    key = lambda row: (row["task_id"], row["declaration"], row["file"])
    canonical = sorted({key(row): row for row in normalized}.values(), key=key)
    if "adopted_outputs" in contract and canonical != raw:
        raise ValueError("noncanonical adopted output provenance")
    if {key(row) for row in live} - {key(row) for row in canonical}:
        raise ValueError("adopted output provenance omits a current binding")
    if outputs is not None:
        if not isinstance(task_id, str) or not task_id.strip() or task_id != task_id.strip():
            raise ValueError("adopted output extension requires an exact task")
        canonical.extend({"task_id": task_id, **row} for row in normalize_outputs(outputs))
    return sorted({key(row): row for row in canonical}.values(), key=key)


def adopted_output_paths(contract: dict) -> set[str]:
    if contract.get("migration_policy") == 2:
        return {path for group in contract["task_bindings"].values() for path in group["files"]}
    if contract.get("migration_policy") == 1:
        return {row["path"] for row in contract["project_baseline"]["compiler_modules"].values()}
    return {row["file"] for row in adopted_output_records(contract)}


def adopted_output_tasks(contract: dict, *, root: Path | None = None) -> list[dict]:
    """Coverage includes retained files, without requiring removed scaffolds.

    Missing retained files can result from separately checked explicit cleanup;
    current bindings still undergo their ordinary declaration/existence checks.
    """
    return [{"task_id": row["task_id"], "lean_decl": row["declaration"], "lean_file": row["file"]}
            for row in adopted_output_records(contract)
            if root is None or (root / row["file"]).exists()]


def output_manifest_blockers(contract: dict, *, task_ids: set[str], task_id: str,
                             proposed_outputs: list[dict]) -> list[dict]:
    """Cheap declaration-binding checks, shared by submission and verification.

    This does not establish declaration existence, mathematical coverage or proof
    correctness. Changing adopted outputs always requires explicit refinement.
    """
    if contract.get("migration_policy") == 2:
        try:
            from .bump_checker_v2 import validate_contract
            validate_contract(contract)
            if (task_id not in task_ids or not task_ids <= contract["task_bindings"].keys()
                    or normalize_outputs(proposed_outputs) != contract["bindings"].get(task_id)):
                raise ValueError("migration outputs differ from their explicit current mapping")
            return []
        except (ValueError, KeyError, TypeError) as exc:
            return [{"code": "migration_mapping_changed", "prerequisite_id": "", "task_ids": [task_id],
                "message": str(exc), "required_action": "Explicitly propose and adopt the correspondence before submission.",
                "deterministic": True}]
    if contract.get("version") != 3:
        return []

    def blocked(code: str, message: str, action: str, **details) -> list[dict]:
        return [{"code": code, "prerequisite_id": "", "task_ids": [task_id] if task_id in task_ids else [],
                 "message": message, "required_action": action, "deterministic": True, **details}]

    if contract.get("migration_policy") == 1:
        try:
            bindings, targets = _migration_bindings(contract["project_baseline"])
            if (task_ids != set(bindings) or contract["bindings"] != bindings or contract["targets"] != targets
                    or task_id not in bindings or normalize_outputs(proposed_outputs) != bindings[task_id]):
                raise ValueError("migration outputs must exactly preserve the sealed original module inventory")
            return []
        except (ValueError, KeyError, TypeError) as exc:
            return blocked("migration_outputs_changed", str(exc), "Restore the original module output inventory.")

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
        proposed["adopted_outputs"] = adopted_output_records(
            contract, task_id=task_id, outputs=proposed_outputs)
        actual_tasks = _binding_tasks(proposed)
        if not actual_tasks:
            reject("no Lean representations have been adopted or proposed", "output_manifest_missing", set(),
                   "Submit the current task's actual Lean declaration/file outputs.")
        layout = workspace_layout(root) if layout is None else layout
        layout = scoped_layout(root, layout, contract.get("project_baseline"))
        for row in actual_tasks:
            try:
                module_for_file(root, row["lean_file"], _editable_modules(layout))
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
    except bump_jobs.JobCancelled:
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
    if "project_baseline" in contract:
        for issue in _project_context_issues(root, proposed, actual_tasks, completed=checked_complete, final=final):
            checked_issue(issue, "project_context_changed", set(),
                          "Restore the preserved project context; do not weaken original targets or edit unrelated code.")
    else:
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
        if inspection["project_axioms"] and "project_baseline" not in contract:
            checked_issue("project retains custom axioms: " + ", ".join(inspection["project_axioms"]), "project_custom_axioms",
                          set(), "Replace the named project axioms with proved declarations.")
        if inspection["project_sorries"] and "project_baseline" not in contract:
            checked_issue("project retains proof holes: " + ", ".join(inspection["project_sorries"]), "project_proof_holes",
                          set(), "Complete the named proof holes or remove genuinely obsolete drafts without dropping source coverage.")
    verified_targets = {}
    for task in actual_tasks:
        name, owner = task["lean_decl"], task["task_id"]
        row = targets[name]
        fingerprint = digest(_semantic_record(row, fingerprint_version=fingerprint_version))
        if row["module"] != module_for_file(root, task["lean_file"], _editable_modules(layout)):
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


def _migration_bindings(baseline: dict) -> tuple[dict, dict]:
    """Immutable original inventories, including private and generated declarations."""
    from . import bump_project
    if baseline.get("policy") != "migration-v1" or not bump_project.baseline_is_valid(baseline):
        raise ValueError("missing sealed migration baseline")
    bindings, targets = {}, {}
    for module, graph in sorted(baseline["compiler_modules"].items()):
        original = baseline["original_reports"][module]
        bindings[module] = [{"declaration": name, "file": graph["path"]}
                            for name in sorted(original["declarations"])]
        for name, row in original["declarations"].items():
            native_name = original["meanings"][name]["meaning"]["name"]
            occurrence = bump_project.migration_occurrence_id(module, native_name)
            targets[occurrence] = {"fingerprint": digest({"module": module, "name": native_name,
                "original_evidence": original["evidence_sha256"]}),
                "target_kind": row["kind"], "axioms": row["axioms"],
                "meaning_dependencies": original["meanings"][name]["dependencies"],
                "module": module, "declaration": name, "native_name": copy.deepcopy(native_name)}
    return bindings, targets


def prepare_migration_contract_v2(paths, *, baseline: dict, graph: dict, mapping=None,
                                  source: dict | None = None, main_sha: str | None = None) -> dict:
    from .bump_checker_v2 import prepare_migration_contract
    return prepare_migration_contract(paths, baseline=baseline, graph=graph, mapping=mapping,
                                      source=source, main_sha=main_sha)


def migration_baseline_v2(root: Path, contract: dict) -> dict:
    from .bump_checker_v2 import resolved_baseline
    return resolved_baseline(root, contract)


def migration_source_identity_v2(root: Path, contract: dict) -> dict:
    baseline = migration_baseline_v2(root, contract)
    from . import bump_project
    bump_project.require_pinned_inputs(root, baseline)
    return source_identity(root, layout=baseline["layout"])


def migration_group_mapping_content(contract: dict, task_id: str) -> dict:
    """Task-local correspondence intent, never a build or acceptance receipt.

    Global contract/mapping hashes and artifact identifiers intentionally do not
    make an unrelated group a new repair input. Exact evidence is still checked
    under the current complete contract at every integration and final review.
    """
    binding = contract.get("task_bindings", {}).get(task_id)
    if not binding:
        raise ValueError("unknown migration execution group")
    originals = set(binding["obligation_ids"])
    mappings = []
    for row in contract.get("mapping", {}).values():
        if not originals.intersection(row.get("original_ids", [])):
            continue
        mappings.append({
            "original_ids": sorted(row["original_ids"]), "mode": row["mode"],
            "targets": sorted(copy.deepcopy(row["targets"]), key=lambda value: digest(value)),
            # Mapping evidence is checked by content SHA when adopted. Artifact
            # aliases are not new intent, but changed evidence bytes are.
            "evidence_sha256": sorted({ref["sha256"] for ref in row.get("evidence_refs", [])}),
            **{key: row[key] for key in ("relation", "reason") if key in row},
        })
    return {"version": 1, "binding": copy.deepcopy(binding),
            "outputs": copy.deepcopy(contract.get("bindings", {}).get(task_id, [])),
            "mappings": sorted(mappings, key=lambda value: digest(value))}


def validate_mapping_v2(contract: dict, mapping=None, task_bindings=None) -> None:
    from .bump_checker_v2 import validate_mapping
    validate_mapping(contract, mapping, task_bindings)


def with_migration_mapping_v2(root: Path, contract: dict, mapping: dict) -> dict:
    from .bump_checker_v2 import with_mapping
    return with_mapping(root, contract, mapping)


def require_migration_receipt_v2(task: dict, contract: dict, verification: dict) -> None:
    from .bump_checker_v2 import require_receipt
    require_receipt(task, contract, verification)


def _check_migration_contract(root: Path, contract: dict, *, task_id: str | None,
                              proposed_outputs: list[dict] | None, stage: str, final: bool) -> dict:
    from . import bump_migration_contract as native, bump_project
    issues, targets, verified, comparisons, compiled = [], {}, {}, {}, {}
    checked = []
    before = None
    try:
        baseline = contract["project_baseline"]
        if (contract.get("inspection_policy") != 4 or contract.get("migration_policy") != 1
                or contract.get("migration_scope_policy") != 1
                or contract.get("migration_occurrence_policy") != 1):
            raise ValueError("migration requires sealed build scope and native environment inspection policy 4")
        bindings, expected_targets = _migration_bindings(baseline)
        if (contract["bindings"] != bindings or contract["targets"] != expected_targets
                or contract["obligation_ids"] != sorted(bindings)
                or contract.get("adopted_outputs") != adopted_output_records({"bindings": bindings})):
            raise ValueError("original migration declarations or meanings changed in the contract")
        if stage != "complete":
            raise ValueError("migration candidates must be complete module repairs")
        if not final and task_id not in bindings:
            raise ValueError("migration candidate requires an exact original module task")
        if proposed_outputs is not None and (task_id not in bindings or normalize_outputs(proposed_outputs) != bindings[task_id]):
            raise ValueError("migration candidate changed its fixed original output inventory")
        bump_project.require_pinned_inputs(root, baseline)
        before = source_identity(root)
        if before["environment"] != contract["environment"]:
            raise ValueError("target environment changed from the sealed migration contract")
        modules = sorted(bindings) if final else [task_id]
        for module in modules:
            original = baseline["original_reports"][module]
            current = native.inspect_module(root, module, sorted(bindings))
            scope_issues = bump_project.migration_inspection_scope_errors(current, baseline, root=root)
            if scope_issues:
                raise ValueError("; ".join(scope_issues))
            comparison = native.compare_module(original, current)
            comparisons[module] = comparison
            issues.extend(module + ": " + issue for issue in comparison["issues"])
            if not comparison["passed"]:
                continue
            checked.append(module)
            for name in original["declarations"]:
                occurrence = output_target_key(contract, module, name)
                targets[occurrence] = {**current["declarations"][name], "occurrence_id": occurrence,
                    "declaration": name, "module": module,
                    "native_name": copy.deepcopy(expected_targets[occurrence]["native_name"])}
                verified[occurrence] = expected_targets[occurrence]["fingerprint"]
            for filename, identity in current["compiled_inputs"].items():
                if filename in compiled and compiled[filename] != identity:
                    raise ValueError("compiled import changed between module inspection contexts: " + filename)
                compiled[filename] = identity
        bump_project.require_pinned_inputs(root, baseline)
        if source_identity(root) != before:
            raise ValueError("source or environment changed during migration verification")
    except bump_jobs.JobCancelled:
        raise
    except (ValueError, OSError, KeyError, TypeError) as exc:
        issues.append(str(exc))
    receipt = bump_cache.compiled_receipt(root, compiled) if compiled and not issues else None
    if not issues and not bump_cache.compiled_receipt_current(root, receipt):
        issues.append("migration compiled-input receipt is missing or stale")
    passed = not issues
    module_receipts = {module: {"module": module, "passed": passed,
        "scope_sha256": baseline["build_scope"]["sha256"], "occurrence_policy": 1,
        "verified_targets": output_fingerprints(contract, module) if passed else {},
        "policy_sha256": policy_hash(), "source_sha256": before["source_sha256"] if before else None,
        "compiled_receipt": receipt, "original_evidence": result["original_evidence"],
        "current_evidence": result["current_evidence"], "comparison_sha256": result["evidence_sha256"]}
        for module, result in comparisons.items()}
    return {"passed": passed, "issues": issues, "blockers": [], "targets": targets,
            "source_identity": before,
            "verified_targets": verified if passed else {}, "verified_tasks": checked if passed else [],
            "compiled_receipt": receipt, "final": final, "proposed_contract": contract,
            "module_receipts": module_receipts,
            "module_receipt": module_receipts.get(task_id, {"module": task_id, "passed": False,
                "policy_sha256": policy_hash(), "source_sha256": before["source_sha256"] if before else None}),
            "project_declarations": list(targets.values())}


def check_migration_module(paths, state: dict, module: str) -> dict:
    """Controller frontier check; never advances state or persists acceptance."""
    contract = state.get("formalization", {}).get("contract") or {}
    return check_formal_contract(paths.project_root, contract, list(state.get("formal_tasks", {}).values()),
        completed={module}, task_id=module, proposed_outputs=contract.get("bindings", {}).get(module, []))


def validate_migration_state(state: dict) -> None:
    """Pure resume guard over original occurrences and saved task receipts.

    A historical completed leaf may precede later changes elsewhere in main;
    its recorded check is not reinterpreted as a fresh whole-project check.
    Normal candidate/final boundaries still inspect current native artifacts.
    """
    from . import bump_state
    contract = state.get("formalization", {}).get("contract") or {}
    if contract.get("migration_policy") == 2:
        from .bump_checker_v2 import validate_saved_state
        validate_saved_state(state)
        return
    if (contract.get("migration_policy") != 1 or not _baseline_matches(state, contract)
            or contract.get("sha256") != _contract_digest(contract)):
        raise ValueError("Bump resume requires the sealed module-occurrence contract")
    baseline = contract["project_baseline"]
    bindings, targets = _migration_bindings(baseline)
    tasks = state.get("formal_tasks", {})
    if (contract.get("bindings") != bindings or contract.get("targets") != targets
            or contract.get("obligation_ids") != sorted(bindings) or set(tasks) != set(bindings)
            or adopted_output_records(contract) != adopted_output_records({"bindings": bindings})):
        raise ValueError("Bump saved state lost original module declaration occurrences")
    for module, task in tasks.items():
        graph = baseline["compiler_modules"][module]
        if (task.get("task_id") != module or task.get("migration_module") != module
                or task.get("lean_file") != graph["path"] or task.get("outputs") != bindings[module]
                or task.get("dependencies") != sorted(set(graph["imports"]) & set(bindings))):
            raise ValueError("Bump saved task changed its original occurrence inventory: " + module)
        if task.get("status") != "complete":
            continue
        candidate_id = task.get("accepted_candidate")
        candidate = state.get("formal_candidates", {}).get(candidate_id, {})
        if (not candidate_id or candidate.get("status") != "merged" or candidate.get("task_id") != module
                or candidate.get("outputs") != bindings[module]
                or candidate.get("stage", "complete") != "complete"
                or not bump_state.candidate_is_current(state, candidate)):
            raise ValueError("Bump completed occurrence lacks its current merged candidate: " + module)
        bump_state._require_migration_receipt(task, contract, candidate.get("verification") or {})


def validate_migration_snapshot(state: dict, report: dict) -> None:
    """Pure state-boundary validation; exact compiled-byte freshness is separate."""
    contract = state.get("formalization", {}).get("contract") or {}
    if contract.get("migration_policy") == 2:
        from .bump_checker_v2 import validate_snapshot
        validate_snapshot(state, report)
        return
    baseline = state.get("project_baseline") or {}
    if contract.get("migration_policy") != 1 or not _baseline_matches(state, contract):
        raise ValueError("machine review is not bound to a sealed migration contract")
    bindings, targets = _migration_bindings(baseline)
    if (contract.get("inspection_policy") != 4 or contract.get("migration_scope_policy") != 1
            or contract.get("migration_occurrence_policy") != 1
            or contract.get("bindings") != bindings
            or contract.get("targets") != targets or report.get("policy_sha256") != policy_hash()
            or report.get("project_baseline_sha256") != baseline["sha256"]
            or report.get("project_verification") != project_verification(Path(baseline["project_root"]), baseline)):
        raise ValueError("machine review migration policy or original module coverage changed")
    if report.get("passed") is True:
        receipts = report.get("module_receipts")
        if (not isinstance(receipts, dict) or set(receipts) != set(bindings)
                or report.get("verified_targets") != {n: r["fingerprint"] for n, r in targets.items()}
                or report.get("declarations") != snapshot_declarations(contract)
                or report.get("declaration_occurrences") != declaration_occurrences(contract)):
            raise ValueError("accepted migration review lacks complete native module/original meaning receipts")
        for module, receipt in receipts.items():
            if (receipt.get("module") != module or receipt.get("passed") is not True
                    or receipt.get("occurrence_policy") != 1
                    or receipt.get("verified_targets") != output_fingerprints(contract, module)
                    or receipt.get("scope_sha256") != baseline["build_scope"]["sha256"]
                    or receipt.get("policy_sha256") != report["policy_sha256"]
                    or receipt.get("source_sha256") != report.get("source_sha256")
                    or receipt.get("compiled_receipt") != report.get("compiled_receipt")
                    or receipt.get("original_evidence") != baseline["original_reports"][module]["evidence_sha256"]
                    or any(not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get(key, "")))
                           for key in ("current_evidence", "comparison_sha256"))):
                raise ValueError("accepted migration review has invalid native receipt for " + module)


def check_formal_contract(root: Path, contract: dict, tasks: list[dict],
                          *, completed: set[str], layout: dict | None = None,
                          environment: dict | None = None, timings: dict | None = None,
                          proposed_outputs: list[dict] | None = None, task_id: str | None = None,
                          stage: str = "complete", final: bool = False) -> dict:
    """Check exact adopted meaning; new representations extend it only on success."""
    body = {key: value for key, value in contract.items() if key not in {"sha256", "artifact_id"}}
    if not contract or digest(body) != contract.get("sha256"):
        return {"passed": False, "issues": ["formal contract is missing or corrupt"], "targets": {}}
    if contract.get("migration_policy") == 2 or contract.get("version") == 4:
        from .bump_checker_v2 import check
        return check(root, contract, task_id=task_id, proposed_outputs=proposed_outputs, stage=stage, final=final)
    if contract.get("fingerprint_version", 1) not in {1, 2}:
        return {"passed": False, "issues": ["unsupported semantic fingerprint version"], "targets": {}}
    if contract.get("version") not in {2, 3} or not isinstance(contract.get("spec"), dict):
        return {"passed": False, "issues": ["formal contract lacks source-evidence metadata; re-chunk it"], "targets": {}}
    if digest(contract["spec"]) != contract.get("spec_sha256"):
        return {"passed": False, "issues": ["formal contract spec digest is inconsistent"], "targets": {}}
    if contract.get("migration_policy") == 1:
        return _check_migration_contract(root, contract, task_id=task_id,
            proposed_outputs=proposed_outputs, stage=stage, final=final)
    if "project_baseline" in contract:
        from . import bump_project
        try:
            if contract["project_baseline"].get("version") == 2 and contract.get("inspection_policy") != 2:
                raise ValueError("change-focused baseline requires context-bound inspection policy 2")
            paths = adopted_output_paths(contract)
            if proposed_outputs is not None:
                paths.update(row["file"] for row in normalize_outputs(proposed_outputs))
            bump_project.require_pinned_inputs(root, contract["project_baseline"], allowed_new_paths=paths)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            message = "existing project inputs changed before inspection: " + str(exc)
            return {"passed": False, "issues": [message], "targets": {}, "blockers": [{
                "code": "project_context_changed", "prerequisite_id": "", "task_ids": [],
                "message": message, "required_action": "Restore the original project inputs before checking candidates.",
                "deterministic": True}]}
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
    except bump_jobs.JobCancelled:
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
    from .bump_input import scope_bytes
    try:
        return hashlib.sha256(scope_bytes(paths)).hexdigest() == state["problem_sha256"]
    except (OSError, ValueError, KeyError):
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
    except bump_jobs.JobCancelled:
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
    from . import bump_project
    from .bump_input import source_matches
    from .bump_representation import snapshot as representation_snapshot
    from .bump_state import repair_digest

    if not snapshot:
        return False
    formal = state["formalization"]
    contract = formal.get("contract") or {}
    if contract.get("migration_policy") == 2:
        from .bump_checker_v2 import snapshot_is_current as current
        return current(paths, state, snapshot, require_complete=require_complete)
    if not _baseline_matches(state, contract) or contract.get("version") != 3:
        return False
    try:
        if contract.get("migration_policy") == 1:
            validate_migration_snapshot(state, snapshot)
        bump_project.require_original_branch(paths.project_root, contract["project_baseline"])
        bump_project.require_pinned_inputs(
            paths.project_root, contract["project_baseline"],
            allowed_new_paths=adopted_output_paths(contract),
        )
        current = source_identity(paths.project_root)
        coverage = project_verification(paths.project_root, contract["project_baseline"],
            **({"tasks": adopted_output_tasks(contract, root=paths.project_root)} if contract["project_baseline"].get("version") == 2 else {}))
    except (OSError, ValueError, KeyError, TypeError):
        return False
    source = state.get("input_source") or {}
    return (
        _baseline_matches(state, contract)
        and contract.get("version") == 3
        and snapshot.get("project_baseline_sha256") == contract["project_baseline"]["sha256"]
        and snapshot.get("project_verification") == coverage
        and snapshot.get("policy_sha256") == policy_hash()
        and (snapshot.get("passed") is not True
             or bump_cache.compiled_receipt_current(paths.project_root, snapshot.get("compiled_receipt")))
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
             or (contract.get("sha256") == _contract_digest(contract)
                 and snapshot_declarations(contract)
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


def _candidate_build_reusable(root: Path, contract: dict, candidate: dict, current: dict) -> bool:
    """A changes-mode build receipt includes every actual compiled context."""
    verification = candidate.get("verification") or {}
    reusable = (verification.get("status") == "passed"
                and verification.get("policy_sha256") == policy_hash()
                and verification.get("source_identity") == current
                and candidate.get("build", {}).get("returncode") == 0)
    if reusable and contract.get("project_baseline", {}).get("project_scope") == "changes":
        reusable = bump_cache.compiled_receipt_current(root, verification.get("compiled_receipt"))
    return reusable


def verify_final_project(paths, state: dict) -> dict:
    from . import bump_project
    from .bump_input import source_matches
    from .bump_representation import snapshot as representation_snapshot
    from .bump_state import interface_available, repair_digest

    root = paths.project_root
    formal = state["formalization"]
    contract = formal.get("contract") or {}
    if contract.get("migration_policy") == 2:
        from .bump_checker_v2 import verify_final
        return verify_final(paths, state)
    baseline_current = _baseline_matches(state, contract)
    if not baseline_current or contract.get("version") != 3:
        raise ValueError("verified existing-project context is missing or changed; legacy scaffold contracts cannot be accepted")
    bump_project.require_original_branch(root, contract["project_baseline"])
    bump_project.require_pinned_inputs(
        root, contract["project_baseline"],
        allowed_new_paths=adopted_output_paths(contract),
    )
    before = source_identity(root)
    tasks = list(state["formal_tasks"].values())
    last = next((candidate for candidate in reversed(list(state["formal_candidates"].values()))
                 if candidate.get("status") == "merged" and candidate.get("main_sha") == before["main_sha"]), {})
    verification = last.get("verification") or {}
    complete_ids = set(state["formal_tasks"])
    build_reusable = (contract.get("migration_policy") != 1
                      and _candidate_build_reusable(root, contract, last, before))
    reusable = (baseline_current and contract.get("version") == 3 and build_reusable
                and bump_cache.compiled_receipt_current(root, verification.get("compiled_receipt"))
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
                 if contract.get("version") == 3 and build_reusable else build_sources(root, full=True,
                     **({"baseline": contract["project_baseline"]}
                        if ("verification_scope" in contract.get("project_baseline", {})
                            or contract.get("project_baseline", {}).get("version") == 2
                            or contract.get("migration_policy") == 1) else {}),
                     **({"tasks": adopted_output_tasks(contract, root=root)}
                        if contract.get("project_baseline", {}).get("version") == 2 else {})))
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
        if contract.get("sha256") != _contract_digest(contract):
            issues.append("source contract is corrupt")
        if (set(contract.get("obligation_ids", [])) != complete_ids
                or set(contract.get("bindings", {})) != complete_ids
                or (contract.get("migration_policy") != 1
                    and any(not outputs for outputs in contract.get("bindings", {}).values()))):
            issues.append("source obligations lack adopted Lean outputs")
    coverage = project_verification(root, contract["project_baseline"],
        **({"tasks": adopted_output_tasks(contract, root=root)} if contract["project_baseline"].get("version") == 2 else {}))
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
        "project_baseline_sha256": (review_contract.get("project_baseline") or {}).get("sha256"),
        **({"project_verification": coverage} if coverage is not None else {}),
        "compiled_receipt": check.get("compiled_receipt"),
        **({"module_receipts": check.get("module_receipts", {}),
            "verified_targets": check.get("verified_targets", {}),
            "declaration_occurrences": declaration_occurrences(review_contract)}
           if contract.get("migration_policy") == 1 else {}),
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
        "declarations": (snapshot_declarations(contract) if contract.get("version") == 3 else
                         {task["lean_decl"]: task["task_id"] for task in tasks}),
        "build": build,
        "targets": check["targets"],
        **extension,
    }
    report["snapshot_id"] = "review-" + uuid.uuid4().hex
    artifact = artifacts.store_text(paths.artifacts, json.dumps(report, sort_keys=True) + "\n",
                                    kind="bump_machine_review", producer="Unity")
    # Keep telemetry out of shared prompt memory. Detail remains in the artifact.
    return {key: value for key, value in report.items() if key not in {"build", "targets"}} | {
        "artifact_id": artifact["artifact_id"],
    }
