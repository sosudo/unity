"""Dependency resolution and recursive-project coordination for compiler Bump.

These are controller operations, not worker tools.  A configuration transaction
holds the same lock as authoritative builds across edits, resolution and checks.
Nothing here installs toolchains, publishes repositories, or calls a model.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import tarfile
import tempfile
from threading import get_ident
import tomllib

from . import artifacts, bump_jobs

SCHEMA = "compiler-migration-v2"
_COMMIT = re.compile(r"[0-9a-fA-F]{40}")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_'.-]*")
_VERSION = re.compile(r"(?:leanprover/lean4:)?(?:v[0-9]+\.[0-9]+\.[0-9]+(?:-rc[0-9]+)?|nightly-[0-9]{4}-[0-9]{2}-[0-9]{2})")
_transactions: ContextVar[frozenset[tuple[str, int]]] = ContextVar("bump_configuration_transactions", default=frozenset())


class DependencyError(ValueError):
    """A dependency/configuration task needs intervention before continuing."""


def normalize_version(version: str) -> str:
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ValueError("Bump requires an exact Lean release or dated nightly")
    return version if ":" in version else "leanprover/lean4:" + version


def checked_lake_args(values=()) -> list[str]:
    """Configuration options only; do not permit cwd/toolchain/command overrides."""
    result = list(values)
    if any(not isinstance(value, str) or not re.fullmatch(r"-K[A-Za-z_][A-Za-z0-9_.-]*=[^\x00\r\n]*", value)
           for value in result):
        raise DependencyError("Bump Lake arguments must be explicit -Kkey=value configuration options")
    keys = [value.partition("=")[0] for value in result]
    if len(set(keys)) != len(keys):
        raise DependencyError("Bump Lake configuration options must have unique keys")
    return result


def parse_dependency_pins(values) -> dict[str, str]:
    result = {}
    for value in values:
        name, separator, revision = value.partition("=")
        if (not separator or not _NAME.fullmatch(name) or name in result
                or not (_COMMIT.fullmatch(revision) or _VERSION.fullmatch(revision))):
            raise ValueError("dependencies require unique NAME=COMMIT or NAME=VERSION pins")
        result[name] = revision
    return result


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    artifacts._atomic_write(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


def _local_path(root: Path, value: str, *, exists: bool = False) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise DependencyError("missing project-local dependency path")
    path = Path(value)
    root = Path(root).resolve()
    target = root / path
    if (path.is_absolute() or ".." in path.parts or path.as_posix() != value
            or target == root or not target.resolve().is_relative_to(root)
            or any((root / Path(*path.parts[:n])).is_symlink() for n in range(1, len(path.parts) + 1))
            or (exists and not target.is_dir())):
        raise DependencyError("dependency path must be canonical, project-local and not a symlink")
    return target


def read_manifest(root: Path, *, required: bool = True) -> dict:
    """Read Git or local-path packages; transitive changes are legitimate."""
    root = Path(root).resolve()
    path = root / "lake-manifest.json"
    if not path.exists() and not required:
        return {"packages": []}
    if path.is_symlink() or not path.is_file():
        raise DependencyError("Lake manifest must be a regular project file")
    data = json.loads(path.read_text())
    packages = data.get("packages")
    if not isinstance(packages, list):
        raise DependencyError("Lake manifest has no package list")
    _local_path(root, data.get("packagesDir", ".lake/packages"))
    seen = set()
    for row in packages:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"]:
            raise DependencyError("Lake manifest has an invalid package name")
        name = row["name"]
        if name in seen or any(char in name for char in ("/", "\\", "\x00")) or name in {".", ".."}:
            raise DependencyError("Lake manifest package names must be unique and local")
        seen.add(name)
        if row.get("type") == "git":
            if not _COMMIT.fullmatch(str(row.get("rev", ""))) or not isinstance(row.get("url"), str):
                raise DependencyError("Git dependency lacks a resolved revision or repository")
            _repository(row["url"])
            subdir = row.get("subDir")
            if subdir:
                _local_path(root, subdir)
        elif row.get("type") == "path":
            _local_path(root, row.get("dir", row.get("path", "")), exists=True)
        else:
            raise DependencyError("unsupported Lake dependency type")
    return data


@contextmanager
def configuration_transaction(root: Path):
    """Serialize the entire edit/update/check sequence against all builds.

    Nested calls on the same thread share the lock.  Subprocesses launched while
    inside this transaction must use ``serialize_build=False``.
    """
    project = str(Path(root).resolve())
    key = (project, get_ident())
    held = _transactions.get()
    if key in held:
        yield
        return
    with bump_jobs._build_lock(Path(project)):
        token = _transactions.set(held | {key})
        try:
            yield
        finally:
            _transactions.reset(token)


def _run(root: Path, args: list[str], *, task_id: str = "dependencies"):
    return bump_jobs.run(root, args, cwd=root, task_id=task_id, serialize_build=False)


def _repository(value: str) -> str:
    # Git supports local paths, HTTPS and scp-style SSH locations. Never accept
    # options or executable remote-helper transports such as ext::.
    if (not isinstance(value, str) or not value or value.startswith("-")
            or any(char in value for char in ("\x00", "\n", "\r"))
            or "::" in value or value.startswith("file://") and "@" in value):
        raise DependencyError("invalid dependency repository")
    return value


def discover_compatible_ref(root: Path, repository: str, version: str) -> dict:
    """Discover an exact upstream release; an absent tag is not a build result."""
    version = normalize_version(version).split(":", 1)[1]
    repository = _repository(repository)
    ref = "refs/tags/" + version
    result = _run(root, ["git", "ls-remote", "--tags", "--", repository, ref, ref + "^{}"],
                  task_id="dependency-release-lookup")
    if result.returncode:
        return {"status": "lookup_failed", "repository": repository, "version": version}
    refs = {parts[1]: parts[0] for line in result.stdout.splitlines()
            if len(parts := line.split()) == 2 and _COMMIT.fullmatch(parts[0])}
    revision = refs.get(ref + "^{}", refs.get(ref))
    return {"status": "matching_release" if revision else "no_matching_release", "repository": repository,
            "version": version, "revision": revision, "ref": ref if revision else None}


def resolve_source_revision(root: Path, repository: str, ref: str | None = None) -> str:
    """Resolve the dependency's current source when no old manifest exists."""
    if ref and _COMMIT.fullmatch(ref):
        return ref.lower()
    if ref and (ref.startswith("-") or any(c in ref for c in "\x00\r\n")):
        raise DependencyError("invalid dependency source revision")
    repository = _repository(repository)
    requested = ["HEAD"] if not ref else [ref, "refs/tags/" + ref, "refs/tags/" + ref + "^{}", "refs/heads/" + ref]
    result = _run(root, ["git", "ls-remote", "--", repository, *requested], task_id="dependency-source-lookup")
    if result.returncode:
        raise DependencyError("could not resolve dependency source revision")
    refs = {row[1]: row[0] for line in result.stdout.splitlines()
            if len(row := line.split()) == 2 and _COMMIT.fullmatch(row[0])}
    if ref and "refs/tags/" + ref + "^{}" in refs:
        return refs["refs/tags/" + ref + "^{}"].lower()
    revisions = set(refs.values())
    if len(revisions) != 1:
        raise DependencyError("dependency source revision is missing or ambiguous")
    return revisions.pop().lower()


