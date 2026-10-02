"""Controller-owned Bump workspaces and version transitions.

This module never chooses a release, deletes a cache, or edits proof sources.
The original checkout is left untouched.  Every build uses the worktree's own
Lake directory and cache; version resolution is an explicit, separately checked
operation.  Unsupported project configurations fail closed.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib
from typing import Any

from . import bump_jobs
from .config import find_unity_dir


class ProjectError(ValueError):
    """The project cannot be migrated under the supported preservation policy."""


_EXCLUDED = {".git", ".unity", ".lake", ".worktrees", ".bump-runtime"}
_CONFIG = ("lean-toolchain", "lakefile.toml", "lakefile.lean", "lake-manifest.json")
_VERSION = re.compile(r"(?:leanprover/lean4:)?(?:v\d+\.\d+\.\d+(?:-rc\d+)?|nightly-\d{4}-\d{2}-\d{2})\Z")
_PIN = re.compile(r"(?:[0-9a-fA-F]{40}|v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?)\Z")
_DEPENDENCY_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_'-]*(?:\.[A-Za-z_][A-Za-z0-9_'-]*)*\Z")


def _run(root: Path, argv: list[str], *, timeout: int = 1800, output_stream=None) -> subprocess.CompletedProcess:
    """No shell interpretation; caches are isolated even for direct Lake calls."""
    root = Path(root).resolve()
    env = os.environ.copy()
    unity_dir = find_unity_dir(root)
    project_root = unity_dir.resolve().parent if unity_dir is not None else root
    env["LAKE_CACHE_DIR"] = str(project_root / ".unity" / "bump-cache" / "lake")
    env["XDG_CACHE_HOME"] = str(project_root / ".unity" / "bump-cache" / "xdg")
    if argv and argv[0] == "lake":
        for key in ("LEAN_PATH", "LEAN_SRC_PATH", "LEAN_SYSROOT", "LAKE_HOME", "LAKE_PACKAGES_DIR"):
            env.pop(key, None)
        toolchain = root / "lean-toolchain"
        if toolchain.is_file():
            env["ELAN_TOOLCHAIN"] = toolchain.read_text().strip()
    # Use the same cancellation registry as copied candidate integration. A
    # dependency checkout is a command cwd, never a new Unity project root.
    options = {"output_stream": output_stream} if output_stream is not None else {}
    return bump_jobs.run(project_root, argv, cwd=root, env=env, timeout=timeout, **options)


def _git(root: Path, *args: str) -> str:
    result = _run(root, ["git", *args], timeout=60)
    if result.returncode:
        raise ProjectError(f"Git {' '.join(args[:2])} failed: {result.stderr[-4000:]}")
    return result.stdout.strip()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _hash(path: Path) -> str:
    if path.is_symlink():
        return _digest({"symlink": os.readlink(path)})
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _excluded(path: str) -> bool:
    return any(part in _EXCLUDED for part in Path(path).parts)


def source_files(root: Path) -> dict[str, str]:
    """Hash all tracked bytes plus every project Lean file, including ignored ones."""
    root = Path(root).resolve()
    result = _run(root, ["git", "ls-files", "-z"], timeout=60)
    if result.returncode:
        raise ProjectError("Bump requires a Git project")
    names = {name for name in result.stdout.split("\0") if name and not _excluded(name)}
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in _EXCLUDED)
        for filename in files:
            if filename.endswith(".lean") or (Path(directory) == root and filename in _CONFIG):
                names.add((Path(directory) / filename).relative_to(root).as_posix())
    hashes: dict[str, str] = {}
    for name in sorted(names):
        path = root / name
        if path.is_file() or path.is_symlink():
            hashes[name] = _hash(path)
        else:
            raise ProjectError(f"Tracked path is missing or unsupported: {name}")
    return hashes


def snapshot(root: Path) -> str:
    return _digest(source_files(Path(root)))


def _strip_comments(text: str) -> str:
    """Preserve line positions while ignoring nested Lean comments and strings."""
    output: list[str] = []
    index, depth = 0, 0
    in_string = False
    while index < len(text):
        pair = text[index:index + 2]
        char = text[index]
        if depth:
            if pair == "/-":
                depth += 1
                output.extend("  ")
                index += 2
                continue
            if pair == "-/":
                depth -= 1
                output.extend("  ")
                index += 2
                continue
            output.append("\n" if char == "\n" else " ")
        elif in_string:
            if char == "\\" and index + 1 < len(text):
                output.extend("  ")
                index += 2
                continue
            if char == '"':
                in_string = False
            output.append("\n" if char == "\n" else " ")
        elif pair == "/-":
            depth = 1
            output.extend("  ")
            index += 2
            continue
        elif pair == "--":
            end = text.find("\n", index)
            if end < 0:
                end = len(text)
            output.extend(" " * (end - index))
            index = end
            continue
        elif char == '"':
            in_string = True
            output.append(" ")
        else:
            output.append(char)
        index += 1
    if depth or in_string:
        raise ProjectError("Unterminated Lean comment or string in project source")
    return "".join(output)


def inventory_modules(root: Path) -> dict[str, dict[str, Any]]:
    """All local Lean modules, not merely those covered by the default build."""
    root = Path(root).resolve()
    modules: dict[str, dict[str, Any]] = {}
    for name in source_files(root):
        if not name.endswith(".lean") or name == "lakefile.lean":
            continue
        path = root / name
        if path.is_symlink():
            raise ProjectError(f"Symlinked Lean source is unsupported: {name}")
        module = name[:-5].replace("/", ".")
        if not _NAME.fullmatch(module):
            raise ProjectError(f"Unsupported Lean module path: {name}")
        source = _strip_comments(path.read_text(encoding="utf-8"))
        imports: list[str] = []
        for line in source.splitlines():
            match = re.match(r"^\s*(?:(?:public|private|meta)\s+)*import\s+(.+?)\s*$", line)
            if match:
                for imported in match.group(1).split():
                    if not _NAME.fullmatch(imported):
                        raise ProjectError(f"Unsupported import in {name}: {imported}")
                    if imported not in imports:
                        imports.append(imported)
        modules[module] = {"path": name, "imports": imports}
    if not modules:
        raise ProjectError("No project Lean modules found")
    return modules


def _manifest(root: Path) -> dict[str, Any]:
    path = root / "lake-manifest.json"
    if not path.is_file() or path.is_symlink():
        raise ProjectError("A checked-in, regular lake-manifest.json is required")
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise ProjectError("Invalid Lake manifest") from exc
    if not isinstance(data, dict) or not isinstance(data.get("packages"), list):
        raise ProjectError("Unsupported Lake manifest shape")
    if data.get("packagesDir", ".lake/packages") != ".lake/packages":
        raise ProjectError("Only project-local .lake/packages dependency storage is supported")
    names: set[str] = set()
    for package in data["packages"]:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            raise ProjectError("Invalid dependency entry")
        name = package["name"]
        if name in names:
            raise ProjectError(f"Duplicate dependency name: {name}")
        names.add(name)
        if package.get("type") != "git":
            raise ProjectError(f"Dependency {name} is not an isolated Git dependency")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", str(package.get("rev", ""))):
            raise ProjectError(f"Dependency {name} lacks an immutable commit pin")
        if package.get("subDir") and (Path(package["subDir"]).is_absolute() or ".." in Path(package["subDir"]).parts):
            raise ProjectError(f"Dependency {name} escapes its package checkout")
    return data


def _config_hashes(root: Path) -> dict[str, str]:
    return {name: _hash(root / name) for name in _CONFIG if (root / name).exists()}


config_hashes = _config_hashes


def _edit_toml(source: str, pins: dict[str, str]) -> str:
    parsed = tomllib.loads(source)
    requirements = parsed.get("require", [])
    if not isinstance(requirements, list):
        raise ProjectError("Only TOML [[require]] dependencies can be pinned")
    names = [entry.get("name") for entry in requirements]
    if len(set(names)) != len(names):
        raise ProjectError("Duplicate TOML dependency names")
    for name in pins:
        if name not in names:
            raise ProjectError(f"Requested dependency is not a direct TOML requirement: {name}")
    blocks = list(re.finditer(r"(?m)^\s*\[\[require\]\]\s*(?:#.*)?$", source))
    replacements: list[tuple[int, int, str]] = []
    changed: set[str] = set()
    for block in blocks:
        end_match = re.search(r"(?m)^\s*\[", source[block.end():])
        end = block.end() + end_match.start() if end_match else len(source)
        content = source[block.end():end]
        entry = tomllib.loads("[[require]]" + content)["require"][0]
        name = entry.get("name")
        if name not in pins:
            continue
        if "git" not in entry or "path" in entry:
            raise ProjectError(f"Dependency {name} is not a declarative Git requirement")
        rev = re.search(r"(?m)^(\s*rev\s*=\s*)(?:\"[^\"\n]*\"|'[^'\n]*')(\s*(?:#.*)?)$", content)
        if rev:
            content = content[:rev.start()] + rev.group(1) + json.dumps(pins[name]) + rev.group(2) + content[rev.end():]
        elif "rev" not in entry:
            content = content.rstrip() + "\nrev = " + json.dumps(pins[name]) + "\n\n"
        else:
            raise ProjectError(f"Unsupported revision syntax for {name}")
        replacements.append((block.end(), end, content))
        changed.add(name)
    if changed != set(pins):
        raise ProjectError("Unsupported TOML dependency table layout")
    for start, end, content in reversed(replacements):
        source = source[:start] + content + source[end:]
    final = tomllib.loads(source)
    expected = json.loads(json.dumps(parsed))
    for entry in expected.get("require", []):
        if entry["name"] in pins:
            entry["rev"] = pins[entry["name"]]
    if final != expected:
        raise ProjectError("TOML transition changed unrelated configuration")
    return source


def _edit_lean(source: str, pins: dict[str, str]) -> str:
    # Deliberately narrow: executable Lake configuration is not rewritten by an
    # LLM or guessed from a partial parse. Unsupported forms require a new adapter.
    for name, revision in pins.items():
        if not _NAME.fullmatch(name) or "." in name:
            raise ProjectError(f"Unsupported Lean dependency name: {name}")
        # A literal URL/revision may continue on the next indented line. Do not
        # match computed expressions, escaped/multiline strings, nested commands
        # or comments between tokens. In particular, an unpinned match must not
        # truncate a following `@ revision`/`with ...` continuation.
        continuation = r"[ \t]*(?:\r?\n[ \t]+)?"
        pattern = re.compile(r"(?m)^require[ \t]+" + re.escape(name) +
                             r"[ \t]+from[ \t]+git" + continuation +
                             r'(?P<url>"[^"\\\r\n]+")' +
                             r"(?:" + continuation + r"@" + continuation +
                             r'(?P<revision>"[^"\\\r\n]*"))?' +
                             r"[ \t]*(?:--[^\r\n]*)?(?=\r?$)")
        matches = list(pattern.finditer(source))
        uncommented = _strip_comments(source)
        real_requirements = len(re.findall(r"(?m)^\s*require\s+" + re.escape(name) + r"\b", uncommented))
        if len(matches) != 1 or real_requirements != 1:
            raise ProjectError(f"Unsupported or ambiguous Lean revision syntax for {name}")
        match = matches[0]
        # Do not accidentally replace a require statement inside a block comment.
        if not re.search(r"\brequire\b", uncommented[match.start():match.end()]):
            raise ProjectError(f"Commented Lean requirement cannot be edited: {name}")
        following = next((line for line in uncommented[match.end():].splitlines() if line.strip()), "")
        if following and (following[0].isspace() or re.match(r"(?:@(?!\[)|\+|\(|with\b|where\b)", following)):
            raise ProjectError(f"Unsupported Lean requirement continuation for {name}")
        if match.group("revision") is None:
            at = match.end("url")
            source = source[:at] + " @ " + json.dumps(revision) + source[at:]
        else:
            source = source[:match.start("revision")] + json.dumps(revision) + source[match.end("revision"):]
    return source


def _partition_dependency_pins(manifest: dict[str, Any], pins: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Direct requirements drive Lake; inherited pins constrain its resolution."""
    packages = {entry["name"]: entry for entry in manifest["packages"]}
    if set(pins) - set(packages):
        raise ProjectError("Every requested dependency must already exist in the pinned manifest")
    direct, inherited = {}, {}
    for name, revision in pins.items():
        flag = packages[name].get("inherited", False)
        if type(flag) is not bool:
            raise ProjectError(f"Dependency has an ambiguous inherited flag: {name}")
        if flag and not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
            raise ProjectError(f"Inherited dependency requires an exact full commit hash: {name}")
        (inherited if flag else direct)[name] = revision
    if inherited and not direct:
        raise ProjectError("Inherited dependency expectations require at least one explicit direct dependency pin")
    return direct, inherited


