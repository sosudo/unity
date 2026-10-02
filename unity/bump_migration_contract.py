"""Fail-closed, two-environment semantic checks for Bump.

Receipts attest to exactly inspected compiled contexts; the project controller
must additionally bind them to its immutable original/target source snapshots.
Structural mismatch is a blocker, not proof that a migration is mathematically
wrong. This initial policy does not claim arbitrary cross-version equivalence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from . import bump_migration_project as bump_project, bump_cache as formalize_cache

SCHEMA_VERSION = 2
DECLARATION_INVENTORY = "raw-module-constants-v1"
_KINDS = {"theorem", "def", "opaque", "axiom", "inductive", "constructor", "recursor", "quot"}
_CONFIGS = ("lean-toolchain", "lakefile.lean", "lakefile.toml", "lake-manifest.json")
_NATIVE = re.compile(r"(?:^|\.)_native(?:\.[^.]+)*\.ax(?:_[0-9]+)+$")
_STANDARD_ASSUMPTIONS = {
    ("str", ("anonymous",), "propext"): {"Init.Core"},
    ("str", ("str", ("anonymous",), "Classical"), "choice"): {"Init.Prelude"},
    ("str", ("str", ("anonymous",), "Quot"), "sound"): {"Init.Core", "Init.Prelude"},
}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def policy_hash() -> str:
    return digest({p.name: _file_digest(p) for p in
                   (Path(__file__), Path(__file__).with_name("bump_inspect.lean"))})


def _run(root: Path, command: list[str]):
    # The Bump project runner supplies an explicit per-environment cache and
    # toolchain environment; never alter process-global environment variables.
    result = bump_project._run(root, command)
    if result.returncode:
        raise ValueError("Bump native inspection/build failed: " +
                         (result.stderr or result.stdout or str(result.returncode))[-2000:])
    return result


def _source_hashes(root: Path) -> dict:
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in {".git", ".unity", ".lake", ".worktrees", ".bump-runtime"})
        for name in sorted(files):
            if not name.endswith(".lean"):
                continue
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError("Bump source must not be a symlink: " + str(path.relative_to(root)))
            result[path.relative_to(root).as_posix()] = _file_digest(path)
    return result


def _native_executable(root: Path, source: Path, environment: dict) -> Path:
    """Own Bump cache; exact source/compiler binding, with atomic publication."""
    source_sha = _file_digest(source)
    recipe = {"version": 1, "source": source_sha, "environment": environment,
              "link_flags": ["-rdynamic"], "compile_flags": ["-R", "-c"]}
    directory = root / ".unity" / "bin" / "bump-inspect" / digest(recipe)
    binary, receipt = directory / "bump-inspect", directory / "receipt.json"
    for path in [binary, receipt, directory, *directory.parents]:
        if path == root:
            break
        if path.is_symlink():
            raise ValueError("Bump native cache path must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True)
    if binary.is_file() and receipt.is_file():
        try:
            saved = json.loads(receipt.read_text())
            if saved == {"recipe": recipe, "binary_sha256": _file_digest(binary)}:
                return binary
        except (OSError, ValueError):
            pass
    with tempfile.TemporaryDirectory(prefix="building-", dir=directory) as staging:
        staging = Path(staging)
        generated, built = staging / "bump-inspect.c", staging / "bump-inspect"
        _run(root, ["lake", "env", "lean", "-R", str(source.parent), "-c", str(generated), str(source)])
        _run(root, ["lake", "env", "leanc", "-o", str(built), str(generated), "-rdynamic"])
        if not built.is_file() or _file_digest(source) != source_sha:
            raise ValueError("Bump native inspector changed or failed to compile")
        record = staging / "receipt.json"
        record.write_text(json.dumps({"recipe": recipe, "binary_sha256": _file_digest(built)}, sort_keys=True))
        os.replace(built, binary)
        os.replace(record, receipt)
    return binary


def _environment(root: Path) -> dict:
    config = {}
    for name in _CONFIGS:
        path = root / name
        if path.is_symlink():
            raise ValueError("Bump configuration must not be a symlink: " + name)
        if path.exists():
            config[name] = _file_digest(path)
    if "lean-toolchain" not in config or not ({"lakefile.lean", "lakefile.toml"} & config.keys()):
        raise ValueError("Bump inspection requires a pinned Lean project")
    version = _run(root, ["lake", "env", "lean", "--version"]).stdout.strip()
    prefix = Path(_run(root, ["lake", "env", "lean", "--print-prefix"]).stdout.strip())
    if not version or not prefix.is_absolute() or not prefix.is_dir():
        raise ValueError("Bump inspector could not identify the actual Lean toolchain")
    return {"config": config, "lean_version": version, "lean_sysroot": str(prefix.resolve()),
            "lean_binary_sha256": _file_digest(prefix / "bin" / "lean")}


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    """A colliding native display label must fail, never silently drop evidence."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate native JSON object key: " + key)
        result[key] = value
    return result


