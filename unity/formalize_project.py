"""Fail-closed preservation policy for formalizing an existing Lean project.

This is not a semantic critic. It freezes the project's original kernel
declarations and environment, then grants narrowly scoped completion rights.
It never stashes, commits, resets, scaffolds, or repairs the user's project.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
from pathlib import Path

_RUNTIME = {".git", ".lake", ".unity", ".worktrees", "lake-packages", "__pycache__"}
_KINDS = {"theorem", "def", "opaque", "inductive", "constructor", "recursor", "quot", "axiom"}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _seal(value: dict) -> dict:
    value = copy.deepcopy(value)
    value.pop("sha256", None)
    value["sha256"] = _digest(value)
    return value


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "existing project Git inspection failed")
    return result.stdout.strip()


def _safe_path(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and not any(
        part in _RUNTIME for part in path.parts)


def _tracked(root: Path) -> list[str]:
    return sorted(name for name in _git(root, "ls-files", "-z").split("\0")
                  if name and _safe_path(name))


def _require_clean(root: Path) -> None:
    """Runtime directories may be generated; all user work must stay untouched."""
    result = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                            cwd=root, capture_output=True, check=True)
    # NUL framing handles whitespace and rename destinations without shell parsing.
    entries = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    dirty = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status, name = entry[:2], entry[3:]
        if _safe_path(name):
            dirty.append(name)
        if "R" in status or "C" in status:
            if index < len(entries) and _safe_path(entries[index]):
                dirty.append(entries[index])
            index += 1
    if dirty:
        raise ValueError("existing-project baseline requires clean tracked/untracked user inputs; "
                         "preserve or commit them yourself before starting: " + ", ".join(sorted(set(dirty))))


def _records(inspection: dict) -> dict:
    records = inspection.get("project_records")
    inventory = inspection.get("project_declarations")
    if not isinstance(records, dict) or not isinstance(inventory, list):
        raise ValueError("existing-project baseline requires complete native declaration inventory")
    if (any(not isinstance(row, dict) or not isinstance(row.get("name"), str) for row in inventory)
            or len(inventory) != len(records)
            or set(records) != {row["name"] for row in inventory}):
        raise ValueError("native declaration records do not cover the entire project inventory")
    inventory_by_name = {row["name"]: row for row in inventory}
    for name, row in records.items():
        if (not isinstance(row, dict) or row.get("name") != name
                or row.get("target_kind") not in _KINDS or not isinstance(row.get("module"), str)
                or not isinstance(row.get("type"), list) or not isinstance(row.get("level_params"), list)
                or not isinstance(row.get("is_internal_detail"), bool)
                or not isinstance(row.get("direct_dependencies"), list)
                or any(not isinstance(dep, str) for dep in row.get("direct_dependencies", []))
                or not isinstance(row.get("declaration_meaning"), dict) or "proof_body" not in row
                or (row["proof_body"] is not None and not isinstance(row["proof_body"], list))
                or row["module"] != inventory_by_name[name].get("module")
                or row["target_kind"] != inventory_by_name[name].get("kind")):
            raise ValueError(f"incomplete native preservation evidence for {name}")
        meaning = row["declaration_meaning"]
        if (not {"name", "module", "kind", "type", "level_params"} <= meaning.keys()
                or meaning["module"] != row["module"] or meaning["kind"] != row["target_kind"]
                or meaning["type"] != row["type"] or meaning["level_params"] != row["level_params"]
                or (row["target_kind"] in {"theorem", "def", "opaque"} and not isinstance(row["proof_body"], list))
                or (row["target_kind"] in {"def", "opaque"} and meaning.get("value") != row["proof_body"])):
            raise ValueError(f"incomplete native declaration meaning/body evidence for {name}")
    for key in ("project_axioms", "project_sorries", "project_used_axioms"):
        if not isinstance(inspection.get(key), list) or any(not isinstance(name, str)
                                                         for name in inspection[key]):
            raise ValueError(f"native declaration inventory omitted {key}")
    return copy.deepcopy(records)


def _require_editable_targets(selected, records: dict, holes: set[str]) -> None:
    """Reject generated-hole scopes until native source ownership is available.

    Lean can move a structure-field sorry into `definition._proof_1`. A name
    prefix is not reliable evidence that changing that auxiliary preserves the
    original definition's other fields. Do not invent such ownership rights.
    Unrelated generated declarations remain protected and may stay out of scope.
    """
    visited, pending, unsupported = set(), list(selected), set()
    while pending:
        name = pending.pop()
        if name in visited or name not in records:
            continue
        visited.add(name)
        row = records[name]
        if row["is_internal_detail"] and (name in holes or name in selected):
            unsupported.add(name)
        pending.extend(row["direct_dependencies"])
    if unsupported:
        raise ValueError("selected scope contains compiler-generated/internal proof holes without reliable "
                         "editable-owner evidence; this scope is not supported yet. Choose explicit ordinary "
                         "targets independent of these holes, or preserve this project unchanged: "
                         + ", ".join(sorted(unsupported)))


def _initial_scope(target_scope: str, records: dict, holes: set[str]) -> dict:
    if not isinstance(target_scope, str) or not target_scope.strip():
        raise ValueError("target scope must be nonempty")
    text = target_scope.strip()
    if text.lower() == "all":
        _require_editable_targets(holes, records, holes)
        return {"mode": "all", "existing_targets": sorted(holes), "bound": True}
    tokens = [token for token in re.split(r"[,\s]+", text) if token]
    if tokens and all(token in records for token in tokens):
        _require_editable_targets(set(tokens), records, holes)
        return {"mode": "explicit", "existing_targets": sorted(set(tokens)), "bound": True}
    # Names-only syntax with an unknown identifier is likely a typo, not license
    # to let a planner reinterpret it as a broad natural-language request.
    if all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_'.]*", token) for token in tokens) and (
            len(tokens) == 1 or "," in text or any("." in token for token in tokens)):
        raise ValueError("unknown explicit existing target(s): " + ", ".join(
            token for token in tokens if token not in records))
    return {"mode": "natural", "existing_targets": [], "bound": False}


def capture_baseline(root: Path, target_scope: str = "All", inspection: dict | None = None) -> dict:
    """Build and freeze a clean, existing project before any agent dispatch.

    The optional native receipt is an internal testing/reuse hook; it is never an
    agent-supplied tool argument. Native build/environment checks still run.
    """
    from . import formalize_contract as contract
    root = Path(root).resolve()
    _require_clean(root)
    head = _git(root, "rev-parse", "HEAD")
    branch = _git(root, "branch", "--show-current")
    if not branch:
        raise ValueError("existing-project baseline requires a named branch")
    layout = contract.workspace_layout(root)
    before = contract._file_hashes(root, build_dir=layout["build_dir"])
    tracked = _tracked(root)
    uncopied = set(before) - set(tracked)
    if uncopied:
        raise ValueError("existing-project inputs are ignored/untracked and cannot be preserved in worktrees: "
                         + ", ".join(sorted(uncopied)))
    environment = contract.environment_identity(root)
    build = contract.build_sources(root, full=True, layout=layout, task_id="project-baseline")
    if build["returncode"]:
        raise ValueError("existing project must build before formalization: " + build["output"][-3000:])
    if inspection is None:
        inspection = contract.inspect_environment(root, [], layout=layout, _inventory_only=True)
    records = _records(inspection)
    holes = set(inspection["project_axioms"]) | set(inspection["project_sorries"])
    if holes - records.keys():
        raise ValueError("native hole inventory contains unaccounted project declarations")
    _require_clean(root)
    if (head != _git(root, "rev-parse", "HEAD")
            or before != contract._file_hashes(root, build_dir=layout["build_dir"])
            or environment != contract.environment_identity(root)):
        raise ValueError("project inputs changed while capturing the existing-project baseline")
    return _seal({"version": 1, "project_root": str(root), "branch": branch, "head": head, "files": before,
                  "tracked_files": tracked, "environment": environment, "layout": layout,
                  "declarations": records, "target_scope": target_scope.strip(),
                  "scope": _initial_scope(target_scope, records, holes),
                  **{key: sorted(inspection[key]) for key in
                     ("project_axioms", "project_sorries", "project_used_axioms")}})


def baseline_errors(baseline: dict) -> list[str]:
    if not isinstance(baseline, dict) or baseline.get("version") != 1:
        return ["existing-project baseline is missing or unsupported"]
    expected = _seal(baseline)["sha256"]
    if baseline.get("sha256") != expected:
        return ["existing-project baseline integrity mismatch"]
    required = {"branch", "head", "files", "tracked_files", "environment", "layout", "declarations",
                "target_scope", "scope", "project_axioms", "project_sorries", "project_used_axioms"}
    if not required <= baseline.keys():
        return ["existing-project baseline is incomplete"]
    if (any(not isinstance(baseline[key], str) or not baseline[key]
            for key in ("branch", "head", "target_scope"))
            or not isinstance(baseline["declarations"], dict) or not isinstance(baseline["files"], dict)
            or not isinstance(baseline["environment"], dict)
            or not isinstance(baseline["layout"], dict) or not isinstance(baseline["layout"].get("modules"), dict)
            or not isinstance(baseline["layout"].get("build_dir"), str)
            or any(not isinstance(baseline[key], list) or any(not isinstance(name, str) for name in baseline[key])
                   for key in ("tracked_files", "project_axioms", "project_sorries", "project_used_axioms"))
            or not isinstance(baseline["scope"], dict)
            or baseline["scope"].get("mode") not in {"all", "explicit", "natural"}
            or not isinstance(baseline["scope"].get("bound"), bool)
            or not isinstance(baseline["scope"].get("existing_targets"), list)
            or any(not isinstance(name, str) for name in baseline["scope"]["existing_targets"])
            or not set(baseline["scope"]["existing_targets"]) <= baseline["declarations"].keys()):
        return ["existing-project baseline has invalid scope/declaration evidence"]
    try:
        _records({"project_records": baseline["declarations"],
                  "project_declarations": [{"name": name, "module": row["module"], "kind": row["target_kind"]}
                                           for name, row in baseline["declarations"].items()],
                  **{key: baseline[key] for key in ("project_axioms", "project_sorries", "project_used_axioms")}})
    except (ValueError, KeyError, TypeError):
        return ["existing-project baseline has incomplete native declaration evidence"]
    return []


def baseline_is_valid(baseline: dict) -> bool:
    """Cheap checksum/schema guard; not a substitute for native verification."""
    return not baseline_errors(baseline)


def require_original_branch(root: Path, baseline: dict) -> None:
    """Controller continuation guard; call on the original project checkout."""
    branch = _git(Path(root).resolve(), "branch", "--show-current")
    if branch != baseline.get("branch"):
        raise ValueError("existing project branch changed from the preserved baseline")


def require_pinned_inputs(root: Path, baseline: dict, *, allowed_new_paths=()) -> None:
    """Read-only pre-Lake guard for a previously trusted project snapshot.

    Inspect configuration bytes before reading its dependency manifest, and
    inspect dependency sources before any Lake configuration/elaboration runs.
    This is preservation checking, not an adversarial Lean execution sandbox.
    """
    from . import formalize_contract as contract
    errors = baseline_errors(baseline)
    if errors:
        raise ValueError("; ".join(errors))
    root = Path(root).resolve()
    allowed = set(allowed_new_paths)
    if any(not isinstance(path, str) or not _safe_path(path) or Path(path).suffix != ".lean"
           for path in allowed):
        raise ValueError("invalid controller-approved supporting source path")

    def file_hash(name: str) -> str:
        if not _safe_path(name):
            raise ValueError("unsafe path in existing-project baseline")
        path = root / name
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != root and parent.is_relative_to(root)):
            raise ValueError(f"pinned project input became a symlink: {name}")
        if not path.is_file():
            raise ValueError(f"pinned project input is missing or not a regular file: {name}")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    # Even a newly introduced second Lake configuration is a policy change.
    actual_config = {name: file_hash(name) for name in sorted(contract._CONFIGS)
                     if (root / name).exists() or (root / name).is_symlink()}
    if actual_config != baseline["environment"].get("config", {}):
        raise ValueError("pinned project toolchain/build configuration changed before Lake execution")

    selected = set(baseline["scope"]["existing_targets"])
    modules = {baseline["declarations"][name]["module"] for name in selected}
    writable = {name for name, module in baseline["layout"]["modules"].items() if module in modules}
    writable.update(allowed)
    for name, expected in baseline["files"].items():
        # Check existence/symlinks even for a writable proof module. All
        # non-target modules stay byte-locked unless a controller manifest
        # expressly permits a new supporting declaration in that module.
        actual = file_hash(name)
        if name not in writable and actual != expected:
            raise ValueError(f"pinned project input changed before Lake execution: {name}")
    if contract._dependencies(root) != baseline["environment"].get("dependencies", {}):
        raise ValueError("pinned dependency source bytes changed before Lake execution")


def _output_names(dag: dict) -> set[str]:
    names = set()
    for chunk in dag.get("chunks", []):
        if isinstance(chunk.get("lean_decl"), str) and chunk["lean_decl"]:
            names.add(chunk["lean_decl"])
        for output in chunk.get("outputs", []):
            if isinstance(output, dict):
                name = output.get("declaration", output.get("lean_decl"))
                if isinstance(name, str):
                    names.add(name)
    return names


def bind_scope(baseline: dict, dag: dict) -> dict:
    """Bind natural-language scope to exact planned existing names once.

    Mechanical binding only grants completion rights to pre-existing holes. The
    independent critic still judges whether those names cover the user's words.
    """
    errors = baseline_errors(baseline)
    if errors:
        raise ValueError("; ".join(errors))
    result = copy.deepcopy(baseline)
    outputs = _output_names(dag)
    existing_outputs = outputs & baseline["declarations"].keys()
    scope = result["scope"]
    proposed = dag.get("existing_targets")
    if not isinstance(proposed, list) or any(not isinstance(name, str) for name in proposed):
        raise ValueError("plan requires existing_targets listing exact existing declaration names")
    if len(proposed) != len(set(proposed)):
        raise ValueError("existing_targets must not contain duplicate declaration names")
    if not scope.get("bound"):
        selected = set(proposed)
        incomplete = set(baseline["project_axioms"]) | set(baseline["project_sorries"])
        if not selected <= incomplete:
            raise ValueError("natural-language plan may only select existing incomplete declarations: "
                             + ", ".join(sorted(selected - incomplete)))
        scope.update(existing_targets=sorted(selected), bound=True)
    selected = set(scope["existing_targets"])
    _require_editable_targets(selected, baseline["declarations"],
                              set(baseline["project_axioms"]) | set(baseline["project_sorries"]))
    if set(proposed) != selected:
        raise ValueError("plan omits or changes selected existing targets: " + ", ".join(sorted(selected ^ set(proposed))))
    if existing_outputs - selected:
        raise ValueError("plan claims protected existing declarations: "
                         + ", ".join(sorted(existing_outputs - selected)))
    result.setdefault("origin_sha256", baseline["sha256"])
    return _seal(result)


def _signature(row: dict) -> dict:
    # Type ASTs are authoritative. Pretty-printed signatures are never identity.
    return {key: row.get(key) for key in ("name", "module", "level_params", "type")}


def _preserved_meaning(row: dict) -> dict:
    return {key: row.get(key) for key in
            ("name", "module", "target_kind", "level_params", "type", "declaration_meaning", "proof_body")}


def validate_baseline(root: Path, baseline: dict, inspection: dict | None = None, *,
                      final: bool = False, allowed_new_paths=(), allowed_incomplete_declarations=()) -> list[str]:
    """Check original project preservation without changing sources or Git state.

    Caller builds fresh inputs before inspection. Allowed new paths/placeholder
    names must come from controller-validated manifests, never unchecked prose.
    """
    from . import formalize_contract as contract
    errors = baseline_errors(baseline)
    if errors:
        return errors
    root = Path(root).resolve()
    selected = set(baseline["scope"]["existing_targets"])
    if final and not baseline["scope"].get("bound"):
        errors.append("existing-project scope has not been bound to exact targets")
    new_paths = set(allowed_new_paths)
    if any(not isinstance(path, str) or not _safe_path(path) or Path(path).suffix != ".lean"
           for path in new_paths):
        return errors + ["invalid controller-approved supporting source path"]
    try:
        # Private Git worktrees legitimately use task branches. The original
        # checkout (including an original that itself is a worktree) must not.
        if str(root) == baseline.get("project_root") or (root / ".git").is_dir():
            require_original_branch(root, baseline)
        require_pinned_inputs(root, baseline, allowed_new_paths=new_paths)
        if _git(root, "merge-base", "--is-ancestor", baseline["head"], "HEAD"):
            errors.append("current project no longer descends from the preserved baseline")
        layout = contract.workspace_layout(root)
        current_files = contract._file_hashes(root, build_dir=layout["build_dir"])
        if contract.environment_identity(root) != baseline["environment"]:
            errors.append("existing-project toolchain, build configuration or dependency bytes changed")
        if inspection is None:
            inspection = contract.inspect_environment(root, [], layout=layout, _inventory_only=True)
        records = _records(inspection)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        return errors + ["cannot verify existing-project preservation: " + str(exc)]
    old = baseline["declarations"]
    mutable_modules = {old[name]["module"] for name in selected}
    mutable_paths = {name for name, module in baseline["layout"]["modules"].items()
                     if module in mutable_modules}
    # A manifest may add a genuinely new helper in an existing Lean file. Its
    # original declarations still undergo exact kernel/body preservation below.
    mutable_paths.update(new_paths & baseline["files"].keys())
    for path, digest in baseline["files"].items():
        if path not in current_files:
            errors.append(f"existing project input deleted: {path}")
        elif current_files[path] != digest and path not in mutable_paths:
            errors.append(f"protected project input changed: {path}")
    for path in current_files.keys() - baseline["files"].keys():
        if path not in new_paths:
            errors.append(f"unapproved new project input: {path}")
    for path, module in baseline["layout"]["modules"].items():
        if layout["modules"].get(path) != module:
            errors.append(f"existing module ownership changed: {path}")
    incomplete = set(baseline["project_sorries"]) | set(baseline["project_axioms"])
    for name, previous in old.items():
        current = records.get(name)
        if current is None:
            errors.append(f"existing declaration removed: {name}")
            continue
        if name not in selected:
            if _preserved_meaning(current) != _preserved_meaning(previous):
                errors.append(f"protected existing declaration changed: {name}")
            continue
        if name not in incomplete:
            if _preserved_meaning(current) != _preserved_meaning(previous):
                label = "definition" if previous["target_kind"] not in {"axiom", "theorem"} else "declaration"
                errors.append(f"completed existing {label} changed: {name}")
            continue
        if _signature(current) != _signature(previous):
            errors.append(f"existing target signature or namespace/module changed: {name}")
        before_kind, after_kind = previous["target_kind"], current["target_kind"]
        if after_kind != before_kind and (before_kind, after_kind) != ("axiom", "theorem"):
            errors.append(f"existing target declaration kind changed: {name}")
        if before_kind not in {"axiom", "theorem"}:
            # Incomplete bodies are writable. Safety, reducibility, constructors,
            # recursors and mutual-family metadata remain fixed.
            before_meaning = {k: v for k, v in previous["declaration_meaning"].items() if k != "value"}
            after_meaning = {k: v for k, v in current["declaration_meaning"].items() if k != "value"}
            if before_meaning != after_meaning:
                errors.append(f"existing partial definition metadata changed: {name}")
    new_axioms = set(inspection["project_axioms"]) - set(baseline["project_axioms"])
    if new_axioms:
        errors.append("new project axioms: " + ", ".join(sorted(new_axioms)))
    permitted_sorries = set(baseline["project_sorries"])
    if not final:
        permitted_sorries.update(allowed_incomplete_declarations)
    new_sorries = set(inspection["project_sorries"]) - permitted_sorries
    if new_sorries:
        errors.append("new out-of-scope proof holes: " + ", ".join(sorted(new_sorries)))
    new_axiom_uses = (set(inspection["project_used_axioms"]) - set(baseline["project_used_axioms"])
                      - contract.AXIOMS - {"sorryAx"})
    if new_axiom_uses:
        errors.append("new forbidden axiom dependencies: " + ", ".join(sorted(new_axiom_uses)))
    if final:
        unfinished = selected & (set(inspection["project_axioms"]) | set(inspection["project_sorries"]))
        if unfinished:
            errors.append("selected existing targets remain incomplete: " + ", ".join(sorted(unfinished)))
    return errors