def _ensure_excludes(root: Path) -> None:
    path = Path(_git(root, "rev-parse", "--git-path", "info/exclude"))
    if not path.is_absolute():
        path = root / path
    old = path.read_text() if path.exists() else ""
    additions = [name for name in (".unity", ".lake", ".worktrees", ".bump-runtime") if name not in old.splitlines()]
    if additions:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(old + ("\n" if old and not old.endswith("\n") else "") + "\n".join(additions) + "\n")


def _safe_workspace(path: Path) -> list[str]:
    errors: list[str] = []
    for name in (".unity", ".bump-runtime", ".lake", ".lake/packages", ".lake/build"):
        if (path / name).is_symlink():
            errors.append(f"Workspace cache/runtime path must not be a symlink: {name}")
    return errors


def prepare(root: Path, version: str, dependency_pins: dict[str, str], *, run_id: str,
            project_scope: str = "build") -> dict[str, Any]:
    """Create fresh retained worktrees and apply the exact requested config delta.

    No native build, dependency update, model call, or original-checkout edit is
    performed. The caller must build/inspect ``original`` before accepting its
    baseline, resolve dependencies, and seal that receipt before dispatch.
    """
    root = Path(root).resolve()
    if project_scope not in {"build", "all"}:
        raise ProjectError("Bump project scope must be build or all")
    if not _RUN.fullmatch(run_id):
        raise ProjectError("Invalid Bump run identifier")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ProjectError("An exact Lean release or dated nightly is required; no latest or branch")
    version = version if ":" in version else "leanprover/lean4:" + version
    if any(not isinstance(name, str) or not _DEPENDENCY_NAME.fullmatch(name)
           or not isinstance(pin, str) or not _PIN.fullmatch(pin)
           for name, pin in dependency_pins.items()):
        raise ProjectError("Dependency names must be safe package identifiers and revisions full commit hashes or exact version tags")
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ProjectError("Bump must start at the Git project root")
    if _safe_workspace(root):
        raise ProjectError("; ".join(_safe_workspace(root)))
    _ensure_excludes(root)
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ProjectError("Original project must be clean; commit or preserve unrelated changes first")
    manifest = _manifest(root)
    direct_pins, _ = _partition_dependency_pins(manifest, dependency_pins)
    for filename in ("lean-toolchain", "lake-manifest.json"):
        if filename not in _git(root, "ls-files", "--", filename).splitlines():
            raise ProjectError(f"Baseline configuration must be tracked: {filename}")
    configurations = [name for name in ("lakefile.toml", "lakefile.lean") if (root / name).is_file()]
    if len(configurations) != 1:
        raise ProjectError("Exactly one regular lakefile.toml or lakefile.lean is required")
    for name in _CONFIG:
        if (root / name).is_symlink():
            raise ProjectError(f"Symlinked configuration is unsupported: {name}")
    config_name = configurations[0]
    config_text = (root / config_name).read_text()
    updated = (_edit_toml if config_name.endswith("toml") else _edit_lean)(config_text, direct_pins)
    # Build scope does not parse optional notes/scratch programs before native
    # Lake ownership establishes the actual selected source boundary.
    modules = inventory_modules(root) if project_scope == "all" else {}
    head = _git(root, "rev-parse", "HEAD")
    source = source_files(root)
    run_dir = root / ".unity" / "bump" / run_id
    if run_dir.exists() or run_dir.is_symlink():
        raise ProjectError("Existing Bump work must be resumed explicitly, never overwritten")
    run_dir.mkdir(parents=True)
    original, target = run_dir / "original", run_dir / "target"
    _git(root, "worktree", "add", "--detach", str(original), head)
    _git(root, "worktree", "add", "--detach", str(target), head)
    for tree in (original, target):
        # Do not inherit the source checkout's cache via upward Unity discovery.
        # Each compiler environment owns its own registry and cache directory.
        (tree / ".unity").mkdir()
    if source_files(original) != source:
        raise ProjectError("Original worktree omitted project files (possibly ignored Lean sources)")
    (target / "lean-toolchain").write_text(version + "\n")
    (target / config_name).write_text(updated)
    baseline = {
        "version": 1, "run_id": run_id, "root": str(root),
        "original": str(original), "target": str(target), "head": head,
        "original_path": str(original), "target_path": str(target), "source_commit": head,
        "source_hash": _digest(source), "source_files": source,
        "original_config": _config_hashes(original),
        "target_config": _config_hashes(target), "target_version": version,
        "dependency_pins": dict(sorted(dependency_pins.items())),
        "target_sealed": False,
        "original_manifest": manifest, "modules": modules,
        "scope": {"version": 1, "mode": project_scope, "pending": True,
                  "default_build_required": True},
    }
    baseline["identity"] = _digest(baseline)
    return baseline