def _native_json(root: Path, executable: Path, module: str, owned: list[str], *, paths_only=False):
    result = _run(root, ["lake", "env", str(executable), module, "--owned", *owned,
                         *(["--paths-only"] if paths_only else [])])
    try:
        report = json.loads(result.stdout.strip().splitlines()[-1],
                            object_pairs_hook=_unique_json_object)
    except (ValueError, IndexError) as exc:
        raise ValueError("Bump inspector did not return a native JSON report") from exc
    if not isinstance(report, dict) or report.get("issues"):
        raise ValueError("Bump inspector reported missing/unsupported native evidence: " +
                         str(report.get("issues", []))[:1000] if isinstance(report, dict)
                         else "Bump inspector returned an invalid native report")
    if (report.get("schema_version") != SCHEMA_VERSION or report.get("module") != module
            or report.get("owned_modules") != owned
            or report.get("declaration_inventory") != DECLARATION_INVENTORY
            or type(report.get("raw_declaration_count")) is not int
            or report["raw_declaration_count"] < 0):
        raise ValueError("Bump inspector returned mismatched context identity")
    return report


def inspect_module(root: Path, module: str, owned_modules: list[str]) -> dict:
    """Build and inspect one actual native module; never combine sibling contexts.

Only native executable/toolchain preparation is cached. Imported artifact bytes
are observed before and after inspection. Caller owns source/config authorization
and must not run this on unchecked agent-modified Lake configuration.
"""
    root = Path(root).resolve(strict=True)
    if (not isinstance(module, str) or not re.fullmatch(r"[^\W\d]\w*(?:\.[^\W\d]\w*)*", module)
            or not isinstance(owned_modules, list) or module not in owned_modules
            or any(not isinstance(n, str) or not re.fullmatch(r"[^\W\d]\w*(?:\.[^\W\d]\w*)*", n)
                   for n in owned_modules)):
        raise ValueError("Bump inspection requires one exact owned module")
    owned = sorted(set(owned_modules))
    # Resolve/update dependencies outside this call. A build must not silently
    # establish or rewrite the identity being attested.
    before_environment = _environment(root)
    before_sources = _source_hashes(root)
    _run(root, ["lake", "--rehash", "build", "+" + module])
    helper = Path(__file__).with_name("bump_inspect.lean")
    helper_sha = _file_digest(helper)
    executable = _native_executable(root, helper, before_environment)
    executable_sha = _file_digest(executable)
    paths = _native_json(root, executable, module, owned, paths_only=True)
    before = formalize_cache.compiled_identity(paths.get("compiled_modules"))
    report = _native_json(root, executable, module, owned)
    after = formalize_cache.compiled_identity(report.get("compiled_modules"))
    if (before != after or paths["raw_declaration_count"] != report["raw_declaration_count"]
            or _environment(root) != before_environment or _source_hashes(root) != before_sources
            or _file_digest(helper) != helper_sha or _file_digest(executable) != executable_sha):
        raise ValueError("Bump native inputs changed while inspection was running")
    report.update(kind="bump_module_inspection", verified=True, complete_inventory=True,
                  environment=before_environment, compiled_inputs=after,
                  source_hashes=before_sources, source_sha256=digest(before_sources),
                  inspector_sha256=helper_sha, executable_sha256=executable_sha,
                  policy_sha256=policy_hash())
    report["evidence_sha256"] = digest(report)
    issues = _report_issues(report)
    if issues:
        raise ValueError("Bump native report is incomplete: " + "; ".join(issues))
    return report