def _lakefile(root: Path) -> Path:
    files = [root / name for name in ("lakefile.toml", "lakefile.lean") if (root / name).is_file()]
    if len(files) != 1 or files[0].is_symlink():
        raise DependencyError("Bump requires exactly one regular Lake configuration")
    return files[0]


def _lean_requirements(source: str) -> list[dict]:
    # The compiler remains authoritative. This conservative editor handles
    # ordinary literal Git/path requirements; executable declarations are
    # assigned to a configuration agent instead of guessed at.
    from .bump_preparation import _strip_comments
    masked = _strip_comments(source)
    pattern = re.compile(r'(?m)^require[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_\'.-]*)[ \t]+from[ \t]+'
                         r'(?:(?:git[ \t]*(?:\n[ \t]+)?)?(?P<url>"[^"\\\r\n]+"))'
                         r'(?:[ \t]*(?:\n[ \t]+)?@[ \t]*(?:\n[ \t]+)?(?P<rev>"[^"\\\r\n]+"))?'
                         r'[ \t]*(?:--[^\r\n]*)?(?=\r?$)')
    rows = []
    for match in pattern.finditer(source):
        if not re.search(r"\brequire\b", masked[match.start():match.end()]):
            continue
        following = next((line for line in masked[match.end():].splitlines() if line.strip()), "")
        if following and (following[0].isspace() or re.match(r"(?:@|with\b|where\b)", following)):
            continue
        rows.append({"name": match["name"], "git": json.loads(match["url"]) if "from git" in
                     re.sub(r"\s+", " ", match[0]) else None,
                     "path": json.loads(match["url"]) if "from git" not in re.sub(r"\s+", " ", match[0]) else None,
                     "rev": json.loads(match["rev"]) if match["rev"] else None,
                     "start": match.start(), "end": match.end()})
    return rows