def resolve_paths(root: Path, baseline: dict[str, Any]) -> tuple[Path, Path]:
    root = Path(root).resolve()
    run_id = baseline.get("run_id", "")
    if not isinstance(run_id, str) or not _RUN.fullmatch(run_id) or baseline.get("root") != str(root):
        raise ProjectError("Baseline does not belong to this project")
    expected = root / ".unity" / "bump" / run_id
    for role in ("original", "target"):
        path = expected / role
        if baseline.get(role) != str(path) or baseline.get(role + "_path") != str(path) or path.is_symlink() or path.resolve() != path:
            raise ProjectError(f"Invalid Bump {role} workspace identity")
        if not path.is_dir():
            raise ProjectError(f"Bump {role} workspace is missing")
    if baseline.get("identity") != _digest({key: value for key, value in baseline.items() if key != "identity"}):
        raise ProjectError("Bump baseline identity does not match its contents")
    if baseline.get("source_commit") != baseline.get("head"):
        raise ProjectError("Conflicting Bump source commit identities")
    return expected / "original", expected / "target"


def validate_original(root: Path, baseline: dict[str, Any]) -> list[str]:
    try:
        original, _ = resolve_paths(root, baseline)
        errors = _safe_workspace(original)
        for name, path in (("source checkout", Path(root)), ("original worktree", original)):
            if _git(path, "rev-parse", "HEAD") != baseline["head"]:
                errors.append(f"{name} Git identity changed")
            if source_files(path) != baseline["source_files"]:
                errors.append(f"{name} bytes changed")
        return errors
    except (ProjectError, OSError, ValueError) as exc:
        return [str(exc)]