def inspect_module_v2(root: Path, module: str, owned_modules: list[str]) -> dict:
    """Fresh complete-module inspection without recursively serializing upstream meanings."""
    from . import bump_inventory
    root = Path(root).resolve(strict=True)
    owned = sorted(set(owned_modules))
    if module not in owned or any(not re.fullmatch(r"[^\W\d]\w*(?:\.[^\W\d]\w*)*", name) for name in owned):
        raise ValueError("policy 2 inspection requires an exact owned module")
    before_environment, before_sources = _environment(root), _source_hashes(root)
    _run(root, ["lake", "--rehash", "build", "+" + module])
    helper = Path(__file__).with_name("bump_inventory.lean")
    helper_sha = _file_digest(helper)
    executable = _native_executable(root, helper, before_environment)
    executable_sha = _file_digest(executable)
    inventory = bump_inventory.read_native_module(root, module, environment=before_environment, executable=executable)
    before = formalize_cache.compiled_identity(inventory.get("compiled_modules"))
    report = bump_inventory.read_native_module(root, module, local_meanings=True,
        environment=before_environment, executable=executable)
    after = formalize_cache.compiled_identity(report.get("compiled_modules"))
    if (before != after or inventory["raw_declaration_count"] != report["raw_declaration_count"]
            or _environment(root) != before_environment or _source_hashes(root) != before_sources
            or _file_digest(helper) != helper_sha or _file_digest(executable) != executable_sha):
        raise ValueError("policy 2 native inputs changed during inspection")
    report.update(kind="bump_local_module_inspection", verified=True, complete_inventory=True,
        owned_modules=owned, environment=before_environment, compiled_inputs=after,
        source_hashes=before_sources, source_sha256=digest(before_sources),
        inspector_sha256=helper_sha, executable_sha256=executable_sha, inspection_policy=5)
    report["evidence_sha256"] = digest(report)
    local_report_records(report)
    return report


def local_report_records(report: dict) -> dict:
    """Validate policy-2 native data by typed identities, never display-name dictionaries."""
    if (not isinstance(report, dict) or report.get("schema_version") != 1
            or report.get("mode") != "local-meanings" or report.get("kind") != "bump_local_module_inspection"
            or report.get("verified") is not True or report.get("complete_inventory") is not True
            or report.get("inspection_policy") != 5 or report.get("declaration_inventory") != DECLARATION_INVENTORY
            or not isinstance(report.get("declarations"), list)
            or type(report.get("raw_declaration_count")) is not int
            or report["raw_declaration_count"] != len(report["declarations"])
            or report.get("evidence_sha256") != digest({k: v for k, v in report.items() if k != "evidence_sha256"})
            or not isinstance(report.get("compiled_inputs"), dict) or not report["compiled_inputs"]
            or not report.get("environment") or not isinstance(report.get("source_hashes"), dict)
            or report.get("source_sha256") != digest(report["source_hashes"])
            or not all(re.fullmatch(r"[0-9a-f]{64}", str(report.get(k, "")))
                       for k in ("inspector_sha256", "executable_sha256"))):
        raise ValueError("incomplete local module inspection")
    result = {}
    for row in report["declarations"]:
        name = _name(row["name_ast"])
        if (name in result or row.get("kind") not in _KINDS or type(row.get("direct_sorry")) is not bool
                or not isinstance(row.get("display_name"), str) or not isinstance(row.get("axioms"), list)):
            raise ValueError("invalid local declaration/trust inventory")
        meaning = row["meaning"]
        if meaning.get("name") != row["name_ast"] or meaning.get("kind") != row["kind"]:
            raise ValueError("local declaration meaning identity changed")
        _validate_expr(meaning.get("type"))
        if not isinstance(meaning.get("level_params"), list):
            raise ValueError("local declaration universe inventory missing")
        for level in meaning["level_params"]:
            _name(level)
        if row["kind"] in {"def", "opaque"}:
            _validate_expr(meaning.get("value"))
        for axiom in row["axioms"]:
            _name(axiom["reference"]["name_ast"])
            if axiom.get("kind") != "axiom" or type(axiom.get("unsafe")) is not bool:
                raise ValueError("invalid axiom closure evidence")
            _validate_expr(axiom.get("type"))
            if not isinstance(axiom.get("level_params"), list):
                raise ValueError("axiom universe inventory missing")
            for level in axiom["level_params"]:
                _name(level)
        result[name] = row
    return result


