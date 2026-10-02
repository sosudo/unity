"""Change-focused existing-project preservation, versioned independently of v1.

The original Git tree supplies comparison inputs. Native environments are built
one module at a time: sibling libraries never share a synthetic import context.
This is a preservation policy, not a sandbox for hostile Lean metaprograms.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import io
import json
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path

POLICY = "changes-v1"


def is_changes(baseline: dict | None) -> bool:
    return isinstance(baseline, dict) and baseline.get("version") == 2 and baseline.get("policy") == POLICY


def _original_bytes(root: Path, baseline: dict, name: str) -> bytes:
    from . import bump_project as project
    if not project._safe_path(name) or name not in baseline["files"]:
        raise ValueError("not an original project input: " + name)
    result = subprocess.run(["git", "show", f"{baseline['head']}:{name}"], cwd=root, capture_output=True)
    if result.returncode or hashlib.sha256(result.stdout).hexdigest() != baseline["files"][name]:
        raise ValueError("immutable Git input does not match the original baseline: " + name)
    return result.stdout


def errors(baseline: dict) -> list[str]:
    from . import bump_project as project
    try:
        if (not is_changes(baseline) or baseline.get("project_scope") != "changes"
                or "verification_scope" in baseline):
            return ["unsupported change-focused preservation policy"]
        if project._seal(baseline)["sha256"] != baseline.get("sha256"):
            return ["existing-project baseline integrity mismatch"]
        required = {"branch", "head", "files", "tracked_files", "environment", "layout", "declarations",
                    "target_scope", "scope", "project_axioms", "project_sorries", "project_used_axioms",
                    "original_contexts", "import_headers"}
        if not required <= baseline.keys():
            return ["change-focused baseline is incomplete"]
        files, layout, scope = baseline["files"], baseline["layout"], baseline["scope"]
        if (any(not isinstance(baseline[k], str) or not baseline[k] for k in ("branch", "head", "target_scope"))
                or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", baseline["head"])
                or not isinstance(files, dict) or any(not project._safe_path(p) or not re.fullmatch(r"[0-9a-f]{64}", h)
                                                     for p, h in files.items())
                or not isinstance(baseline["tracked_files"], list) or not set(files) <= set(baseline["tracked_files"])
                or not isinstance(baseline["environment"], dict) or not isinstance(layout, dict)
                or not isinstance(layout.get("modules"), dict) or not isinstance(layout.get("build_dir"), str)
                or layout.get("project_scope") != "changes" or not isinstance(scope, dict)
                or not isinstance(layout.get("default_modules"), dict)
                or not layout["default_modules"].items() <= layout["modules"].items()
                or layout.get("unknown_default_targets") != []
                or scope.get("mode") not in {"natural", "explicit"} or not isinstance(scope.get("bound"), bool)
                or not isinstance(scope.get("existing_targets"), list)
                or any(not isinstance(n, str) or not n for n in scope["existing_targets"])
                or len(scope["existing_targets"]) != len(set(scope["existing_targets"]))):
            return ["invalid change-focused baseline identity/scope"]
        headers = baseline["import_headers"]
        if (not isinstance(headers, dict) or set(headers) != set(layout["default_modules"])
                or any(not isinstance(v, list) or any(not isinstance(n, str) for n in v) for v in headers.values())):
            return ["incomplete original import-header evidence"]
        if not isinstance(baseline["original_contexts"], dict) or not isinstance(baseline["declarations"], dict):
            return ["invalid original module-context evidence"]
        selected = set(scope["existing_targets"])
        combined = {}
        for module, receipt in baseline["original_contexts"].items():
            if not _receipt_valid(receipt, baseline, module):
                return ["invalid original module-context receipt: " + module]
            for name, row in receipt["inspection"]["project_records"].items():
                if name not in selected:
                    continue
                if name in combined and combined[name] != row:
                    return ["ambiguous selected existing declaration across module contexts: " + name]
                combined[name] = row
        if combined != baseline["declarations"]:
            return ["lazy original declaration evidence differs from its module contexts"]
        for key in ("project_axioms", "project_sorries", "project_used_axioms"):
            expected = sorted({n for receipt in baseline["original_contexts"].values()
                               for n in receipt["inspection"][key] if n in selected or key == "project_used_axioms"})
            if baseline[key] != expected:
                return ["lazy original hole evidence differs from its module contexts"]
        if scope["bound"] and not set(scope["existing_targets"]) <= set(combined):
            return ["bound existing targets lack original native evidence"]
        return []
    except (ValueError, TypeError, KeyError, AttributeError):
        return ["malformed change-focused baseline"]


def baseline_origin_matches(original: dict, bound: dict) -> bool:
    if errors(original) or errors(bound):
        return False
    mutable = {"sha256", "origin_sha256", "scope", "declarations", "original_contexts",
               "project_axioms", "project_sorries", "project_used_axioms"}
    if ({k: v for k, v in original.items() if k not in mutable}
            != {k: v for k, v in bound.items() if k not in mutable}):
        return False
    if bound.get("origin_sha256", bound["sha256"]) != original.get("origin_sha256", original["sha256"]):
        return False
    if any(bound["original_contexts"].get(m) != r for m, r in original["original_contexts"].items()):
        return False
    before, after = original["scope"], bound["scope"]
    if set(before) != set(after) or before["mode"] != after["mode"]:
        return False
    if before["bound"]:
        return before == after
    if before["mode"] == "explicit" and before["existing_targets"] != after["existing_targets"]:
        return False
    return (before == after or after["bound"] is True)


baseline_matches = baseline_origin_matches
baseline_errors = errors


def capture(root: Path, target_scope: str) -> dict:
    from . import bump_contract as contract, bump_project as project, bump_scope
    root = Path(root).resolve()
    project._require_clean(root)
    head, branch = project._git(root, "rev-parse", "HEAD"), project._git(root, "branch", "--show-current")
    if not branch:
        raise ValueError("existing-project baseline requires a named branch")
    if not isinstance(target_scope, str) or not target_scope.strip():
        raise ValueError("target scope must be nonempty")
    if target_scope.strip().lower() == "all":
        raise ValueError("changes scope requires bounded targets; use explicit --project-scope all for a whole-project hole inventory")
    layout = contract.workspace_layout(root)
    layout = {**layout, "project_scope": "changes", "verification_modules": {},
              "editable_modules": copy.deepcopy(layout["modules"])}
    before, tracked = contract._file_hashes(root, build_dir=layout["build_dir"]), project._tracked(root)
    if set(before) - set(tracked):
        raise ValueError("existing-project inputs are ignored/untracked and cannot be preserved in worktrees: "
                         + ", ".join(sorted(set(before) - set(tracked))))
    environment = contract.environment_identity(root)
    build = contract.build_sources(root, full=True, layout=layout, task_id="project-baseline")
    if build["returncode"]:
        raise ValueError("existing project must build before formalization: " + build["output"][-3000:])
    if "default_modules" not in layout or layout.get("unknown_default_targets"):
        raise ValueError("change-focused preservation requires native default-target module coverage; unsupported custom default target")
    normal = dict(layout["default_modules"])
    by_name = {module: path for path, module in layout["modules"].items()}
    headers = {}
    pending = set(normal)
    while pending:
        batch = bump_scope._headers(root, pending)
        headers.update(batch)
        pending = set()
        for names in batch.values():
            for imported in names:
                dependency = by_name.get(imported)
                if dependency is not None and dependency not in headers:
                    normal[dependency] = imported
                    pending.add(dependency)
    layout["default_modules"] = dict(sorted(normal.items()))
    project._require_clean(root)
    if (head != project._git(root, "rev-parse", "HEAD")
            or before != contract._file_hashes(root, build_dir=layout["build_dir"])
            or environment != contract.environment_identity(root)):
        raise ValueError("project inputs changed while capturing the existing-project baseline")
    tokens = [x for x in re.split(r"[,\s]+", target_scope.strip()) if x]
    explicit = all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*", x) for x in tokens) and (
        len(tokens) == 1 or "," in target_scope or any("." in x for x in tokens))
    return project._seal({"version": 2, "policy": POLICY, "project_scope": "changes", "project_root": str(root),
        "branch": branch, "head": head, "files": before, "tracked_files": tracked,
        "environment": environment, "layout": layout, "declarations": {}, "original_contexts": {},
        "import_headers": headers, "target_scope": target_scope.strip(),
        "scope": {"mode": "explicit" if explicit else "natural", "bound": False,
                  "existing_targets": sorted(set(tokens)) if explicit else []},
        "project_axioms": [], "project_sorries": [], "project_used_axioms": []})


def verification_modules(root: Path, baseline: dict, layout: dict, tasks=None) -> dict:
    """Return exact changed/new and reverse-import-dependent modules, not siblings."""
    from . import bump_contract as contract, bump_scope
    current = contract._file_hashes(root, build_dir=layout["build_dir"])
    modules = layout["modules"]
    changed = {p for p in modules if current.get(p) != baseline["files"].get(p)}
    selected_modules = {row["module"] for row in baseline["declarations"].values()}
    changed.update(p for p, module in modules.items() if module in selected_modules)
    for row in tasks or []:
        path = row.get("lean_file")
        if path in modules:
            changed.add(path)
    if not changed:
        return {}
    by_name = {module: path for path, module in modules.items()}
    current_headers, pending = {}, set(baseline["layout"]["default_modules"]) | changed
    while pending:
        batch = bump_scope._headers(root, pending)
        current_headers.update(batch)
        pending = {by_name[n] for path, names in batch.items()
                   for n in set(names) | set(baseline["import_headers"].get(path, []))
                   if n in by_name and by_name[n] not in current_headers}
    graph = {p: set(names) | set(baseline["import_headers"].get(p, []))
             for p, names in current_headers.items()}
    used, pending = set(changed), set(changed)
    while pending:
        path = pending.pop()
        for imported in graph[path]:
            dependency = by_name.get(imported)
            if dependency is not None and dependency not in used:
                used.add(dependency)
                pending.add(dependency)
    eligible_dependents = set(baseline["layout"]["default_modules"]) | used
    affected = set(changed)
    while True:
        names = {modules[p] for p in affected}
        added = {p for p in eligible_dependents if p in modules and graph[p] & names} - affected
        if not added:
            break
        affected.update(added)
    return {p: modules[p] for p in sorted(affected)}


def apply(root: Path, layout: dict, baseline: dict, tasks=None) -> dict:
    return {**copy.deepcopy(layout), "project_scope": "changes",
            "verification_modules": verification_modules(root, baseline, layout, tasks),
            "editable_modules": copy.deepcopy(layout["modules"]),
            "scope_sha256": baseline["sha256"]}


def _receipt_identity(baseline: dict, module: str) -> dict:
    from . import bump_project as project, bump_contract as contract
    return {"version": 1, "policy": POLICY, "head": baseline["head"], "module": module,
            "files_sha256": project._digest(baseline["files"]), "environment": baseline["environment"],
            "inspector_policy_sha256": contract.policy_hash()}


def _receipt_valid(receipt: dict, baseline: dict, module: str) -> bool:
    from . import bump_project as project
    try:
        if not isinstance(receipt, dict) or receipt.get("sha256") != project._seal(receipt)["sha256"]:
            return False
        if any(receipt.get(k) != v for k, v in _receipt_identity(baseline, module).items()):
            return False
        return all(row["module"] == module for row in project._records(receipt["inspection"]).values())
    except (KeyError, TypeError, ValueError):
        return False


def original_context(root: Path, baseline: dict, module: str) -> dict:
    """Inspect only immutable Git sources using separate root build artifacts."""
    from . import bump_contract as contract, bump_project as project
    if module not in baseline["layout"]["modules"].values():
        raise ValueError("not an original module: " + module)
    if contract._dependencies(root) != baseline["environment"]["dependencies"]:
        raise ValueError("pinned dependencies changed before original-context inspection")
    stored = baseline["original_contexts"].get(module)
    if stored is not None:
        if not _receipt_valid(stored, baseline, module):
            raise ValueError("invalid original-context receipt")
        return copy.deepcopy(stored)
    identity = _receipt_identity(baseline, module)
    cache = root / ".unity" / "bump-original-contexts"
    cache.mkdir(parents=True, exist_ok=True)
    cache_file = cache / (project._digest(identity) + ".json")
    try:
        cached = json.loads(cache_file.read_text())
        if _receipt_valid(cached, baseline, module):
            return cached
    except (OSError, ValueError):
        pass
    archive = subprocess.run(["git", "archive", "--format=tar", baseline["head"]], cwd=root, capture_output=True)
    if archive.returncode:
        raise ValueError("cannot recover immutable original Git tree")
    with tempfile.TemporaryDirectory(prefix="original-", dir=cache) as directory:
        original = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as source:
            members = {member.name: member for member in source.getmembers()}
            for name, expected in baseline["files"].items():
                member = members.get(name)
                if member is None or not member.isfile() or not project._safe_path(name):
                    raise ValueError("unsafe/missing original Git input: " + name)
                content = source.extractfile(member).read()
                if hashlib.sha256(content).hexdigest() != expected:
                    raise ValueError("original Git file differs from captured baseline: " + name)
                path = original / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        # Lake dependency checkouts are shared only after their exact source
        # fingerprints matched. The root build directory is never shared.
        manifest = json.loads((original / "lake-manifest.json").read_text()) if (original / "lake-manifest.json").exists() else {}
        packages_dir = Path(manifest.get("packagesDir", ".lake/packages"))
        if packages_dir.is_absolute() or ".." in packages_dir.parts:
            raise ValueError("unsupported external package directory for original context")
        package_link = original / packages_dir
        if (root / packages_dir).is_dir() and not package_link.exists():
            package_link.parent.mkdir(parents=True, exist_ok=True)
            package_link.symlink_to((root / packages_dir).resolve(), target_is_directory=True)
        for package in manifest.get("packages", []):
            if package.get("type") != "path":
                continue
            path = Path(package["dir"])
            if path.is_absolute():
                continue
            if ".." in path.parts:
                raise ValueError("original-context inspection requires an absolute or in-project path dependency")
            target = original / path
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to((root / path).resolve(), target_is_directory=True)
        layout = copy.deepcopy(baseline["layout"])
        layout["verification_modules"] = {p: m for p, m in layout["modules"].items() if m == module}
        build = contract.build_sources(original, full=False, layout=layout, task_id="original-context")
        if build["returncode"]:
            raise ValueError("original affected module does not build in its own context: " + module + ": " + build["output"][-2000:])
        inspection = contract.inspect_environment(original, [], layout=layout, module_context=[module], _inventory_only=True)
        project._records(inspection)
        if any(row["module"] != module for row in inspection["project_records"].values()):
            raise ValueError("original inspector escaped the requested module context")
        for name, expected in baseline["files"].items():
            if hashlib.sha256((original / name).read_bytes()).hexdigest() != expected:
                raise ValueError("original Git inputs changed during native inspection")
        if contract._dependencies(root) != baseline["environment"]["dependencies"]:
            raise ValueError("pinned dependency sources changed during original inspection")
        receipt = project._seal({**identity, "inspection": inspection})
    temporary = cache_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    temporary.replace(cache_file)
    return receipt


def bind_scope(root: Path | None, baseline: dict, dag: dict) -> dict:
    from . import bump_project as project
    issues = errors(baseline)
    if issues:
        raise ValueError("; ".join(issues))
    proposed = dag.get("existing_targets")
    if (not isinstance(proposed, list) or any(not isinstance(n, str) or not n for n in proposed)
            or len(proposed) != len(set(proposed))):
        raise ValueError("plan requires unique exact existing_targets")
    result = copy.deepcopy(baseline)
    scope = result["scope"]
    if (scope["bound"] or scope["mode"] == "explicit") and set(proposed) != set(scope["existing_targets"]):
        raise ValueError("plan omits or changes selected existing targets")
    if scope["bound"]:
        # Refinement plans deliberately normalize away candidate output
        # manifests. They do not need to rediscover already pinned original
        # declarations, nor may they manufacture a new baseline or scope.
        return result
    if proposed:
        if root is None:
            raise ValueError("existing-target binding requires immutable native module context inspection")
        candidates = {}
        for chunk in dag.get("chunks", []):
            for row in [chunk, *chunk.get("outputs", [])]:
                name, path = row.get("declaration", row.get("lean_decl")), row.get("lean_file")
                if name in proposed and path in baseline["layout"]["modules"]:
                    candidates[name] = baseline["layout"]["modules"][path]
        # Existing declarations must have an explicit planned source file. Never
        # search every library by importing it into one speculative environment.
        missing = set(proposed) - set(candidates)
        if missing:
            raise ValueError("existing targets need exact original lean_file outputs: " + ", ".join(sorted(missing)))
        for module in sorted(set(candidates.values())):
            result["original_contexts"][module] = original_context(Path(root), baseline, module)
        combined = {}
        for receipt in result["original_contexts"].values():
            for name, row in receipt["inspection"]["project_records"].items():
                if name not in proposed:
                    continue
                if name in combined and combined[name] != row:
                    raise ValueError("ambiguous existing target across module contexts: " + name)
                combined[name] = row
        result["declarations"] = combined
        for key in ("project_axioms", "project_sorries", "project_used_axioms"):
            result[key] = sorted({n for receipt in result["original_contexts"].values()
                                  for n in receipt["inspection"][key] if n in proposed or key == "project_used_axioms"})
        holes = set(result["project_axioms"]) | set(result["project_sorries"])
        for name in proposed:
            if name not in combined or combined[name]["module"] != candidates[name]:
                raise ValueError("existing target not found in its original module: " + name)
        if scope["mode"] == "natural" and set(proposed) - holes:
            raise ValueError("natural-language plan may only select existing incomplete declarations")
        project._require_editable_targets(proposed, combined, holes)
    scope.update(existing_targets=sorted(proposed), bound=True)
    result.setdefault("origin_sha256", baseline["sha256"])
    return project._seal(result)


def _masked(text: str) -> str:
    """Mask comments/strings without moving offsets; reject unterminated syntax."""
    out, index, depth, quoted = list(text), 0, 0, False
    while index < len(text):
        if depth:
            if text.startswith("/-", index):
                depth += 1; out[index:index + 2] = "  "; index += 2; continue
            if text.startswith("-/", index):
                depth -= 1; out[index:index + 2] = "  "; index += 2; continue
            if text[index] != "\n": out[index] = " "
            index += 1; continue
        if quoted:
            if text[index] == "\\":
                out[index:index + 2] = " " * len(text[index:index + 2]); index += 2; continue
            if text[index] == '"': quoted = False
            if text[index] != "\n": out[index] = " "
            index += 1; continue
        if text.startswith("--", index):
            end = text.find("\n", index)
            if end < 0: end = len(text)
            out[index:end] = " " * (end - index); index = end; continue
        if text.startswith("/-", index):
            depth = 1; out[index:index + 2] = "  "; index += 2; continue
        if text[index] == '"': quoted = True; out[index] = " "
        index += 1
    if depth or quoted:
        raise ValueError("unterminated comment/string in changed Lean input")
    return "".join(out)


_DECL = re.compile(r"(?m)^(?:@[\[][^\n]*\]\s*)?(?:(?:private|protected|noncomputable)\s+)*(theorem|lemma|def|opaque|axiom)\s+([A-Za-z_][\w'.]*)\b")
_COMMAND = re.compile(r"(?m)^[ \t]*(?:@[\[][^\n]*\]\s*)?(?:(?:private|protected|noncomputable|local|scoped)\s+)*(?:theorem|lemma|def|opaque|axiom|instance|structure|class|inductive|namespace|section|end|open|variable|universe|attribute|notation|infix|prefix|postfix|syntax|macro|elab|initialize|set_option|export|import|mutual)\b")
# Commands can be nested under `open ... in`, `set_option ... in`, etc.
# Scan keyword tokens anywhere outside comments/strings, not merely line starts.
# This deliberately rejects some benign local syntax in edited fragments rather
# than pretending that kernel declaration records cover persistent attributes.
_ENV_MUTATION = re.compile(r"(?<![\w.'])(?:attribute|export|notation|infix[lr]?|prefix|postfix|syntax|macro|elab|initialize|builtin_initialize|run_cmd|run_elab|set_option)\b")


def _import_insertion(original: str, offset: int, addition: str) -> bool:
    """New header imports may precede old commands; native contexts recheck them."""
    if offset and original[offset - 1] != "\n":
        return False
    header = re.compile(r"(?:(?:(?:public|meta)\s+)*import(?:\s+[\w'.«»]+)+|prelude)")
    prefix = [line.strip() for line in _masked(original[:offset]).splitlines() if line.strip()]
    added = [line.strip() for line in _masked(addition).splitlines() if line.strip()]
    return (bool(added) and all(header.fullmatch(line) for line in prefix)
            and all(header.fullmatch(line) and line != "prelude" for line in added)
            and addition.endswith("\n"))


def require_source_edits(root: Path, baseline: dict, *, allowed_new_paths=()) -> None:
    """Protect every old command; append declarations or fill selected bodies.

    Unsupported source syntax fails closed rather than granting whole-file edit
    rights. Native signature/body comparison remains authoritative afterwards.
    """
    from . import bump_project as project
    selected = set(baseline["scope"]["existing_targets"])
    for path in allowed_new_paths:
        file = root / path
        if not file.is_file():
            continue
        current = file.read_text()
        if path not in baseline["files"]:
            if _ENV_MUTATION.search(_masked(current)):
                raise ValueError("new module contains unsupported environment-changing commands: " + path)
            continue
        original = _original_bytes(root, baseline, path).decode()
        if current == original:
            continue
        masked = _masked(original)
        regions = []
        commands = list(_COMMAND.finditer(masked))
        for match in _DECL.finditer(masked):
            short = match.group(2)
            candidates = [n for n in selected if n == short or n.endswith("." + short)]
            if len(candidates) != 1:
                continue
            name = candidates[0]
            row = baseline["declarations"].get(name, {})
            if row.get("module") != baseline["layout"]["modules"].get(path):
                continue
            if name not in set(baseline["project_axioms"]) | set(baseline["project_sorries"]):
                continue
            end = next((cmd.start() for cmd in commands if cmd.start() > match.start()), len(original))
            assignment = masked.find(":=", match.end(), end)
            if match.group(1) == "axiom":
                # Axiom-to-theorem syntax changes the command header. Its
                # exact kernel statement/kind transition is checked natively.
                regions.append((match.start(), end))
            elif assignment >= 0:
                regions.append((assignment + 2, end))
        matcher = difflib.SequenceMatcher(a=original, b=current, autojunk=False)
        for tag, a0, a1, b0, b1 in matcher.get_opcodes():
            if tag == "equal":
                continue
            if a0 == a1 == len(original):
                addition = current[b0:b1]
                if original and not original.endswith("\n") and not addition.startswith("\n"):
                    raise ValueError("append must start after an original command boundary: " + path)
                if _ENV_MUTATION.search(_masked(addition)):
                    raise ValueError("appended source changes protected environment commands: " + path)
                continue
            if a0 == a1 and _import_insertion(original, a0, current[b0:b1]):
                continue
            if not any(start <= a0 <= a1 <= end for start, end in regions):
                raise ValueError("protected existing source command changed: " + path)
            if _ENV_MUTATION.search(_masked(current[b0:b1])):
                raise ValueError("selected proof edit introduces an environment-changing command: " + path)
        # Replacements cannot smuggle a second command into the authorized
        # theorem body. Axiom replacements permit only their one declaration.
        if _ENV_MUTATION.search(_masked(current)) and not _ENV_MUTATION.search(masked):
            raise ValueError("changed source introduces environment-changing commands: " + path)


def validate(root: Path, baseline: dict, inspection: dict | None = None, *, final=False,
             allowed_new_paths=(), allowed_incomplete_declarations=(), claimed_declarations=None,
             allowed_incomplete_files=None) -> list[str]:
    from . import bump_contract as contract, bump_project as project
    issues = errors(baseline)
    if issues:
        return issues
    if final and not baseline["scope"]["bound"]:
        issues.append("existing-project scope has not been bound to exact targets")
    try:
        if str(root) == baseline.get("project_root") or (root / ".git").is_dir():
            project.require_original_branch(root, baseline)
        project.require_pinned_inputs(root, baseline, allowed_new_paths=allowed_new_paths)
        project._git(root, "merge-base", "--is-ancestor", baseline["head"], "HEAD")
        layout = contract.workspace_layout(root)
        for path, module in baseline["layout"]["modules"].items():
            if layout["modules"].get(path) != module:
                raise ValueError("existing module ownership changed: " + path)
        if contract.environment_identity(root) != baseline["environment"]:
            raise ValueError("existing-project toolchain/build configuration/dependency bytes changed")
        current_files = contract._file_hashes(root, build_dir=layout["build_dir"])
        unauthorized = set(current_files) - set(baseline["files"]) - set(allowed_new_paths)
        if unauthorized:
            raise ValueError("unapproved new project input: " + ", ".join(sorted(unauthorized)))
        selected = set(baseline["scope"]["existing_targets"])
        affected = verification_modules(root, baseline, layout)
        claims = {} if claimed_declarations is None else claimed_declarations
        if allowed_incomplete_files is not None and (
                not isinstance(allowed_incomplete_files, dict)
                or any(not isinstance(name, str) or not isinstance(files, (set, list, tuple))
                       or any(not isinstance(path, str) or path not in allowed_new_paths for path in files)
                       for name, files in allowed_incomplete_files.items())):
            raise ValueError("invalid exact-file placeholder provenance")
        if (not isinstance(claims, dict) or any(not isinstance(n, str) or not isinstance(p, str)
                                               for n, p in claims.items())):
            raise ValueError("invalid controller output declaration bindings")
        for path in claims.values():
            if path not in layout["modules"]:
                raise ValueError("claimed output has no owned module: " + path)
            affected[path] = layout["modules"][path]
        current_contexts = (inspection or {}).get("contexts", {})
        for module in sorted(set(affected.values())):
            context_layout = {**layout, "project_scope": "changes", "verification_modules":
                              {p: m for p, m in layout["modules"].items() if m == module}}
            current = current_contexts.get(module)
            if current is None:
                current = contract.inspect_environment(root, [], layout=context_layout,
                                                       module_context=[module], _inventory_only=True)
            records = project._records(current)
            if any(row["module"] != module for row in records.values()):
                raise ValueError("current native inventory escaped its module context: " + module)
            previous = (original_context(root, baseline, module)["inspection"]
                        if module in baseline["layout"]["modules"].values()
                        else {"project_records": {}, "project_axioms": [], "project_sorries": [], "project_used_axioms": []})
            holes = set(previous["project_axioms"]) | set(previous["project_sorries"])
            for name, path in claims.items():
                if (layout["modules"][path] == module and name in previous["project_records"]
                        and name not in selected):
                    issues.append(f"output claims protected existing declaration in {module}: {name}")
            for name, before in previous["project_records"].items():
                after = records.get(name)
                if after is None:
                    issues.append(f"existing declaration removed in {module}: {name}")
                elif name not in selected or name not in holes:
                    if project._preserved_meaning(before) != project._preserved_meaning(after):
                        issues.append(f"protected existing declaration changed in {module}: {name}")
                else:
                    if project._signature(before) != project._signature(after):
                        issues.append(f"existing target signature changed in {module}: {name}")
                    if before["target_kind"] != after["target_kind"] and (before["target_kind"], after["target_kind"]) != ("axiom", "theorem"):
                        issues.append(f"existing target declaration kind changed: {name}")
                    if before["target_kind"] not in {"axiom", "theorem"}:
                        if ({k: v for k, v in before["declaration_meaning"].items() if k != "value"}
                                != {k: v for k, v in after["declaration_meaning"].items() if k != "value"}):
                            issues.append(f"existing partial definition metadata changed: {name}")
            new_axioms = set(current["project_axioms"]) - set(previous["project_axioms"])
            if new_axioms:
                issues.append(f"new project axioms in {module}: " + ", ".join(sorted(new_axioms)))
            permitted = set(allowed_incomplete_declarations)
            if allowed_incomplete_files is not None:
                permitted &= {name for name, files in allowed_incomplete_files.items()
                              if any(layout["modules"].get(path) == module for path in files)}
                # Retained provenance permits unfinished theorem proofs, never
                # holes in a reopened definition or an unrelated same-name module.
                permitted &= {name for name, row in records.items()
                              if row["target_kind"] == "theorem" and '"sorryAx"' not in
                              json.dumps([row["type"], row["declaration_meaning"]])}
            allowed_holes = set(previous["project_sorries"]) | (set() if final else permitted)
            new_holes = set(current["project_sorries"]) - allowed_holes
            if new_holes:
                issues.append(f"new out-of-scope proof holes in {module}: " + ", ".join(sorted(new_holes)))
            forbidden = set(current["project_used_axioms"]) - set(previous["project_used_axioms"]) - contract.AXIOMS - {"sorryAx"}
            if forbidden:
                issues.append(f"new forbidden axiom dependencies in {module}: " + ", ".join(sorted(forbidden)))
            if final and selected & (set(current["project_axioms"]) | set(current["project_sorries"])):
                issues.append("selected existing targets remain incomplete in " + module)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        issues.append("cannot verify existing-project changes: " + str(exc))
    return issues