def direct_requirements(root: Path) -> list[dict]:
    path = _lakefile(Path(root))
    if path.suffix == ".toml":
        rows = tomllib.loads(path.read_text()).get("require", [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise DependencyError("Lake TOML requirements are not a table array")
        return rows
    return _lean_requirements(path.read_text())


def edit_requirement(root: Path, name: str, *, revision: str | None = None, path: str | None = None) -> None:
    """Edit only a known literal direct requirement, never arbitrary Lean code."""
    if not _NAME.fullmatch(name) or (revision is None) == (path is None):
        raise DependencyError("require exactly one dependency revision or local path")
    if revision is not None and (not isinstance(revision, str) or any(c in revision for c in "\n\r\x00")):
        raise DependencyError("invalid dependency revision")
    root = Path(root)
    if path is not None:
        _local_path(root, path, exists=True)
    config = _lakefile(root)
    source = config.read_text()
    if config.suffix == ".lean":
        rows = [row for row in _lean_requirements(source) if row["name"] == name]
        if len(rows) != 1 or (revision is not None and not rows[0]["git"]):
            raise DependencyError("configuration task needed for nonliteral Lean requirement: " + name)
        row = rows[0]
        updated = (f"require {name} from {json.dumps(path)}" if path is not None else
                   f"require {name} from git {json.dumps(row['git'])} @ {json.dumps(revision)}")
        source = source[:row["start"]] + updated + source[row["end"]:]
    else:
        parsed = tomllib.loads(source)
        rows = [row for row in parsed.get("require", []) if row.get("name") == name]
        if len(rows) != 1 or revision is not None and not rows[0].get("git"):
            raise DependencyError("dependency is not a direct Git requirement: " + name)
        blocks = list(re.finditer(r"(?m)^\s*\[\[require\]\]\s*(?:#.*)?$", source))
        replacements = []
        for block in blocks:
            next_table = re.search(r"(?m)^\s*\[", source[block.end():])
            end = block.end() + next_table.start() if next_table else len(source)
            content = source[block.end():end]
            row = tomllib.loads("[[require]]" + content)["require"][0]
            if row.get("name") != name:
                continue
            key, value = ("path", path) if path is not None else ("rev", revision)
            removal = {"git", "rev", "path", "subDir"} if path is not None else {"rev"}
            for field in removal:
                matches = list(re.finditer(r"(?m)^\s*" + field + r"\s*=\s*(?:\"[^\"\n]*\"|'[^'\n]*')\s*(?:#.*)?$", content))
                if field in row and len(matches) != 1:
                    raise DependencyError("configuration task needed for complex TOML dependency field")
                for match in reversed(matches):
                    content = content[:match.start()] + content[match.end():]
            content = content.rstrip() + "\n" + key + " = " + json.dumps(value) + "\n\n"
            replacements.append((block.end(), end, content))
        if len(replacements) != 1:
            raise DependencyError("ambiguous TOML dependency layout")
        start, end, content = replacements[0]
        source = source[:start] + content + source[end:]
        tomllib.loads(source)
    artifacts._atomic_write(config, source.encode())


def _check_pins(manifest: dict, pins: dict[str, str]) -> None:
    rows = {row["name"]: row for row in manifest["packages"]}
    for name, pin in pins.items():
        row = rows.get(name)
        if row is None or row.get("type") != "git" or (
                row.get("rev", "").lower() != pin.lower() if _COMMIT.fullmatch(pin)
                else row.get("inputRev") != pin):
            raise DependencyError("dependency did not resolve its explicit pin: " + name)


def resolve_dependencies(root: Path, version: str, pins: dict[str, str], *, lake_args=()) -> dict:
    """Select matching direct releases, always update, then validate explicit pins.

    No complete old/new package-set equivalence is required. Release manifests
    may legitimately add, remove or change transitives. Missing direct releases
    become explicit child-project candidates, not automatic failed migrations.
    """
    root = Path(root).resolve()
    version = normalize_version(version)
    lake_args = checked_lake_args(lake_args)
    pins = dict(pins)
    parse_dependency_pins([name + "=" + pin for name, pin in pins.items()])
    receipt = {"schema": SCHEMA, "status": "pending", "target_version": version,
               "explicit_pins": pins, "selected_releases": [], "dependency_candidates": [],
               "lake_args": list(lake_args), "rationale": "matching direct releases and their resolved transitive sets"}
    with configuration_transaction(root):
        before = read_manifest(root, required=False)
        receipt["before"] = before
        old = {row["name"]: row for row in before["packages"]}
        requirements = direct_requirements(root)
        names = [row.get("name") for row in requirements]
        if len(set(names)) != len(names):
            raise DependencyError("duplicate direct dependency requirements")
        unknown = set(pins) - (set(names) | set(old))
        if unknown:
            raise DependencyError("explicit pins name unknown dependencies: " + ", ".join(sorted(unknown)))
        for row in requirements:
            name, repository = row.get("name"), row.get("git")
            if name in pins:
                edit_requirement(root, name, revision=pins[name])
            elif repository:
                release = discover_compatible_ref(root, repository, version)
                receipt["selected_releases"].append({"name": name, **release})
                if release["status"] == "matching_release":
                    edit_requirement(root, name, revision=release["version"])
                else:
                    previous = old.get(name, {})
                    revision = previous.get("rev") or resolve_source_revision(root, repository, row.get("rev"))
                    receipt["dependency_candidates"].append({"name": name, "repository": repository,
                        "revision": revision, "version": version,
                        "status": "needs_compatibility_check", "reason": release["status"]})
        # Dependency resolution must not restart under a newer compiler or
        # trigger an implicit global toolchain installation behind Bump's back.
        command = ["lake", "--keep-toolchain", *lake_args, "update"]
        result = _run(root, command, task_id="migration-dependencies")
        receipt.update(command=command, returncode=result.returncode)
        log = artifacts.store_text(root / ".unity" / "artifacts", result.stdout + "\n" + result.stderr,
                                   kind="bump_dependency_update", producer="Unity")
        receipt["log_ref"] = {key: log[key] for key in ("artifact_id", "sha256")}
        if result.returncode:
            receipt.update(status="blocked", reason="lake update failed; inspect the dependency update artifact")
        else:
            toolchain = root / "lean-toolchain"
            if toolchain.is_file() and normalize_version(toolchain.read_text().strip()) != version:
                raise DependencyError("dependency update changed the requested Lean toolchain")
            after = read_manifest(root)
            receipt["after"] = after
            _check_pins(after, pins)
            new = {row["name"]: row for row in after["packages"]}
            for candidate in receipt["dependency_candidates"]:
                current = new.get(candidate["name"])
                if current and current.get("type") == "git":
                    # An unpinned branch may advance during lake update. Recurse
                    # into the actual selected source, not the prior manifest.
                    candidate["revision"] = current["rev"]
                    candidate["repository"] = current["url"]
            receipt["changes"] = {"added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()),
                                  "changed": sorted(name for name in new.keys() & old.keys() if new[name] != old[name])}
            receipt["status"] = "resolved"
        history = root / ".unity" / "bump" / "dependency-resolution.json"
        _write_json(history, receipt)
    return receipt


def dependency_task(registry: dict, name: str, repository: str, revision: str, version: str, *, parent_id=None) -> dict:
    """Deduplicate child work by exact source and reject recursive project cycles."""
    repository = _repository(repository).rstrip("/")
    if not _COMMIT.fullmatch(revision):
        raise DependencyError("recursive migration needs an exact dependency revision")
    version = normalize_version(version)
    key = hashlib.sha256(json.dumps([repository, revision.lower(), version]).encode()).hexdigest()[:20]
    task_id = "dependency-" + key
    tasks = registry.setdefault("tasks", {})
    if parent_id is not None and parent_id not in tasks:
        raise DependencyError("unknown parent dependency task")
    # Parent links encode the project recursion graph, independent of imports.
    pending, seen = [parent_id] if parent_id else [], set()
    while pending:
        parent = pending.pop()
        if parent in seen:
            continue
        seen.add(parent)
        ancestor = tasks[parent]
        if parent == task_id or (ancestor["repository"] == repository and ancestor["version"] == version):
            raise DependencyError("recursive dependency migration cycle")
        pending.extend(ancestor.get("parents", []))
    task = tasks.setdefault(task_id, {"task_id": task_id, "kind": "dependency", "name": name,
        "repository": repository, "revision": revision.lower(), "version": version,
        "status": "pending", "parents": [], "names": [], "attempts": 0})
    if name not in task["names"]:
        task["names"].append(name)
    if parent_id and parent_id not in task["parents"]:
        task["parents"].append(parent_id)
    return task


def materialize_dependency(root: Path, task: dict) -> Path:
    """Create one owned, exact-revision child checkout; never reset an existing one."""
    root = Path(root).resolve()
    task_id = task.get("task_id", "")
    if not re.fullmatch(r"dependency-[0-9a-f]{20}", task_id) or not _COMMIT.fullmatch(task.get("revision", "")):
        raise DependencyError("invalid child migration identity")
    target = _local_path(root, ".unity/bump/dependencies/" + task_id)
    # A shared child can serve several parent projects. Reuse its verified
    # owned workspace, never create a new clone and pretend it is the result.
    if task.get("workspace"):
        recorded = Path(task["workspace"])
        if (not recorded.is_absolute() or recorded.name != task_id
                or tuple(recorded.parts[-4:-1]) != (".unity", "bump", "dependencies")):
            raise DependencyError("invalid retained dependency workspace path")
        target = _local_path(recorded.parents[3], ".unity/bump/dependencies/" + task_id, exists=True)
    origin = target / ".unity" / "dependency-origin.json"
    identity = {key: task[key] for key in ("task_id", "repository", "revision", "version")}
    if target.exists():
        if origin.is_symlink() or not origin.is_file() or json.loads(origin.read_text()) != identity:
            raise DependencyError("existing dependency workspace has another owner")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    journal_path = target.with_suffix(".materialization.json")
    if journal_path.is_symlink():
        raise DependencyError("redirected dependency materialization journal")
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else {
        "schema": SCHEMA, "identity": identity, "target": str(target), "attempts": []}
    if (journal.get("schema") != SCHEMA or journal.get("identity") != identity
            or journal.get("target") != str(target) or not isinstance(journal.get("attempts"), list)):
        raise DependencyError("dependency materialization journal has another owner")
    # The final pathname is never a half-cloned repository. An interrupted
    # incomplete staging attempt is retained and a new attempt is created.
    # A completed stage can be published without another clone.
    if journal.get("staging"):
        stage = _local_path(target.parent, journal["staging"])
        staged_origin = stage / ".unity" / "dependency-origin.json"
        if staged_origin.is_file() and not staged_origin.is_symlink():
            if json.loads(staged_origin.read_text()) != identity:
                raise DependencyError("staged dependency has another owner")
            head = _run(stage, ["git", "rev-parse", "HEAD"], task_id=task_id)
            status = _run(stage, ["git", "status", "--porcelain", "--untracked-files=no"], task_id=task_id)
            if head.returncode or head.stdout.strip() != task["revision"] or status.returncode or status.stdout.strip():
                raise DependencyError("completed dependency staging changed before publication")
            stage.rename(target)
            journal.update(status="complete")
            _write_json(journal_path, journal)
            return target
    stage = Path(tempfile.mkdtemp(prefix="." + task_id + "-clone-", dir=target.parent))
    journal["staging"] = stage.name
    journal["attempts"].append(stage.name)
    journal["status"] = "cloning"
    _write_json(journal_path, journal)
    repository = _repository(task["repository"])
    result = _run(root, ["git", "clone", "--no-checkout", "--", repository, str(stage)], task_id=task_id)
    if result.returncode:
        raise DependencyError("dependency clone failed; workspace retained")
    result = _run(stage, ["git", "checkout", "--detach", task["revision"]], task_id=task_id)
    if result.returncode:
        raise DependencyError("dependency revision checkout failed; workspace retained")
    head = _run(stage, ["git", "rev-parse", "HEAD"], task_id=task_id)
    if head.returncode or head.stdout.strip().lower() != task["revision"].lower():
        raise DependencyError("dependency checkout revision mismatch")
    _write_json(stage / ".unity" / "dependency-origin.json", identity)
    stage.rename(target)
    journal.update(status="complete")
    _write_json(journal_path, journal)
    return target


def _file_digest(handle) -> str:
    digest = hashlib.sha256()
    while chunk := handle.read(1024 * 1024):
        bump_jobs.check_cancelled()
        digest.update(chunk)
    return digest.hexdigest()


def _tree_inventory(root: Path) -> dict:
    """Exact ordinary-source inventory; links/special files are never adopted."""
    if root.is_symlink() or not root.is_dir():
        raise DependencyError("vendor publication is not an ordinary directory")
    files = {}
    for path in root.rglob("*"):
        bump_jobs.check_cancelled()
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise DependencyError("vendor publication contains a nonregular path")
        if path.is_file():
            with path.open("rb") as handle:
                files[path.relative_to(root).as_posix()] = {
                    # Git preserves executable status, not umask/read bits.
                    "sha256": _file_digest(handle), "mode": 0o755 if path.stat().st_mode & 0o100 else 0o644}
    return dict(sorted(files.items()))


def _vendor_journal(parent: Path, name: str) -> Path:
    return _local_path(parent, ".unity/bump/vendor-publications/" + name + ".json")


def validate_vendor_publication(parent: Path, record: dict) -> None:
    """Revalidate a prior publication without editing files or accepting drift."""
    parent = Path(parent).resolve()
    name = record.get("name", "")
    if not isinstance(name, str) or not _NAME.fullmatch(name) or record.get("path") != "vendor/" + name:
        raise DependencyError("invalid saved vendor publication")
    journal_path = _vendor_journal(parent, name)
    if not journal_path.is_file():
        raise DependencyError("vendor publication journal is missing")
    journal = json.loads(journal_path.read_text())
    if (journal.get("schema") != SCHEMA or journal.get("record") != record
            or journal.get("parent") != str(parent) or not isinstance(journal.get("files"), dict)):
        raise DependencyError("vendor publication journal disagrees with its exact source")
    destination = _local_path(parent, record["path"], exists=True)
    if _tree_inventory(destination) != journal["files"]:
        raise DependencyError("vendored source changed since its verified publication")


def vendor_dependency(parent: Path, child: Path, name: str, *, provenance: dict) -> dict:
    """Integrate a built child as ordinary tracked source and a relative require.

    Export a committed child snapshot rather than copying live caches, runtime
    configuration or nested Git repositories. Build verification is supplied by
    the controller and bound to that exact commit before this operation.
    """
    if not _NAME.fullmatch(name):
        raise DependencyError("invalid vendor package name")
    parent, child = Path(parent).resolve(), Path(child).resolve()
    head = _run(child, ["git", "rev-parse", "HEAD"])
    status = _run(child, ["git", "status", "--porcelain", "--untracked-files=no"])
    revision = head.stdout.strip()
    if head.returncode or status.returncode or status.stdout.strip() or not _COMMIT.fullmatch(revision):
        raise DependencyError("commit the child migration before vendoring")
    if (provenance.get("verified_commit") != revision or provenance.get("build_passed") is not True
            or provenance.get("review_approved") is not True):
        raise DependencyError("vendoring requires a successful child build and review at its exact committed revision")
    relative = "vendor/" + name
    destination = _local_path(parent, relative)
    with configuration_transaction(parent):
        record = {**provenance, "name": name, "path": relative, "commit": revision, "schema": SCHEMA}
        journal_path = _vendor_journal(parent, name)
        existing = json.loads(journal_path.read_text()) if journal_path.is_file() else None
        if existing and (existing.get("record") != record or existing.get("parent") != str(parent)
                         or existing.get("schema") != SCHEMA):
            raise DependencyError("vendor publication journal has another source identity")
        if destination.exists() and existing is None:
            raise DependencyError("vendor destination already exists; explicit integration required")
        # Archive only committed source. Build the exact inventory before any
        # parent mutation, and stage on the destination filesystem for rename.
        with tempfile.TemporaryDirectory(prefix="unity-bump-vendor-") as scratch:
            archive = Path(scratch) / "source.tar"
            result = _run(child, ["git", "archive", "--format=tar", "--output=" + str(archive), revision])
            if result.returncode:
                raise DependencyError("dependency source export failed")
            with tarfile.open(archive) as tar:
                members = tar.getmembers()
                expected = {}
                for member in members:
                    parts = Path(member.name).parts
                    if (not parts or Path(member.name).is_absolute() or ".." in parts
                            or any(p in {".git", ".unity", ".lake"} for p in parts)
                            or not (member.isfile() or member.isdir())):
                        raise DependencyError("dependency archive includes non-source or nonregular paths")
                    if member.name == "unity-bump-provenance.json":
                        raise DependencyError("dependency source already contains reserved root provenance")
                    if member.isfile():
                        with tar.extractfile(member) as handle:
                            expected[member.name] = {
                                "sha256": _file_digest(handle), "mode": 0o755 if member.mode & 0o100 else 0o644}
                if not any(re.match(r"(?i)^(?:licen[cs]e|copying|notice)(?:[._-].*)?$", Path(m.name).name)
                           and m.isfile() for m in members):
                    raise DependencyError("dependency license/provenance needs explicit review before vendoring")
                provenance_bytes = (json.dumps(record, sort_keys=True, indent=2) + "\n").encode()
                expected["unity-bump-provenance.json"] = {
                    "sha256": hashlib.sha256(provenance_bytes).hexdigest(), "mode": 0o644}
                config = _lakefile(parent)
                journal = existing or {"schema": SCHEMA, "record": record, "parent": str(parent),
                    "files": expected, "config_path": config.name,
                    "config_before_sha256": hashlib.sha256(config.read_bytes()).hexdigest(), "attempts": []}
                if journal.get("files") != expected or journal.get("config_path") != config.name:
                    raise DependencyError("vendor publication source/configuration identity changed")
                if not destination.exists():
                    stage_root = _local_path(parent, ".unity/bump/vendor-staging")
                    stage_root.mkdir(parents=True, exist_ok=True)
                    stage = None
                    if journal.get("staging"):
                        previous = _local_path(stage_root, journal["staging"])
                        if previous.is_dir() and _tree_inventory(previous) == expected:
                            stage = previous
                    if stage is None:
                        stage = Path(tempfile.mkdtemp(prefix=name + "-", dir=stage_root))
                        journal["staging"] = stage.name
                        journal["attempts"].append(stage.name)
                        journal["status"] = "extracting"
                        _write_json(journal_path, journal)
                        tar.extractall(stage, members=members, filter="data")
                        _write_json(stage / "unity-bump-provenance.json", record)
                        (stage / "unity-bump-provenance.json").chmod(0o600)
                    if _tree_inventory(stage) != expected:
                        raise DependencyError("staged vendor source differs from the verified archive")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    stage.rename(destination)
                validate_vendor_publication(parent, record)
        # A crash after rename or after this atomic configuration edit can be
        # resumed without another model run or overwriting any source bytes.
        requirements = [row for row in direct_requirements(parent) if row.get("name") == name]
        if len(requirements) != 1:
            raise DependencyError("vendor requirement needs explicit reconciliation")
        if requirements[0].get("path") != relative:
            if hashlib.sha256(config.read_bytes()).hexdigest() != journal["config_before_sha256"]:
                raise DependencyError("parent configuration changed during vendor publication")
            edit_requirement(parent, name, path=relative)
        journal.update(status="complete", config_after_sha256=hashlib.sha256(config.read_bytes()).hexdigest())
        _write_json(journal_path, journal)
    return record
