"""Conservative raw kernel-inspection cache; never stores acceptance decisions.

An unseen import closure first warms a path hint. A later inspection may publish
only after hashing that complete closure before AND after importing it. Missing,
changed, corrupt or unavailable evidence is a cache miss, not a verification.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from . import artifacts


class CacheUnavailable(ValueError):
    def __init__(self, identity: dict):
        super().__init__("compiled import hint could not be stored")
        self.identity = identity


def _directory(root: Path) -> Path:
    directory = root / ".unity" / "verification-cache"
    if directory.is_symlink():
        raise ValueError("inspection cache cannot be a symlink")
    return directory


def _read(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("inspection cache entry cannot be a symlink")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("invalid inspection cache entry")
    return value


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid.uuid4().hex}.json")
    try:
        temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def compiled_identity(modules: list[str]) -> dict:
    if not isinstance(modules, list) or not modules:
        raise ValueError("missing compiled import closure")
    result = {}
    for filename in sorted(set(modules)):
        if not isinstance(filename, str) or not Path(filename).is_absolute() or not filename.endswith(".olean"):
            raise ValueError("invalid compiled module path")
        base = Path(filename)
        if not base.is_file():
            raise ValueError("missing compiled module")
        # importAll can load private/server data and interpreter IR. Absence is
        # recorded too, so a newly appearing companion invalidates reuse.
        for path in (base, Path(filename + ".server"), Path(filename + ".private"),
                     base.with_suffix(".ir"), base.with_suffix(".ir.sig")):
            if not path.exists():
                result[str(path)] = None
                continue
            if not path.is_file():
                raise ValueError("compiled input is not a regular file")
            hashed = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    hashed.update(block)
            result[str(path)] = {"path": str(path.resolve()), "sha256": hashed.hexdigest()}
    return result


def lookup(root: Path, key: str) -> tuple[dict | None, dict | None]:
    """Return checked raw data, plus a fresh pre-import closure observation."""
    before = None
    try:
        directory = _directory(root)
        try:
            entry = _read(directory / f"{key}.json")
        except (OSError, ValueError):
            entry = _read(directory / "imports.json")
        before = compiled_identity(entry["compiled_modules"])
        if entry.get("key") != key or entry.get("compiled_identity") != before:
            return None, before
        payload = artifacts.artifact_bytes(root / ".unity" / "artifacts", entry["artifact_id"])
        if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
            return None, before
        report = json.loads(payload)
        if not isinstance(report, dict) or report.get("compiled_modules") != entry["compiled_modules"]:
            return None, before
        return report, before
    except (OSError, ValueError, KeyError, TypeError):
        return None, before


def publish(root: Path, key: str, report: dict, before: dict | None) -> dict | None:
    try:
        modules = report["compiled_modules"]
        after = compiled_identity(modules)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        directory = _directory(root)
        _write(directory / "imports.json", {"compiled_modules": modules})
        if before is None or after != before:
            return
        record = artifacts.store_text(root / ".unity" / "artifacts",
            json.dumps(report, sort_keys=True), kind="solve_kernel_inspection", producer="Unity")
        _write(directory / f"{key}.json", {"key": key, "compiled_modules": modules,
            "compiled_identity": after, "artifact_id": record["artifact_id"], "sha256": record["sha256"]})
    except (OSError, ValueError, KeyError, TypeError):
        if before != after:
            raise CacheUnavailable(after)
        # Checked compiled identity does not depend on caching succeeding.
    return after


def compiled_receipt(root: Path, identity: dict | None) -> dict | None:
    """Keep the potentially large import manifest in an integrity-bound artifact."""
    if not identity:
        return None
    try:
        record = artifacts.store_text(root / ".unity" / "artifacts", json.dumps(identity, sort_keys=True),
                                      kind="solve_compiled_inputs", producer="Unity")
        return {"artifact_id": record["artifact_id"], "sha256": record["sha256"]}
    except (OSError, ValueError):
        return None


def compiled_receipt_current(root: Path, receipt: dict | None) -> bool:
    try:
        if not isinstance(receipt, dict):
            return False
        payload = artifacts.artifact_bytes(root / ".unity" / "artifacts", receipt["artifact_id"])
        if hashlib.sha256(payload).hexdigest() != receipt["sha256"]:
            return False
        expected = json.loads(payload)
        if not isinstance(expected, dict):
            return False
        modules = [name for name in expected if name.endswith(".olean")]
        return compiled_identity(modules) == expected
    except (OSError, ValueError, KeyError, TypeError):
        return False