def _pin_errors(manifest: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    before = {item["name"]: item for item in baseline["original_manifest"]["packages"]}
    after = {item["name"]: item for item in manifest["packages"]}
    pins = baseline["dependency_pins"]
    errors: list[str] = []
    auxiliary = baseline.get("auxiliary_dependencies", {})
    if not isinstance(auxiliary, dict) or set(auxiliary) - {"LeanArchitect"} or set(auxiliary) & set(before):
        return ["Invalid optional dependency identity"]
    if set(before) | set(auxiliary) != set(after):
        errors.append("Dependency set changed; adding or removing dependencies requires explicit support")
    for name, expected in auxiliary.items():
        if (not isinstance(expected, dict) or expected.get("name") != name
                or expected.get("type") != "git"
                or expected.get("url") != "https://github.com/hanwenzhu/LeanArchitect.git"
                or not re.fullmatch(r"[0-9a-f]{40}", expected.get("rev", ""))
                or expected.get("inputRev") != expected.get("rev")
                or expected.get("inherited", False) is not False or after.get(name) != expected):
            errors.append("Optional dependency identity changed: " + name)
    for name in before.keys() & after.keys():
        old, new = before[name], after[name]
        if name not in pins:
            if old != new:
                errors.append(f"Unrequested dependency changed: {name}")
            continue
        # Identity/source ownership stays fixed, only the requested revision can move.
        if {key: value for key, value in old.items() if key not in {"rev", "inputRev"}} != {
                key: value for key, value in new.items() if key not in {"rev", "inputRev"}}:
            errors.append(f"Requested dependency changed unrelated metadata: {name}")
        pin = pins[name]
        if re.fullmatch(r"[0-9a-fA-F]{40}", pin):
            if new.get("rev", "").lower() != pin.lower():
                errors.append(f"Requested dependency commit mismatch: {name}")
        elif new.get("inputRev") != pin:
            errors.append(f"Requested dependency version mismatch: {name}")
    return errors


def resolve_dependencies(target: Path, baseline: dict[str, Any]) -> dict[str, Any]:
    """Resolve explicitly requested pins once and return a sealable receipt.

    A resolver's unrelated transitive update is rejected and retained for
    diagnosis; this method never rewrites pins to disguise the discrepancy.
    """
    _, owned_target = resolve_paths(Path(baseline["root"]), baseline)
    if Path(target).resolve() != owned_target:
        raise ProjectError("Dependency resolution requires the owned target workspace")
    errors = validate_target(target, baseline, require_resolved=False)
    if errors:
        raise ProjectError("; ".join(errors))
    if baseline.get("target_sealed"):
        raise ProjectError("Dependency resolution cannot modify a sealed target transition")
    direct_pins, _ = _partition_dependency_pins(baseline["original_manifest"], baseline["dependency_pins"])
    source_before = source_files(target)
    if {k: v for k, v in source_before.items() if k not in _CONFIG} != {
            k: v for k, v in baseline["source_files"].items() if k not in _CONFIG}:
        raise ProjectError("Target source changed before dependency resolution")
    # Empty `lake update` means update *every* dependency, not update none.
    # Inherited pins are exact expected commits, not independent update targets.
    command = ["lake", "update", *sorted(direct_pins)] if direct_pins else []
    result = _run(target, command) if command else subprocess.CompletedProcess([], 0, "", "")
    diagnostics = (result.stdout + "\n" + result.stderr)[-16000:]
    errors = [] if result.returncode == 0 else ["Lake dependency resolution failed"]
    try:
        manifest = _manifest(target)
        errors.extend(_pin_errors(manifest, baseline))
        if {k: v for k, v in source_files(target).items() if k != "lake-manifest.json"} != {
                k: v for k, v in source_before.items() if k != "lake-manifest.json"}:
            errors.append("Dependency resolver changed project source or non-manifest configuration")
        hashes = _config_hashes(target)
        for name, value in baseline["target_config"].items():
            if name != "lake-manifest.json" and hashes.get(name) != value:
                errors.append(f"Dependency resolver changed configuration: {name}")
    except (ProjectError, OSError, ValueError) as exc:
        manifest, hashes = None, {}
        errors.append(str(exc))
    return {"passed": not errors, "returncode": result.returncode, "errors": errors,
            "diagnostics": diagnostics, "command": command,
            "baseline_identity": baseline["identity"], "config": hashes,
            "manifest": manifest, "target_version": baseline["target_version"]}


def seal_target(target: Path, baseline: dict[str, Any], *,
                resolution: dict[str, Any] | None = None) -> dict[str, Any]:
    """Freeze exact resolved configuration before any worker gets write access."""
    _, owned = resolve_paths(Path(baseline["root"]), baseline)
    if Path(target).resolve() != owned:
        raise ProjectError("Cannot seal an unowned target")
    if baseline.get("target_sealed"):
        raise ProjectError("Target transition is already sealed")
    if resolution is None:
        if baseline["dependency_pins"]:
            raise ProjectError("Explicit dependency changes require a successful resolver receipt")
        resolution = resolve_dependencies(target, baseline)
    if not resolution.get("passed") or resolution.get("baseline_identity") != baseline["identity"]:
        raise ProjectError("Invalid dependency-resolution receipt")
    errors = validate_target(target, baseline, require_resolved=False, resolution=resolution)
    errors.extend(_pin_errors(_manifest(target), baseline))
    if errors:
        raise ProjectError("; ".join(errors))
    result = json.loads(json.dumps(baseline))
    result["preparation_identity"] = baseline["identity"]
    result["target_config"] = _config_hashes(Path(target))
    result["target_manifest"] = _manifest(Path(target))
    result["target_sealed"] = True
    result["resolution_receipt_hash"] = _digest(resolution)
    result.pop("identity", None)
    result["identity"] = _digest(result)
    return result


def validate_target(target: Path, baseline: dict[str, Any], *,
                    require_resolved: bool = True, resolution: dict[str, Any] | None = None) -> list[str]:
    """Check target config/pins; proof source changes are handled by the contract."""
    try:
        _, owned_target = resolve_paths(Path(baseline["root"]), baseline)
        target = Path(target).resolve()
        if target != owned_target:
            return ["Not the owned Bump target workspace"]
        errors = _safe_workspace(target)
        current = _config_hashes(target)
        expected = baseline["target_config"]
        if require_resolved and not baseline.get("target_sealed"):
            errors.append("Target configuration has not been sealed after dependency resolution")
        if resolution is not None:
            if not resolution.get("passed") or resolution.get("baseline_identity") != baseline["identity"]:
                errors.append("Invalid dependency-resolution receipt")
            expected = resolution.get("config", {})
        for name in set(current) | set(expected):
            if current.get(name) != expected.get(name):
                errors.append(f"Target configuration changed: {name}")
        if (target / "lean-toolchain").read_text().strip() != baseline["target_version"]:
            errors.append("Target toolchain does not match requested version")
        if require_resolved:
            errors.extend(_pin_errors(_manifest(target), baseline))
        return errors
    except (ProjectError, OSError, ValueError) as exc:
        return [str(exc)]


def _scope_seal(value: dict) -> dict:
    value = {key: item for key, item in value.items() if key != "sha256"}
    return {**value, "sha256": _digest(value)}


def _scope_path(value: Any) -> bool:
    return (isinstance(value, str) and bool(value) and "\\" not in value and "\0" not in value
            and not Path(value).is_absolute() and ".." not in Path(value).parts
            and Path(value).as_posix() == value and not _excluded(value))


def scope_errors(scope: dict, files: dict[str, str] | None = None) -> list[str]:
    """Pure validation of the immutable native-vs-byte-only boundary."""
    try:
        keys = {"version", "mode", "kind", "default_build_required", "selected_modules", "excluded_modules",
                "excluded_files", "native_default_modules", "native_metadata", "sha256"}
        if (not isinstance(scope, dict) or set(scope) != keys or scope.get("version") != 1
                or scope.get("sha256") != _scope_seal(scope)["sha256"]
                or scope.get("mode") not in {"build", "all"} or scope.get("default_build_required") is not True
                or scope.get("kind") != {"build": "default_build_closure", "all": "all_project_modules"}[scope["mode"]]):
            return ["Invalid Bump verification scope seal or policy"]
        selected, excluded, frozen = (scope[key] for key in ("selected_modules", "excluded_modules", "excluded_files"))
        def module_map(value):
            return (isinstance(value, dict) and len(set(value.values())) == len(value)
                    and all(isinstance(module, str) and _NAME.fullmatch(module) and _scope_path(path)
                            and path.endswith(".lean") and path != "lakefile.lean" for module, path in value.items()))
        if (not module_map(selected) or not selected or not module_map(excluded)
                or set(selected) & set(excluded) or set(selected.values()) & set(excluded.values())
                or not isinstance(frozen, dict) or any(not _scope_path(path) or path in _CONFIG
                    or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha) for path, sha in frozen.items())
                or set(selected.values()) & set(frozen) or not set(excluded.values()) <= set(frozen)):
            return ["Invalid Bump selected or byte-preserved file boundary"]
        defaults, native = scope["native_default_modules"], scope["native_metadata"]
        if scope["mode"] == "all":
            if excluded or defaults or native or any(path.endswith(".lean") for path in frozen):
                return ["Strict all-module scope cannot exclude Lean modules"]
        else:
            if (not module_map(defaults) or not defaults or not defaults.items() <= selected.items()
                    or not isinstance(native, dict) or set(native) != {
                        "modules", "module_owners", "source_roots", "default_targets", "build_dir"}
                    or native["modules"] != {path: module for module, path in {**selected, **excluded}.items()}
                    or not isinstance(native["module_owners"], dict)
                    or set(native["module_owners"]) != set(native["modules"])
                    or not isinstance(native["source_roots"], list) or not isinstance(native["default_targets"], list)
                    or not native["default_targets"] or not isinstance(native["build_dir"], str)
                    or not native["build_dir"]):
                return ["Incomplete native Lake default-target scope"]
            build_dir = Path(native["build_dir"])
            if build_dir.is_absolute() or ".." in build_dir.parts or build_dir.as_posix() != native["build_dir"]:
                return ["Invalid native build output path"]
            for row in native["source_roots"] + native["default_targets"]:
                if (not isinstance(row, dict) or set(row) != {"kind", "name", "path"}
                        or row["kind"] not in {"library", "executable"} or not isinstance(row["name"], str) or not row["name"]
                        or not isinstance(row["path"], str) or Path(row["path"]).is_absolute()
                        or ".." in Path(row["path"]).parts):
                    return ["Invalid native target/root identity"]
            for row in native["module_owners"].values():
                if (not isinstance(row, dict) or set(row) != {"libraries", "executables"}
                        or any(not isinstance(row[key], list) or row[key] != sorted(set(row[key]))
                               or any(not isinstance(name, str) or not name for name in row[key])
                               for key in ("libraries", "executables")) or not (row["libraries"] or row["executables"])):
                    return ["Invalid native module ownership"]
        if files is not None:
            expected = {path: sha for path, sha in files.items() if path not in _CONFIG and path not in selected.values()}
            if frozen != expected or not set(selected.values()) <= set(files):
                return ["Bump scope does not partition the complete original source inventory"]
        return []
    except (KeyError, TypeError, ValueError, AttributeError):
        return ["Malformed Bump verification scope"]