def _axiom_identity_v2(row: dict) -> str:
    # Module relocation is not a new axiom; a changed axiom type is.
    return digest({"name": row["reference"]["name_ast"], "type": row["type"],
        "level_params": row["level_params"], "unsafe": row["unsafe"]})


def _standard_axiom_v2(axiom: dict, report: dict) -> bool:
    name = _name(axiom["reference"]["name_ast"])
    module = axiom["reference"].get("module")
    if axiom["unsafe"] or module not in _STANDARD_ASSUMPTIONS.get(name, set()):
        return False
    try:
        path = report["compiled_modules"][report["imported_modules"].index(module)]
        actual = report["compiled_inputs"][path]
        return (Path(actual["path"]).resolve() == Path(path).resolve()
            and Path(path).resolve().is_relative_to(Path(report["environment"]["lean_sysroot"]).resolve())
            and bool(re.fullmatch(r"[0-9a-f]{64}", actual["sha256"])))
    except (KeyError, ValueError, TypeError, IndexError):
        return False


def compare_local_module_v2(original: dict, current: dict, *, groups: dict,
                            occurrences: dict) -> dict:
    """Check local structure, declared drift, and per-original trust; not equivalence."""
    old, new = local_report_records(original), local_report_records(current)
    issues, checked, declared, used_targets = [], [], [], set()
    if original["module"] != current["module"] or original["owned_modules"] != current["owned_modules"]:
        raise ValueError("local module ownership changed")
    relevant = {key: value for key, value in occurrences.items() if value["module"] == original["module"]}
    if {_name(row["name_ast"]) for row in relevant.values()} != set(old):
        raise ValueError("lazy original evidence differs from the immutable occurrence inventory")
    renames = {}
    for group in groups.values():
        if len(group["original_ids"]) == len(group["targets"]) == 1:
            before = occurrences[group["original_ids"][0]]["name_ast"]
            identity = _name(before)
            after = group["targets"][0]["native_name"]
            if identity in renames and renames[identity] != after:
                raise ValueError("ambiguous cross-module automatic name correspondence")
            renames[identity] = after
    for group_id, group in groups.items():
        ids = [key for key in group["original_ids"] if key in relevant]
        if not ids:
            continue
        if len(ids) != len(group["original_ids"]):
            raise ValueError("local correspondence crosses unchecked module contexts")
        original_rows = [old[_name(relevant[key]["name_ast"])] for key in ids]
        allowed = set.intersection(*({ _axiom_identity_v2(a) for a in row["axioms"] } for row in original_rows))
        current_rows = []
        for target in group["targets"]:
            identity = _name(target["native_name"])
            if target["module"] != current["module"] or identity not in new:
                issues.append("mapped target is missing from its native module: " + target["declaration"])
                continue
            row = new[identity]
            used_targets.add(identity)
            current_rows.append(row)
            if row["display_name"] != target["declaration"]:
                issues.append("mapped target display reference is not its native identity")
            if { _axiom_identity_v2(a) for a in row["axioms"] } - allowed:
                issues.append("trusted assumptions expanded for correspondence " + group_id)
            if row["direct_sorry"] and not all(before["direct_sorry"] for before in original_rows):
                issues.append("new direct proof hole in correspondence " + group_id)
            if row["kind"] == "axiom" and not all(before["kind"] == "axiom" for before in original_rows):
                issues.append("new axiom replacing original declaration")
            if group["mode"] == "identity":
                expected = _rename_meaning(original_rows[0]["meaning"], renames)
                if row["meaning"] != expected:
                    issues.append("undeclared local type/value/metadata change: " + target["declaration"])
            elif digest(row["meaning"]) != target["expected_meaning_sha256"]:
                issues.append("declared target meaning changed: " + target["declaration"])
        if group["mode"] == "declared":
            declared.append(group_id)
        if len(current_rows) == len(group["targets"]):
            checked.extend(ids)
    if set(checked) != set(relevant):
        issues.append("local comparison does not cover every original occurrence")
    old_standard = {_axiom_identity_v2(a) for row in old.values() for a in row["axioms"]
                    if _standard_axiom_v2(a, original)}
    helpers = []
    for name in set(new) - used_targets:
        row = new[name]
        if (row["kind"] == "axiom" or row["direct_sorry"]
                or any(_axiom_identity_v2(a) not in old_standard or not _standard_axiom_v2(a, current)
                       for a in row["axioms"])):
            issues.append("new helper introduces nonbaseline trust: " + row["display_name"])
        helpers.append(row["display_name"])
    value = {"schema_version": 2, "policy": "bump-local-correspondence-v2", "passed": not issues,
        "issues": issues, "original_evidence": original["evidence_sha256"],
        "current_evidence": current["evidence_sha256"], "checked_occurrences": sorted(checked),
        "declared_correspondences": sorted(declared), "new_helpers": sorted(helpers),
        "semantic_equivalence_proved": False,
        "qualification": "Local structure and per-declaration trust checked; semantic correspondence requires independent review."}
    return {**value, "evidence_sha256": digest(value)}


