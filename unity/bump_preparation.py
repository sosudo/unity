"""Original/target preparation for the declaration migration workflow.

Only this adapter changes the requested toolchain and package configuration.
Workers inherit the sealed target environment from the copied Bump workflow.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

from . import bump_jobs, bump_project, bump_workspace
from .bump_inventory import digest

ProjectError = ValueError
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_']*")
_CONFIG = ("lean-toolchain", "lakefile.toml", "lakefile.lean", "lake-manifest.json")
_RUNTIME = {".git", ".lake", ".unity", ".worktrees", "lake-packages", "__pycache__"}


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Bump Git operation failed")
    return result.stdout.strip()


def checked_build_dir(root: Path, build_dir: str) -> str:
    """Accept only the exact project-local directory returned by native Lake."""
    if not isinstance(build_dir, str) or not build_dir or "\x00" in build_dir:
        raise ValueError("native Lake build directory is missing or invalid")
    relative = Path(build_dir)
    root = Path(root).resolve()
    output = root / relative
    if (relative.is_absolute() or ".." in relative.parts or relative.as_posix() != build_dir
            or output == root or not output.resolve().is_relative_to(root)
            or any((root / Path(*relative.parts[:n])).is_symlink()
                   for n in range(1, len(relative.parts) + 1))
            or (output.exists() and not output.is_dir())):
        raise ValueError("native Lake build directory must be canonical and strictly project-local")
    return build_dir


def source_files(root: Path, *, build_dir: str | None = None) -> dict[str, str]:
    root = Path(root).resolve()
    output = root / checked_build_dir(root, build_dir) if build_dir is not None else None
    result = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in _RUNTIME
                   and (output is None or Path(directory) / name != output)]
        for name in [*dirs, *files]:
            path = Path(directory) / name
            if name in _RUNTIME:
                continue
            relative = path.relative_to(root)
            if path.is_symlink():
                raise ValueError("Bump input symlinks require explicit handling: " + relative.as_posix())
            if path.is_file():
                result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(result.items()))


def config_hashes(root: Path) -> dict:
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in _CONFIG if (root / name).is_file()}


def manifest(root: Path) -> dict:
    path = root / "lake-manifest.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("Bump requires a regular pinned Lake manifest")
    result = json.loads(path.read_text())
    packages = result.get("packages")
    if (not isinstance(packages, list) or result.get("packagesDir", ".lake/packages") != ".lake/packages"
            or any(not isinstance(row, dict) or row.get("type") != "git"
                   or not re.fullmatch(r"[0-9a-fA-F]{40}", str(row.get("rev", ""))) for row in packages)
            or len({row["name"] for row in packages}) != len(packages)):
        raise ValueError("Bump requires unique pinned Git dependencies in project-local storage")
    for row in packages:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", row["name"]):
            raise ValueError("invalid Lake dependency name")
        subdir = Path(row.get("subDir") or ".")
        if subdir.is_absolute() or ".." in subdir.parts:
            raise ValueError("dependency subdirectory escapes its checkout")
    return result


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



def parse_dependency_pins(values) -> dict[str, str]:
    result = {}
    for value in values:
        name, separator, revision = value.partition("=")
        if (not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name)
                or not re.fullmatch(r"[0-9a-fA-F]{40}|v[0-9]+\.[0-9]+\.[0-9]+(?:-rc[0-9]+)?", revision)
                or name in result):
            raise ValueError("dependencies require unique NAME=COMMIT or NAME=VERSION pins")
        result[name] = revision
    return result


def prepare(root: Path, version: str, pins: dict, *, run_id: str, project_scope: str) -> dict:
    root = Path(root).resolve()
    if not re.fullmatch(r"(?:leanprover/lean4:)?v[0-9]+\.[0-9]+\.[0-9]+(?:-rc[0-9]+)?|(?:leanprover/lean4:)?nightly-[0-9]{4}-[0-9]{2}-[0-9]{2}", version or ""):
        raise ValueError("Bump requires an exact Lean release or dated nightly")
    if project_scope not in {"build", "all"} or not re.fullmatch(r"bump-[0-9a-f]{12}", run_id):
        raise ValueError("invalid Bump scope or workspace identity")
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("Bump starts at the project Git root")
    bump_project._require_clean(root)
    old_manifest = manifest(root)
    packages = {row["name"]: row for row in old_manifest["packages"]}
    if set(pins) - set(packages):
        raise ValueError("requested dependency is absent from the original manifest")
    direct = {name: pin for name, pin in pins.items() if not packages[name].get("inherited", False)}
    if pins and not direct:
        raise ValueError("inherited dependency expectations need a direct dependency update")
    configs = [name for name in ("lakefile.toml", "lakefile.lean") if (root / name).is_file()]
    if len(configs) != 1:
        raise ValueError("Bump requires exactly one Lake configuration")
    lakefile = configs[0]
    updated = (_edit_toml if lakefile.endswith(".toml") else _edit_lean)((root / lakefile).read_text(), direct)
    head = git(root, "rev-parse", "HEAD")
    original_config = config_hashes(root)
    # Lake, not a guessed cache path or a second parser for lakefile.lean,
    # defines the build output excluded from the immutable source inventory.
    layout = bump_workspace.discover(root, ["--layout-only"])
    build_dir = checked_build_dir(root, layout.get("build_dir"))
    bump_project._require_clean(root)
    if git(root, "rev-parse", "HEAD") != head or config_hashes(root) != original_config:
        raise ValueError("original project changed during native layout discovery")
    original_files = source_files(root, build_dir=build_dir)
    tracked = set(git(root, "ls-files", "-z").split("\0"))
    if any(name == build_dir or name.startswith(build_dir + "/") for name in tracked):
        raise ValueError("native build directory contains tracked original inputs")
    if set(original_files) - tracked:
        raise ValueError("all original non-runtime inputs must be tracked")
    run_root = root / ".unity" / "bump" / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    original, target = run_root / "original", run_root / "target"
    git(root, "worktree", "add", "--detach", str(original), head)
    git(root, "worktree", "add", "--detach", str(target), head)
    for tree in (original, target):
        (tree / ".unity").mkdir()
    if (source_files(original, build_dir=build_dir) != original_files
            or source_files(target, build_dir=build_dir) != original_files):
        raise ValueError("fresh migration workspaces differ from tracked original inputs")
    (target / "lean-toolchain").write_text((version if ":" in version else "leanprover/lean4:" + version) + "\n")
    (target / lakefile).write_text(updated)
    return {"version": 1, "policy": "migration-v1", "run_id": run_id,
            "source_root": str(root), "original_root": str(original), "target_root": str(target),
            "original_commit": head, "original_files": original_files, "original_config": config_hashes(original),
            "original_manifest": old_manifest, "target_config": config_hashes(target),
            "target_version": (target / "lean-toolchain").read_text().strip(), "dependency_pins": dict(pins),
            "direct_dependencies": sorted(direct), "scope": {"mode": project_scope, "build_dir": build_dir}}


def capture_scope(original: Path, migration: dict) -> dict:
    build_dir = checked_build_dir(original, migration["scope"].get("build_dir"))
    files = source_files(original, build_dir=build_dir)
    if files != migration["original_files"]:
        raise ValueError("original project changed before scope capture")
    layout = bump_workspace.discover(original, ["--layout-only"])
    if checked_build_dir(original, layout.get("build_dir")) != build_dir:
        raise ValueError("original native build directory changed during preparation")
    if not isinstance(layout.get("default_modules"), dict) or layout.get("unknown_default_targets"):
        raise ValueError("Bump build scope requires native default library/executable targets")
    # Layout-only deliberately reports no per-file ownership. Ask Lake to map
    # the actual original Lean paths without elaborating them; unmatched notes
    # are byte-preserved outside build scope, not compiled or indexed.
    lean_paths = sorted(path for path in files if path.endswith(".lean") and path not in _CONFIG)
    owned = bump_workspace.discover(original, lean_paths)
    if migration["scope"]["mode"] == "all" and owned.get("unmatched"):
        raise ValueError("all-source migration includes files without native module ownership")
    known = {module: path for path, module in owned["modules"].items()}
    selected = dict(known) if migration["scope"]["mode"] == "all" else {
        module: path for path, module in layout["default_modules"].items()}
    if not selected or any(known.get(module) != path for module, path in selected.items()):
        raise ValueError("native default modules do not resolve to the exact original source paths")
    pending = set(selected)
    while pending:
        headers = bump_workspace.read_imports(original, [known[module] for module in pending])
        imported = {name for names in headers.values() for name in names if name in known}
        pending = imported - selected.keys()
        selected.update({module: known[module] for module in pending})
    scope = {"version": 1, "mode": migration["scope"]["mode"], "selected_modules": dict(sorted(selected.items())),
             "excluded_files": {name: sha for name, sha in files.items()
                                if name not in selected.values() and name not in _CONFIG},
             "default_modules": layout["default_modules"], "build_dir": layout["build_dir"]}
    scope["sha256"] = digest(scope)
    return scope


def build(root: Path, *, task_id: str = "migration-build", modules: list[str] | None = None):
    command = ["lake", "build", *(["+" + module for module in modules] if modules is not None else [])]
    return bump_jobs.run(root, command, cwd=root, task_id=task_id, serialize_build=True)


def resolve_dependencies(target: Path, migration: dict) -> dict:
    build_dir = migration["scope"]["build_dir"]
    before = source_files(target, build_dir=build_dir)
    direct = migration["direct_dependencies"]
    result = bump_jobs.run(target, ["lake", "update", *direct], cwd=target,
                           task_id="migration-dependencies", serialize_build=True) if direct else None
    if result is not None and result.returncode:
        raise ValueError("target dependency update failed: " + (result.stdout + result.stderr)[-2000:])
    after = source_files(target, build_dir=build_dir)
    if {k: v for k, v in before.items() if k != "lake-manifest.json"} != {
            k: v for k, v in after.items() if k != "lake-manifest.json"}:
        raise ValueError("dependency update changed project inputs")
    resolved = manifest(target)
    old = {row["name"]: row for row in migration["original_manifest"]["packages"]}
    new = {row["name"]: row for row in resolved["packages"]}
    if set(old) != set(new):
        raise ValueError("target dependencies changed outside the supplied complete pin set")
    for name, original in old.items():
        current = new[name]
        pin = migration["dependency_pins"].get(name)
        if pin:
            if (current["rev"] != pin if re.fullmatch(r"[0-9a-fA-F]{40}", pin)
                    else current.get("inputRev") != pin):
                raise ValueError("target dependency did not resolve its requested pin: " + name)
            if current.get("url") != original.get("url") or current.get("subDir") != original.get("subDir"):
                raise ValueError("target dependency provenance changed: " + name)
        elif current != original:
            raise ValueError("unrequested dependency changed: " + name)
    return resolved


def optional_architect(target: Path, *, mode: str, version: str, build_dir: str | None = None) -> dict:
    if mode == "off":
        return {"status": "skipped", "reason": "disabled"}
    if mode != "auto":
        raise ValueError("LeanArchitect mode must be auto or off")
    from .Architect import LEANARCHITECT_GIT, _set_requirement
    old_manifest = manifest(target)
    if any(row["name"] == "LeanArchitect" for row in old_manifest["packages"]):
        return {"status": "existing"}
    release = version.split(":")[-1]
    lookup = bump_jobs.run(target, ["git", "ls-remote", "--tags", LEANARCHITECT_GIT,
                                    "refs/tags/" + release, "refs/tags/" + release + "^{}"],
                           cwd=target, task_id="optional-architect-lookup")
    tags = {row[1]: row[0] for line in lookup.stdout.splitlines() if len(row := line.split()) == 2}
    revision = tags.get("refs/tags/" + release + "^{}", tags.get("refs/tags/" + release))
    if lookup.returncode or not revision or not re.fullmatch(r"[0-9a-f]{40}", revision):
        return {"status": "skipped", "reason": "matching optional release unavailable"}
    lakefile = next(target / name for name in ("lakefile.toml", "lakefile.lean") if (target / name).is_file())
    manifest_path = target / "lake-manifest.json"
    originals = {path: path.read_bytes() for path in (lakefile, manifest_path)}
    before = source_files(target, build_dir=build_dir)
    accepted = False
    try:
        lakefile.write_text(_set_requirement(originals[lakefile].decode(), lakefile, revision))
        updated = bump_jobs.run(target, ["lake", "update", "LeanArchitect"], cwd=target,
                                task_id="optional-architect-update", serialize_build=True)
        if {p: sha for p, sha in source_files(target, build_dir=build_dir).items() if p not in {lakefile.name, manifest_path.name}} != {
                p: sha for p, sha in before.items() if p not in {lakefile.name, manifest_path.name}}:
            raise ValueError("optional package setup changed project source")
        new = {row["name"]: row for row in manifest(target)["packages"]}
        old = {row["name"]: row for row in old_manifest["packages"]}
        if updated.returncode or set(new) != set(old) | {"LeanArchitect"} or any(new[n] != row for n, row in old.items()):
            return {"status": "skipped", "reason": "optional package requires incompatible dependencies"}
        row = new["LeanArchitect"]
        if row.get("url") != LEANARCHITECT_GIT or row.get("rev") != revision:
            raise ValueError("optional package resolved an unexpected source")
        result = bump_jobs.run(target, ["lake", "build", "LeanArchitect"], cwd=target,
                               task_id="optional-architect-build", serialize_build=True)
        if result.returncode:
            return {"status": "skipped", "reason": "matching optional package did not build"}
        accepted = True
        return {"status": "enabled", "revision": revision, "release": release}
    finally:
        if not accepted:
            for path, content in originals.items():
                path.write_bytes(content)