def _scope_native(root: Path) -> tuple[dict, dict[str, str], dict[str, str]]:
    """Use the pinned compiler's Lake loader; never infer ownership from paths."""
    from . import bump_native, bump_workspace
    before = source_files(root)
    files = sorted(path for path in before if path.endswith(".lean") and path != "lakefile.lean")
    layout = bump_workspace.discover(root, files)
    source = Path(__file__).with_name("bump_migration_defaults.lean")
    executable = bump_native.executable(root, source, name="migration-defaults", link_args=("-lLake",))
    result = _run(root, ["lake", "env", str(executable)])
    try:
        targets = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise ProjectError("Native Lake default-target discovery returned no JSON") from exc
    if not isinstance(targets, dict) or result.returncode or targets.get("issues") or layout.get("unknown_default_targets"):
        raise ProjectError("Opaque/custom Lake default targets are unsupported by Bump build scope")
    if before != source_files(root):
        raise ProjectError("Lake scope discovery changed project source/configuration")
    modules = layout.get("modules", {})
    if (set(modules) | set(layout.get("unmatched", [])) != set(files)
            or set(modules) & set(layout.get("unmatched", [])) or len(set(modules.values())) != len(modules)):
        raise ProjectError("Native Lake ownership did not account for every project Lean file")
    defaults = layout.get("default_modules")
    if not isinstance(defaults, dict) or not defaults or not defaults.items() <= modules.items():
        raise ProjectError("Bump build scope requires nonempty native default library/executable modules")
    metadata = {"modules": modules, "module_owners": layout["module_owners"],
                "source_roots": sorted(layout["source_roots"], key=lambda row: (row["kind"], row["name"], row["path"])),
                "default_targets": sorted(targets.get("targets", []), key=lambda row: (row["kind"], row["name"], row["path"])),
                "build_dir": layout["build_dir"]}
    return metadata, {module: path for path, module in defaults.items()}, before