def _name(value: object) -> tuple:
    """Lossless typed Name identity, independent of Lean's display printer.

    Display labels may contain escaped dots/operators, macro scopes, numeric
    components or anonymous names. Flattening them loses constructor identity.
    """
    if not isinstance(value, list) or not value:
        raise ValueError("invalid structural Lean name")
    if value == ["anonymous"]:
        return ("anonymous",)
    if len(value) == 3 and value[0] in {"str", "num"}:
        prefix = _name(value[1])
        part = value[2]
        if ((value[0] == "str" and isinstance(part, str))
                or (value[0] == "num" and type(part) is int and part >= 0)):
            return (value[0], prefix, part)
    raise ValueError("invalid structural Lean name")


def _name_index(meanings: dict) -> dict[tuple, str]:
    """Bind every native label to exactly one structural identity and back.

    Labels are opaque references within one native report, not semantic names.
    Full original/current meaning comparison still includes the exact Name tree.
    """
    result = {}
    for label, row in meanings.items():
        if not isinstance(label, str) or not label:
            raise ValueError("invalid native declaration label")
        identity = _name(row["meaning"]["name"])
        if identity in result:
            raise ValueError("duplicate structural Lean name")
        result[identity] = label
    return result


def _refs(value: object) -> set[tuple]:
    """References in structural expressions; binder/level names are not constants."""
    result = set()
    if isinstance(value, list):
        if value and value[0] in ("const", "proj"):
            result.add(_name(value[1]))
        for item in value:
            result.update(_refs(item))
    elif isinstance(value, dict):
        for item in value.values():
            result.update(_refs(item))
    return result


def _validate_level(value: object) -> None:
    if not isinstance(value, list) or not value:
        raise ValueError("unsupported universe expression")
    if value == ["zero"]:
        return
    if value[0] == "param" and len(value) == 2:
        _name(value[1])
        return
    if ((value[0] == "succ" and len(value) == 2)
            or (value[0] in ("max", "imax") and len(value) == 3)):
        for child in value[1:]:
            _validate_level(child)
        return
    raise ValueError("unsupported/unresolved universe expression")


def _validate_expr(value: object) -> None:
    if not isinstance(value, list) or not value:
        raise ValueError("missing structural expression")
    tag = value[0]
    if tag in ("bvar", "natVal") and len(value) == 2 and type(value[1]) is int and value[1] >= 0:
        return
    if tag == "strVal" and len(value) == 2 and isinstance(value[1], str):
        return
    if tag == "sort" and len(value) == 2:
        _validate_level(value[1])
        return
    if tag == "const" and len(value) == 3 and isinstance(value[2], list):
        _name(value[1])
        for level in value[2]:
            _validate_level(level)
        return
    if tag == "app" and len(value) == 3:
        _validate_expr(value[1])
        _validate_expr(value[2])
        return
    if tag in ("lam", "forallE") and len(value) == 5 and value[4] in ("default", "implicit", "strictImplicit", "instImplicit"):
        _name(value[1])
        _validate_expr(value[2])
        _validate_expr(value[3])
        return
    if tag == "letE" and len(value) == 6 and type(value[5]) is bool:
        _name(value[1])
        for child in value[2:5]:
            _validate_expr(child)
        return
    if tag == "proj" and len(value) == 4 and type(value[2]) is int and value[2] >= 0:
        _name(value[1])
        _validate_expr(value[3])
        return
    raise ValueError("unsupported/unresolved structural expression")


