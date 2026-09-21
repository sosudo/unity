"""Exact accepted-paper input binding for solve formalization."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from . import artifacts


def solve_paths(paths):
    """Solving and formalizing share solve's existing Forum and source tree."""
    return paths


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


def source_digest(paths) -> str:
    """Digest only canonical accepted paper bytes, not changing agent drafts."""
    return hashlib.sha256((paths.unity / "source" / "PROOF.tex").read_bytes()).hexdigest()


def snapshot_sources(paths) -> dict:
    """Return exact accepted provenance; never bless supplied drafts as a paper."""
    from .solve_state import formal_source, load_state

    state = load_state(paths.forum)
    require_source_matches(paths, state)
    return formal_source(state)


def source_matches(paths, state: dict) -> bool:
    """Check accepted immutable artifact/components and canonical paper bytes."""
    from .solve_state import formal_source

    try:
        source = formal_source(state)
        if not source or source_digest(paths) != source["sha256"]:
            return False
        for item in source["source_refs"]:
            payload = artifacts.artifact_bytes(paths.artifacts, item["artifact_id"])
            if hashlib.sha256(payload).hexdigest() != item["sha256"]:
                return False
        return True
    except (KeyError, OSError, ValueError):
        return False


def require_source_matches(paths, state: dict) -> None:
    """Formalization requires the same problem and independently accepted paper."""
    try:
        problem = paths.unity_md.read_bytes()
    except OSError as exc:
        raise ValueError("solve requires the original UNITY.md problem") from exc
    if hashlib.sha256(problem).hexdigest() != state.get("problem_sha256"):
        raise ValueError("solve UNITY.md changed; start a fresh run without --continue")
    if not source_matches(paths, state):
        raise ValueError("solve formalization requires exact accepted PROOF.tex and artifacts; "
                         "restore the accepted paper or use propose_source_fix/reopen_solving")