def _native_headers(root: Path, modules: dict[str, str]) -> dict[str, list[str]]:
    from . import bump_workspace
    values = bump_workspace.read_imports(root, sorted(modules.values()))
    return {module: sorted(values[path]) for module, path in modules.items()}


def capture_build_scope(original: Path, baseline: dict[str, Any]) -> dict[str, Any]:
    """After the ordinary old build, freeze default native closure and all other bytes."""
    original = Path(original).resolve()
    expected, _ = resolve_paths(Path(baseline["root"]), baseline)
    requested = baseline.get("scope", {})
    if original != expected or requested.get("pending") is not True or requested.get("mode") not in {"build", "all"}:
        raise ProjectError("Bump scope must be captured exactly once in its original workspace")
    if validate_original(Path(baseline["root"]), baseline):
        raise ProjectError("Original source changed before native scope capture")
    mode = requested["mode"]
    if mode == "all":
        initial = inventory_modules(original)
        selected = {module: row["path"] for module, row in initial.items()}
        defaults, native, excluded = {}, {}, {}
        files = source_files(original)
    else:
        native, defaults, files = _scope_native(original)
        known = {module: path for path, module in native["modules"].items()}
        selected, pending = {}, set(defaults)
        while pending:
            batch = {module: known[module] for module in sorted(pending)}
            headers = _native_headers(original, batch)
            selected.update(batch)
            pending = {name for imports in headers.values() for name in imports if name in known and name not in selected}
        excluded = {module: path for module, path in known.items() if module not in selected}
        initial = {module: {"path": path, "imports": []} for module, path in selected.items()}
    scope = _scope_seal({"version": 1, "mode": mode,
        "kind": "default_build_closure" if mode == "build" else "all_project_modules", "default_build_required": True,
        "selected_modules": dict(sorted(selected.items())), "excluded_modules": dict(sorted(excluded.items())),
        "excluded_files": {path: sha for path, sha in files.items() if path not in _CONFIG and path not in selected.values()},
        "native_default_modules": defaults, "native_metadata": native})
    errors = scope_errors(scope, files)
    if errors:
        raise ProjectError("; ".join(errors))
    value = {**baseline, "scope": scope, "modules": initial}
    value.pop("identity", None)
    value["identity"] = _digest(value)
    return value