def _meaning_refs(meaning: dict) -> set[tuple]:
    refs = _refs(meaning)
    for key in ("all", "ctors"):
        refs.update(_name(n) for n in meaning.get(key, []))
    if "induct" in meaning:
        refs.add(_name(meaning["induct"]))
    for rule in meaning.get("rules", []):
        refs.add(_name(rule["ctor"]))
    return refs


def _report_issues(report: object) -> list[str]:
    try:
        if not isinstance(report, dict):
            return ["missing inspection report"]
        if (report.get("schema_version") != SCHEMA_VERSION or report.get("kind") != "bump_module_inspection"
                or report.get("verified") is not True or report.get("complete_inventory") is not True
                or report.get("issues") != []):
            return ["missing, failed or unsupported native inspection report"]
        if report.get("evidence_sha256") != digest({k: v for k, v in report.items() if k != "evidence_sha256"}):
            return ["inspection evidence digest mismatch"]
        if (not isinstance(report.get("environment"), dict) or not report["environment"].get("lean_version")
                or not isinstance(report.get("compiled_inputs"), dict) or not report["compiled_inputs"]
                or not isinstance(report.get("source_hashes"), dict)
                or report.get("source_sha256") != digest(report.get("source_hashes"))
                or any(not re.fullmatch(r"[0-9a-f]{64}", str(report.get(k, "")))
                       for k in ("inspector_sha256", "executable_sha256", "policy_sha256"))):
            return ["missing native environment/compiled-input evidence"]
        module, owned = report.get("module"), report.get("owned_modules")
        if not isinstance(module, str) or not isinstance(owned, list) or module not in owned:
            return ["missing native module ownership"]
        declarations, meanings = report.get("declarations"), report.get("meanings")
        if not isinstance(declarations, dict) or not isinstance(meanings, dict):
            return ["missing complete declaration/meaning inventory"]
        # Import merging can retain one owner while replacing a theorem body
        # from another module. Only the exact raw module inventory establishes
        # which declaration occurrences this receipt actually audits.
        if (report.get("declaration_inventory") != DECLARATION_INVENTORY
                or type(report.get("raw_declaration_count")) is not int
                or report["raw_declaration_count"] != len(declarations)):
            return ["missing or incomplete raw module declaration inventory"]
        names = _name_index(meanings)
        for name, row in meanings.items():
            meaning = row["meaning"]
            if (names.get(_name(meaning["name"])) != name or meaning["kind"] not in _KINDS
                    or not isinstance(meaning["type"], list) or not isinstance(meaning["level_params"], list)
                    or not isinstance(row["module"], str) or not isinstance(row["dependencies"], list)):
                return ["malformed structural meaning: " + str(name)]
            if any(not isinstance(dep, str) or dep not in meanings for dep in row["dependencies"]):
                return ["missing transitive semantic evidence: " + str(name)]
            dependencies = set(row["dependencies"])
            required = {"def": {"value", "hints", "safety", "all"},
                        "opaque": {"value", "unsafe", "all"}, "axiom": {"unsafe"},
                        "inductive": {"all", "ctors", "num_params", "num_indices", "num_nested", "recursive", "unsafe", "reflexive"},
                        "constructor": {"induct", "index", "num_params", "num_fields", "unsafe"},
                        "recursor": {"all", "num_params", "num_indices", "num_motives", "num_minors", "rules", "k", "unsafe"},
                        "quot": {"quot_kind"}}.get(meaning["kind"], set())
            if (not required <= meaning.keys()
                    or any(names.get(ref) not in dependencies for ref in _meaning_refs(meaning))):
                return ["incomplete structural meaning: " + str(name)]
            _validate_expr(meaning["type"])
            for parameter in meaning["level_params"]:
                _name(parameter)
            if "value" in meaning:
                _validate_expr(meaning["value"])
            for rule in meaning.get("rules", []):
                _validate_expr(rule["rhs"])
        for name, row in declarations.items():
            if (row["name"] != name or row["module"] != module or name not in meanings
                    or row["kind"] != meanings[name]["meaning"]["kind"]
                    or type(row["direct_sorry"]) is not bool or not isinstance(row["axioms"], list)
                    or any(not isinstance(n, str) or n not in meanings
                           or meanings[n]["meaning"]["kind"] != "axiom" for n in row["axioms"])):
                return ["incomplete declaration or axiom audit: " + str(name)]
        return []
    except (KeyError, TypeError, ValueError, IndexError, RecursionError):
        return ["malformed/unsupported structural inspection evidence"]


