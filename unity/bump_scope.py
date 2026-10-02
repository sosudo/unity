"""Controller-owned verification scope, distinct from source ownership/edit rights.

Library mode never repairs or ignores a required import. Optional auxiliary
sources stay byte-frozen; auxiliary imports required at capture remain verified
but read-only. Native Lake ownership and Lean header parsing are authoritative.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import PurePosixPath


def _seal(value: dict) -> dict:
    value = copy.deepcopy(value)
    value.pop("sha256", None)
    value["sha256"] = hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    return value


def _strings(value) -> bool:
    return (isinstance(value, list) and all(isinstance(item, str) and item for item in value)
            and value == sorted(set(value)))


def _path(value) -> bool:
    return (isinstance(value, str) and bool(value) and "\\" not in value and "\0" not in value
            and not PurePosixPath(value).is_absolute() and ".." not in PurePosixPath(value).parts
            and str(PurePosixPath(value)) == value and value.endswith(".lean")
            and not set(PurePosixPath(value).parts) & {
                ".git", ".lake", ".unity", ".worktrees", "lake-packages", "__pycache__"})


def _modules(value) -> bool:
    return (isinstance(value, dict) and all(_path(path) and isinstance(name, str) and name
                                          for path, name in value.items())
            and len(set(value.values())) == len(value))


def _ownership(layout: dict) -> tuple[dict, dict, list]:
    modules, owners, libraries = (layout.get("modules"), layout.get("module_owners"),
                                 layout.get("libraries"))
    if (not _modules(modules) or not isinstance(owners, dict) or set(owners) != set(modules)
            or not _strings(libraries)):
        raise ValueError("library scope requires complete native Lake ownership metadata")
    for row in owners.values():
        if (not isinstance(row, dict) or set(row) != {"libraries", "executables"}
                or not _strings(row["libraries"]) or not _strings(row["executables"])
                or not set(row["libraries"]) <= set(libraries)
                or not (row["libraries"] or row["executables"])):
            raise ValueError("library scope has invalid or ambiguous native module ownership")
    return modules, owners, libraries


def _eligible(modules: dict, owners: dict, libraries: list) -> dict:
    return {path: name for path, name in modules.items()
            if set(owners[path]["libraries"]) & set(libraries)}


def _walk(modules: dict, initial: dict, imports: dict) -> set[str]:
    by_name = {name: path for path, name in modules.items()}
    reached, pending = set(), list(initial)
    while pending:
        path = pending.pop()
        if path in reached:
            continue
        reached.add(path)
        if path not in imports:
            raise ValueError("verification scope omits native import evidence: " + path)
        pending.extend(by_name[name] for name in imports[path] if name in by_name)
    return reached


def errors(policy: dict, baseline_layout: dict | None = None) -> list[str]:
    """Validate persisted scope without running Lake or trusting agent metadata."""
    try:
        if not isinstance(policy, dict) or policy.get("sha256") != _seal(policy)["sha256"]:
            return ["verification scope integrity mismatch"]
        if policy.get("version") != 1 or policy.get("mode") not in {"all", "libraries"}:
            return ["unsupported verification scope"]
        if policy["mode"] == "all":
            return [] if set(policy) == {"version", "mode", "sha256"} else ["invalid all-project scope"]
        if set(policy) != {"version", "mode", "sha256", "selected_libraries", "original_modules",
                           "original_owners", "verification_modules", "editable_modules", "imports"}:
            return ["incomplete library verification scope"]
        modules, owners, libraries = _ownership({"modules": policy["original_modules"],
            "module_owners": policy["original_owners"], "libraries": policy["selected_libraries"]})
        editable, verified, imports = policy["editable_modules"], policy["verification_modules"], policy["imports"]
        if (not libraries or not _modules(editable) or not editable or not _modules(verified)
                or editable != _eligible(modules, owners, libraries)
                or not editable.items() <= verified.items() or not verified.items() <= modules.items()
                or not isinstance(imports, dict) or set(imports) != set(verified)
                or any(not _strings(value) for value in imports.values())
                or _walk(modules, editable, imports) != set(verified)):
            return ["invalid library verification boundary or import closure"]
        if baseline_layout is not None:
            current = _ownership(baseline_layout)
            if current != (modules, owners, libraries):
                return ["verification scope differs from original Lake ownership"]
            for key, expected in (("verification_modules", verified), ("editable_modules", editable),
                                  ("project_scope", "libraries"), ("scope_sha256", policy["sha256"])):
                if key in baseline_layout and baseline_layout[key] != expected:
                    return ["verification scope annotations differ from sealed policy"]
        return []
    except (ValueError, TypeError, KeyError, AttributeError):
        return ["malformed verification scope"]


def mode(baseline: dict | None) -> str:
    """Older baselines are all-project; invalid present policies never downgrade."""
    if ((baseline or {}).get("version") == 2
            or (baseline or {}).get("project_scope") == "changes"
            or (baseline or {}).get("layout", {}).get("project_scope") == "changes"
            or (baseline or {}).get("policy") == "changes-v1"):
        from . import bump_delta
        issues = bump_delta.baseline_errors(baseline)
        if issues:
            raise ValueError("; ".join(issues))
        return "changes"
    policy = (baseline or {}).get("verification_scope")
    if policy is None:
        if (baseline or {}).get("layout", {}).get("project_scope") == "libraries":
            raise ValueError("library baseline is missing its verification scope")
        return "all"
    issues = errors(policy, baseline.get("layout"))
    if issues:
        raise ValueError("; ".join(issues))
    return policy["mode"]


def _headers(root, files: set[str]) -> dict:
    from . import bump_workspace
    imports = bump_workspace.read_imports(root, sorted(files))
    if (not isinstance(imports, dict) or set(imports) != files
            or any(not isinstance(value, list) or any(not isinstance(name, str) or not name for name in value)
                   for value in imports.values())):
        raise ValueError("native Lean import-header evidence is incomplete")
    return {path: sorted(set(names)) for path, names in imports.items()}


def capture(root, layout: dict, mode: str = "all") -> dict:
    if mode not in {"all", "libraries"}:
        raise ValueError("project scope must be all or libraries")
    if mode == "all":
        return _seal({"version": 1, "mode": "all"})
    modules, owners, libraries = _ownership(layout)
    editable = _eligible(modules, owners, libraries)
    if not libraries or not editable:
        raise ValueError("libraries scope requires a nonempty configured Lean library; executable-only projects need all scope")
    imports, verified, pending = {}, {}, set(editable)
    by_name = {name: path for path, name in modules.items()}
    while pending:
        batch = _headers(root, pending)
        imports.update(batch)
        verified.update({path: modules[path] for path in pending})
        pending = {by_name[name] for names in batch.values() for name in names
                   if name in by_name and by_name[name] not in verified}
    policy = _seal({"version": 1, "mode": "libraries", "selected_libraries": libraries,
                    "original_modules": modules, "original_owners": owners,
                    "verification_modules": verified, "editable_modules": editable, "imports": imports})
    issues = errors(policy, layout)
    if issues:
        raise ValueError("; ".join(issues))
    return policy


def apply(root, layout: dict, policy: dict) -> dict:
    issues = errors(policy)
    if issues:
        raise ValueError("; ".join(issues))
    result = copy.deepcopy(layout)
    if policy["mode"] == "all":
        result.update(verification_modules=copy.deepcopy(layout["modules"]),
                      editable_modules=copy.deepcopy(layout["modules"]),
                      project_scope="all", scope_sha256=policy["sha256"])
        return result
    modules, owners, libraries = _ownership(layout)
    if libraries != policy["selected_libraries"]:
        raise ValueError("configured library selection changed from verification scope")
    original = policy["original_modules"]
    for path, name in original.items():
        if modules.get(path) != name or owners.get(path) != policy["original_owners"][path]:
            raise ValueError("original project module ownership changed: " + path)
    editable = _eligible(modules, owners, libraries)
    if (set(modules) - set(original)) - set(editable):
        raise ValueError("new source modules must belong to a selected Lean library")
    verified = {**policy["verification_modules"], **editable}
    imports = _headers(root, set(verified))
    excluded_names = {name: path for path, name in modules.items() if path not in verified}
    crossed = {excluded_names[name] for names in imports.values() for name in names if name in excluded_names}
    if crossed:
        raise ValueError("new imports cross the frozen verification boundary: " + ", ".join(sorted(crossed)))
    reached = _walk(modules, verified, imports)
    if reached - set(verified):
        raise ValueError("new imports cross the frozen verification boundary: " + ", ".join(sorted(reached - set(verified))))
    result.update(verification_modules=verified, editable_modules=editable,
                  project_scope="libraries", scope_sha256=policy["sha256"])
    return result


def require_writable_paths(baseline: dict, paths) -> None:
    """Pure pre-application guard: manifests cannot thaw existing auxiliary files.

    New paths still require native selected-library ownership after application;
    this cheap guard does not manufacture ownership from directory prefixes.
    """
    if baseline.get("policy") == "migration-v1":
        original_paths = {row["path"] for row in baseline.get("compiler_modules", {}).values()}
        for path in paths:
            if path not in original_paths:
                raise ValueError("migration may only edit inventoried original modules: " + str(path))
        return
    selected_mode = mode(baseline)
    if selected_mode == "all":
        return
    if selected_mode == "changes":
        for path in paths:
            if not _path(path):
                raise ValueError("not an eligible changed source path: " + str(path))
            if path in baseline.get("files", {}) and path not in baseline["layout"]["modules"]:
                raise ValueError("existing non-module project input cannot be claimed: " + path)
        return
    policy = baseline["verification_scope"]
    for path in paths:
        if not _path(path):
            raise ValueError("not an eligible library source path: " + str(path))
        if path in baseline.get("files", {}) and path not in policy["editable_modules"]:
            raise ValueError("frozen auxiliary project source cannot be edited or claimed: " + path)
