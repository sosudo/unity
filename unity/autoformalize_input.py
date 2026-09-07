"""Supplied-document input binding owned by the autoformalize workflow.

Autoformalize consumes the existing `unity source add` tree and UNITY.md scope.
It never materializes an English solution or treats supplied text as reviewed.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from . import artifacts


def autoformalize_paths(paths):
    """Isolate autoformalize coordination while preserving the source tree."""
    return replace(paths, forum=paths.unity / "forum" / "autoformalize")


def store_bytes(
    artifacts_dir: Path,
    payload: bytes,
    *,
    kind: str,
    producer: str = "",
    source: str = "",
    metadata: dict | None = None,
) -> dict:
    """Store supplied binary bytes using the generic immutable artifact layout.

    This workflow owns binary ingestion; the shared text-artifact API is unchanged.
    """
    if not isinstance(payload, bytes):
        raise TypeError("artifact payload must be bytes")
    kind = re.sub(r"\s+", "_", kind.strip().casefold())
    if not kind:
        raise ValueError("artifact kind must be non-empty")
    digest = hashlib.sha256(payload).hexdigest()
    artifact_id = "artifact-" + uuid.uuid4().hex[:12]
    record = {
        "artifact_id": artifact_id, "sha256": digest, "kind": kind,
        "producer": producer.strip(), "source": source.strip(),
        "bytes": len(payload), "lines": len(payload.decode("utf-8", errors="replace").splitlines()),
        "created_at": time.time(), "metadata": metadata or {},
    }
    encoded = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with artifacts._store_lock(artifacts_dir):
        blob = artifacts._blob_path(artifacts_dir, digest)
        if not blob.exists():
            artifacts._atomic_write(blob, payload)
        artifacts._atomic_write(artifacts._record_path(artifacts_dir, artifact_id), encoded)
    return record


def _source_files(paths) -> list[Path]:
    root = paths.unity / "source"
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Add source documents first with `unity source add <file-or-folder>`")
    files = []
    for path in sorted(root.rglob("*")):
        # Do not snapshot arbitrary external files through source symlinks.
        if path.is_symlink():
            raise ValueError(f"source symlinks are not supported: {path.relative_to(root)}")
        if path.is_file() and path.name != ".DS_Store":
            files.append(path)
    if not files or not any(path.stat().st_size for path in files):
        raise ValueError("Add nonempty source documents with `unity source add <file-or-folder>`")
    return files


def _digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_digest(paths) -> str:
    """Hash paths and exact bytes, detecting additions, removals and renames too."""
    return _digest({
        path.relative_to(paths.unity / "source").as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in _source_files(paths)
    })


def snapshot_sources(paths) -> dict:
    """Record a byte-preserving document bundle without changing the source tree."""
    refs = []
    files = {}
    for path in _source_files(paths):
        relative = path.relative_to(paths.unity / "source").as_posix()
        record = store_bytes(
            paths.artifacts, path.read_bytes(), kind="autoformalize_source",
            producer="Unity", source=f".unity/source/{relative}",
        )
        files[relative] = record["sha256"]
        refs.append({
            "ref_id": f"source:{relative}", "kind": "supplied_file",
            "path": f".unity/source/{relative}", "sha256": record["sha256"],
            "artifact_id": record["artifact_id"], "bytes": record["bytes"],
        })
    sha256 = _digest(files)
    if source_digest(paths) != sha256:
        raise ValueError("source documents changed while taking the input snapshot; retry")
    return {"kind": "supplied_sources", "candidate_id": f"source-{sha256}",
            "sha256": sha256, "source_refs": refs}


def source_matches(paths, state: dict) -> bool:
    """Check the current input bytes against the source bound to this run."""
    try:
        return source_digest(paths) == (state.get("input_source") or {}).get("sha256")
    except (OSError, ValueError):
        return False


def require_source_matches(paths, state: dict) -> None:
    """Input edits need a fresh run, not more attempts at an obsolete source."""
    try:
        scope = paths.unity_md.read_bytes()
    except OSError as exc:
        raise ValueError("autoformalize requires the original UNITY.md scope") from exc
    if (not source_matches(paths, state)
            or hashlib.sha256(scope).hexdigest() != state.get("problem_sha256")):
        raise ValueError("autoformalize sources or UNITY.md changed; start a fresh run without --continue")