def _rename_name(value: object, renames: dict[tuple, object]) -> object:
    return renames.get(_name(value), value)


def _rename_expr(value: object, renames: dict[tuple, object]) -> object:
    if isinstance(value, list):
        result = [_rename_expr(item, renames) for item in value]
        if value and value[0] in ("const", "proj"):
            result[1] = _rename_name(value[1], renames)
        return result
    return value


def _rename_meaning(meaning: dict, renames: dict[tuple, object]) -> dict:
    result = dict(meaning)
    result["name"] = _rename_name(meaning["name"], renames)
    for key in ("type", "value"):
        if key in result:
            result[key] = _rename_expr(result[key], renames)
    for key in ("all", "ctors"):
        if key in result:
            result[key] = [_rename_name(n, renames) for n in result[key]]
    if "induct" in result:
        result["induct"] = _rename_name(result["induct"], renames)
    if "rules" in result:
        result["rules"] = [{**r, "ctor": _rename_name(r["ctor"], renames),
                            "rhs": _rename_expr(r["rhs"], renames)} for r in result["rules"]]
    return result


def _native_assumption(name: str, identity: object) -> bool:
    # Keep the prior conservative label check, but never rely on labels to
    # recognize trust-sensitive identities now that labels are opaque references.
    if name in {"Lean.ofReduceBool", "Lean.ofReduceNat", "Lean.trustCompiler"} or _NATIVE.search(name):
        return True
    parts = []
    key = _name(identity)
    while key[0] != "anonymous":
        parts.append((key[0], key[2]))
        key = key[1]
    parts.reverse()
    if parts in [[("str", "Lean"), ("str", suffix)]
                 for suffix in ("ofReduceBool", "ofReduceNat", "trustCompiler")]:
        return True
    return bool(parts and parts[-1][0] == "str" and re.fullmatch(r"ax(?:_[0-9]+)+", parts[-1][1])
                and ("str", "_native") in parts[:-1])