def validate_build_scope(root: Path, scope: dict) -> list[str]:
    """No source edits: selected imports may change only within the sealed boundary."""
    try:
        root = Path(root).resolve()
        errors = scope_errors(scope)
        if errors:
            return errors
        current = source_files(root)
        errors = scope_errors(scope, current)
        if errors:
            return errors
        if scope["mode"] == "all":
            return []
        native, defaults, _ = _scope_native(root)
        if native != scope["native_metadata"] or defaults != scope["native_default_modules"]:
            return ["Native Lake default targets, roots or module ownership crossed the sealed Bump boundary"]
        headers = _native_headers(root, scope["selected_modules"])
        crossed = sorted({name for names in headers.values() for name in names if name in scope["excluded_modules"]})
        if crossed:
            return ["New imports cross the excluded Bump module boundary: " + ", ".join(crossed)]
        return []
    except (ProjectError, OSError, ValueError) as exc:
        return [str(exc)]


def compiler_modules(root: Path, *, scope: dict | None = None) -> dict[str, dict[str, Any]]:
    """Obtain the local import graph from Lean, in this exact native environment.

    Lexical imports are never sufficient scheduling authority. All original
    source modules must be inspectable, including non-default/optional modules.
    No graph is returned on unsupported output or failed native extraction.
    """
    root = Path(root).resolve()
    if scope is not None:
        errors = validate_build_scope(root, scope)
        if errors:
            raise ProjectError("; ".join(errors))
        headers = _native_headers(root, scope["selected_modules"]) if scope["mode"] == "build" else None
        all_modules = inventory_modules(root) if headers is None else None
        modules = {module: {"path": path, "imports": headers[module] if headers else all_modules[module]["imports"]}
                   for module, path in scope["selected_modules"].items()}
    else:
        modules = inventory_modules(root)
    output = (scope or {}).get("native_metadata", {}).get("build_dir", ".lake/build")
    local_output = (root / output / "lib/lean").resolve()
    local_paths = {(local_output / (module.replace(".", "/") + ".olean")).resolve(): module
                   for module in modules}
    graph: dict[str, dict[str, Any]] = {}
    for module, entry in modules.items():
        result = _run(root, ["lake", "env", "lean", "--deps", entry["path"]])
        if result.returncode:
            raise ProjectError(f"Native import extraction failed for {module}: " + result.stderr[-4000:] + result.stdout[-4000:])
        imports: list[str] = []
        for raw in result.stdout.splitlines():
            name = raw.strip()
            if not name:
                continue
            if not name.endswith(".olean"):
                raise ProjectError(f"Unsupported native dependency output for {module}: {name[:400]}")
            path = Path(name)
            if not path.is_absolute():
                path = root / path
            resolved = path.resolve()
            imported = local_paths.get(resolved)
            if scope is not None and resolved.is_relative_to(local_output) and imported is None:
                raise ProjectError(f"Native import crosses the sealed Bump boundary for {module}: {name}")
            if imported is not None and imported not in imports:
                imports.append(imported)
        lexical_local = {name for name in entry["imports"] if name in modules}
        if not lexical_local.issubset(imports):
            raise ProjectError(f"Native import coverage does not include every local import for {module}")
        graph[module] = {**entry, "imports": sorted(imports), "compiler_derived": True}
    return graph