def _standard_assumption(report: dict, name: str) -> bool:
    """Recognize only the native toolchain's standard axioms, not a namesake.

    This reads sealed evidence only: the parallel native import/path arrays and
    canonical compiled receipt bind the axiom's origin to the pinned sysroot.
    A project-defined `propext`, including a shadowed Init module, grants no
    standard-assumption allowance to new helpers.
    """
    row = report["meanings"][name]
    meaning = row["meaning"]
    origins = _STANDARD_ASSUMPTIONS.get(_name(meaning["name"]), set())
    module = row.get("module")
    if (meaning.get("kind") != "axiom" or meaning.get("unsafe") is not False
            or module not in origins or module in report.get("owned_modules", [])):
        return False
    imported, compiled = report.get("imported_modules"), report.get("compiled_modules")
    sysroot = report.get("environment", {}).get("lean_sysroot")
    if (not isinstance(imported, list) or not isinstance(compiled, list)
            or len(imported) != len(compiled) or imported.count(module) != 1
            or not isinstance(sysroot, str) or not Path(sysroot).is_absolute()
            or ".." in Path(sysroot).parts):
        return False
    filename = compiled[imported.index(module)]
    if not isinstance(filename, str):
        return False
    receipt = report.get("compiled_inputs", {}).get(filename)
    expected = str(Path(sysroot) / "lib" / "lean" / (module.replace(".", "/") + ".olean"))
    return (isinstance(receipt, dict) and receipt.get("path") == expected
            and isinstance(receipt.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"]) is not None)


def compare_module(original: dict, current: dict, *, correspondences: dict[str, str] | None = None) -> dict:
    """Compare complete contexts; explicit symbol maps must be globally injective.

Theorem proof terms may change. Definitions, type/universe/inductive metadata,
every recursively referenced external meaning, and per-declaration assumptions
must retain identity modulo the supplied bijection. No LLM/prettyprint fallback.
"""
    issues = ["original: " + x for x in _report_issues(original)]
    issues += ["current: " + x for x in _report_issues(current)]
    mapping = {} if correspondences is None else correspondences
    checked, helpers = [], []
    if not issues:
        old, new = original["meanings"], current["meanings"]
        if original["module"] != current["module"] or original["owned_modules"] != current["owned_modules"]:
            issues.append("module ownership/scope changed")
        if (not isinstance(mapping, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                               or k not in old or v not in new for k, v in mapping.items())):
            issues.append("invalid or unsupported symbol correspondence")
        elif len({mapping.get(n, n) for n in old}) != len(old):
            issues.append("symbol correspondence is not bijective on the original closure")
        else:
            renames = {_name(old[name]["meaning"]["name"]): new[target]["meaning"]["name"]
                       for name, target in mapping.items()}
            old_decls, new_decls = original["declarations"], current["declarations"]
            required = set(old_decls)
            for name, row in old_decls.items():
                now = new_decls.get(mapping.get(name, name), {})
                # Dropping an assumption is allowed. Its unused old closure is
                # not a semantic obligation unless another declaration uses it.
                required.update(a for a in row["axioms"]
                                if mapping.get(a, a) in now.get("axioms", []))
            pending = list(required)
            while pending:
                name = pending.pop()
                for dep in old[name]["dependencies"]:
                    if dep not in required:
                        required.add(dep)
                        pending.append(dep)
            # A shared closure is traversed once; equality of every node establishes
            # recursive correspondence even for inductive/mutually recursive cycles.
            for name in sorted(required):
                row = old[name]
                target = mapping.get(name, name)
                if target not in new:
                    issues.append("semantic declaration removed or unmapped: " + name)
                    continue
                if _rename_meaning(row["meaning"], renames) != new[target]["meaning"]:
                    issues.append("semantic meaning changed: " + name + " -> " + target)
                if {mapping.get(n, n) for n in row["dependencies"]} != set(new[target]["dependencies"]):
                    issues.append("semantic dependency closure changed: " + name)
            for name, row in old_decls.items():
                target = mapping.get(name, name)
                if target not in new_decls:
                    issues.append("original declaration removed: " + name)
                    continue
                now = new_decls[target]
                expanded = set(now["axioms"]) - {mapping.get(n, n) for n in row["axioms"]}
                if expanded:
                    issues.append("trusted assumptions expanded for " + name + ": " + ", ".join(sorted(expanded)))
                if now["direct_sorry"] and not row["direct_sorry"]:
                    issues.append("new direct proof hole: " + name)
                if now["kind"] == "axiom" and row["kind"] != "axiom":
                    issues.append("new axiom replacing declaration: " + name)
                checked.append({"original": name, "current": target})
            baseline_axioms = {mapping.get(a, a) for row in old_decls.values() for a in row["axioms"]}
            helpers = sorted(set(new_decls) - {mapping.get(n, n) for n in old_decls})
            for name in helpers:
                row = new_decls[name]
                # Existing custom assumptions belong to their own original
                # declaration, not a module-wide allowance for new helpers.
                # Standard kernel assumptions must also occur in the baseline;
                # this does not expand the original trust allowance.
                bad = {a for a in row["axioms"] if a == "sorryAx"
                       or _name(new[a]["meaning"]["name"]) == ("str", ("anonymous",), "sorryAx")
                       or _native_assumption(a, new[a]["meaning"]["name"])
                       or not _standard_assumption(current, a)
                       or a not in baseline_axioms}
                if row["kind"] == "axiom" or row["direct_sorry"] or bad:
                    issues.append("new helper introduces a hole/axiom/trusted assumption: " + name)
    result = {"schema_version": SCHEMA_VERSION, "passed": not issues, "issues": sorted(set(issues)),
              "original_evidence": original.get("evidence_sha256") if isinstance(original, dict) else None,
              "current_evidence": current.get("evidence_sha256") if isinstance(current, dict) else None,
              "correspondences": mapping, "checked_declarations": checked, "new_helpers": helpers,
              "policy": "bump-structural-closure-v1"}
    result["evidence_sha256"] = digest(result)
    return result