def validate_dependencies(root: Path) -> list[str]:
    """Verify actual dependency checkouts, not just declarative manifest pins.

    Invoke after Lake has materialized the environment, before trusting native
    inspection. No clones, repairs, fetches, or source changes occur here.
    """
    root = Path(root).resolve()
    try:
        errors = _safe_workspace(root)
        if errors:
            return errors
        # Standalone read-only validation still records owned Git jobs. Establish
        # the actual project registry before entering any dependency checkout.
        (root / ".unity").mkdir(exist_ok=True)
        manifest = _manifest(root)
        packages = root / ".lake" / "packages"
        for package in manifest["packages"]:
            raw_name = package["name"]
            name = raw_name[1:-1] if raw_name.startswith("«") and raw_name.endswith("»") else raw_name
            if (not name or "/" in name or "\\" in name or name in {".", ".."}
                    or "«" in name or "»" in name):
                errors.append(f"Unsupported dependency directory name: {raw_name}")
                continue
            path = packages / name
            if not path.is_dir() or path.is_symlink() or path.resolve() != path:
                errors.append(f"Dependency checkout is missing or escapes its environment: {raw_name}")
                continue
            try:
                if Path(_git(path, "rev-parse", "--show-toplevel")).resolve() != path:
                    errors.append(f"Dependency is not its own Git checkout: {raw_name}")
                    continue
                if _git(path, "rev-parse", "HEAD").lower() != package["rev"].lower():
                    errors.append(f"Dependency checkout commit differs from manifest: {raw_name}")
                if _git(path, "status", "--porcelain", "--untracked-files=all"):
                    errors.append(f"Dependency source checkout is dirty: {raw_name}")
                origin = _run(path, ["git", "remote", "get-url", "origin"], timeout=60)
                if origin.returncode or origin.stdout.strip() != package.get("url"):
                    errors.append(f"Dependency origin differs from manifest: {raw_name}")
                for filename in source_files(path):
                    candidate = path / filename
                    if candidate.is_symlink() and (filename.endswith(".lean") or filename in _CONFIG):
                        errors.append(f"Symlinked dependency source/configuration is unsupported: {raw_name}/{filename}")
            except (ProjectError, OSError, ValueError) as exc:
                errors.append(f"Cannot verify dependency {raw_name}: {exc}")
        return errors
    except (ProjectError, OSError, ValueError) as exc:
        return [str(exc)]


def build(root: Path, modules: list[str] | None = None, *, scope: dict | None = None,
          diagnostics_path: Path | None = None, diagnostic_imports: dict | None = None) -> dict[str, Any]:
    """Build exactly requested module targets, or the project's ordinary default.

    Module targets let a candidate succeed while independent modules still fail.
    This is compilation evidence only, never a preservation/faithfulness verdict.
    """
    root = Path(root).resolve()
    errors = _safe_workspace(root)
    if errors:
        raise ProjectError("; ".join(errors))
    if scope is not None:
        if diagnostic_imports is None:
            errors = validate_build_scope(root, scope)
        else:
            # Discovery-only builds may report malformed/missing imports.
            # This is never used by candidate/final compilation gates.
            from .bump_diagnostics import validate_target_imports
            validate_target_imports(diagnostic_imports)
            if diagnostics_path is None or diagnostic_imports["source_files_sha256"] != snapshot(root):
                raise ProjectError("Diagnostic-only import evidence is missing or stale")
            if {module: row["path"] for module, row in diagnostic_imports["modules"].items()} != scope["selected_modules"]:
                raise ProjectError("Diagnostic imports do not cover the exact sealed modules")
            errors = scope_errors(scope, source_files(root))
            if not errors and scope["mode"] == "build":
                native, defaults, _ = _scope_native(root)
                if native != scope["native_metadata"] or defaults != scope["native_default_modules"]:
                    errors.append("Native target ownership changed before diagnostic build")
        if errors:
            raise ProjectError("; ".join(errors))
    elif diagnostic_imports is not None:
        raise ProjectError("Diagnostic-only import evidence requires a sealed scope")
    if modules is not None:
        if not modules or any(not isinstance(module, str) or not _NAME.fullmatch(module) for module in modules):
            raise ProjectError("Module build targets must be nonempty Lean module names")
        known = scope["selected_modules"] if scope is not None else inventory_modules(root)
        if any(module not in known for module in modules):
            raise ProjectError("A requested build module is absent from the project")
    command = ["lake", "--rehash", "build"] + (["+" + module for module in dict.fromkeys(modules)] if modules else [])
    before = snapshot(root)
    complete_output = False
    try:
        if diagnostics_path is None:
            result = _run(root, command)
            diagnostics = (result.stdout + "\n" + result.stderr)[-200000:]
        else:
            diagnostics_path = Path(diagnostics_path)
            if (not diagnostics_path.is_absolute() or not diagnostics_path.parent.resolve().is_relative_to(root / ".unity")
                    or diagnostics_path.is_symlink()):
                raise ProjectError("Build diagnostics must be a new owned .unity file")
            diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(diagnostics_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                result = _run(root, command, output_stream=handle)
                handle.flush()
                os.fsync(handle.fileno())
            with diagnostics_path.open("rb") as handle:
                handle.seek(max(0, diagnostics_path.stat().st_size - 200000))
                diagnostics = handle.read().decode("utf-8", errors="replace")
            complete_output = True
        returncode = result.returncode
    except subprocess.TimeoutExpired:
        diagnostics, returncode = "Lake build exceeded the bounded timeout", 124
    except OSError as exc:
        diagnostics, returncode = str(exc), 127
    after = snapshot(root)
    cache_errors = _safe_workspace(root)
    if cache_errors:
        diagnostics += "\n" + "; ".join(cache_errors)
    if before != after:
        diagnostics += "\nBuild changed project source/configuration; receipt is invalid."
    receipt = {"passed": returncode == 0 and before == after and not cache_errors, "returncode": returncode,
            "diagnostics": diagnostics, "command": command, "source_hash": after,
            "source_unchanged": before == after, "modules": modules}
    if diagnostics_path is not None:
        receipt.update(diagnostics_path=str(diagnostics_path), diagnostics_complete=complete_output)
    return receipt
