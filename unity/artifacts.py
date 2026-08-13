"""Immutable, content-addressed artifacts for Unity run output."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


DEFAULT_INLINE_THRESHOLD = 16_384
DEFAULT_PREVIEW_BYTES = 4_096
DEFAULT_READ_LIMIT = 12_000
MAX_READ_LIMIT = 32_000

_ARTIFACT_ID_RE = re.compile(r"^artifact-[0-9a-f]{12}$")


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def inline_threshold() -> int:
    return _positive_env("UNITY_ARTIFACT_THRESHOLD_BYTES", DEFAULT_INLINE_THRESHOLD)


def preview_limit() -> int:
    return _positive_env("UNITY_ARTIFACT_PREVIEW_BYTES", DEFAULT_PREVIEW_BYTES)


def default_read_limit() -> int:
    return min(
        _positive_env("UNITY_ARTIFACT_READ_LIMIT_BYTES", DEFAULT_READ_LIMIT),
        MAX_READ_LIMIT,
    )


def _layout(artifacts_dir: Path) -> tuple[Path, Path]:
    root = Path(artifacts_dir)
    blobs = root / "blobs"
    records = root / "records"
    blobs.mkdir(parents=True, exist_ok=True)
    records.mkdir(parents=True, exist_ok=True)
    return blobs, records


@contextmanager
def _store_lock(artifacts_dir: Path) -> Iterator[None]:
    root = Path(artifacts_dir)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "artifacts.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _atomic_write(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _validate_artifact_id(artifact_id: str) -> str:
    if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
        raise ValueError(f"invalid artifact id '{artifact_id}'")
    return artifact_id


def _record_path(artifacts_dir: Path, artifact_id: str) -> Path:
    _, records = _layout(artifacts_dir)
    return records / f"{_validate_artifact_id(artifact_id)}.json"


def _blob_path(artifacts_dir: Path, sha256: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("invalid artifact SHA-256")
    blobs, _ = _layout(artifacts_dir)
    return blobs / sha256


def store_text(
    artifacts_dir: Path,
    content: str,
    *,
    kind: str,
    producer: str = "",
    source: str = "",
    metadata: dict | None = None,
) -> dict:
    """Store exact UTF-8 text and return its immutable artifact record."""
    if not isinstance(content, str):
        raise TypeError("artifact content must be text")
    kind = re.sub(r"\s+", "_", kind.strip().casefold())
    if not kind:
        raise ValueError("artifact kind must be non-empty")
    payload = content.encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    artifact_id = "artifact-" + uuid.uuid4().hex[:12]
    record = {
        "artifact_id": artifact_id,
        "sha256": digest,
        "kind": kind,
        "producer": producer.strip(),
        "source": source.strip(),
        "bytes": len(payload),
        "lines": len(content.splitlines()),
        "created_at": time.time(),
        "metadata": metadata or {},
    }
    encoded_record = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with _store_lock(artifacts_dir):
        blob = _blob_path(artifacts_dir, digest)
        if not blob.exists():
            _atomic_write(blob, payload)
        _atomic_write(_record_path(artifacts_dir, artifact_id), encoded_record)
    return record


def artifact_info(artifacts_dir: Path, artifact_id: str) -> dict:
    """Return metadata for one artifact without loading its content."""
    path = _record_path(artifacts_dir, artifact_id)
    if not path.is_file():
        raise ValueError(f"unknown artifact '{artifact_id}'")
    try:
        record = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"artifact record '{artifact_id}' is unreadable") from exc
    if record.get("artifact_id") != artifact_id:
        raise ValueError(f"artifact record '{artifact_id}' is invalid")
    return record


def artifact_bytes(artifacts_dir: Path, artifact_id: str) -> bytes:
    """Load the exact artifact bytes for non-model download paths."""
    record = artifact_info(artifacts_dir, artifact_id)
    blob = _blob_path(artifacts_dir, record["sha256"])
    if not blob.is_file():
        raise ValueError(f"artifact blob for '{artifact_id}' is missing")
    return blob.read_bytes()


def read_artifact(
    artifacts_dir: Path,
    artifact_id: str,
    *,
    offset: int = 0,
    limit: int = DEFAULT_READ_LIMIT,
) -> dict:
    """Read a bounded byte page from an artifact."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    limit = min(limit, MAX_READ_LIMIT)
    record = artifact_info(artifacts_dir, artifact_id)
    payload = artifact_bytes(artifacts_dir, artifact_id)
    start = min(offset, len(payload))
    end = min(start + limit, len(payload))
    content = payload[start:end].decode("utf-8", errors="replace")
    return {
        **record,
        "offset": start,
        "returned_bytes": end - start,
        "total_bytes": len(payload),
        "content": content,
        "next_offset": end if end < len(payload) else None,
    }


def preview_text(content: str, limit: int | None = None) -> str:
    """Return a bounded head/tail preview measured in UTF-8 bytes."""
    maximum = limit or preview_limit()
    payload = content.encode("utf-8")
    if len(payload) <= maximum:
        return content
    marker = b"\n... [artifact preview clipped] ...\n"
    available = max(maximum - len(marker), 2)
    head_size = available * 2 // 3
    tail_size = available - head_size
    head = payload[:head_size].decode("utf-8", errors="ignore")
    tail = payload[-tail_size:].decode("utf-8", errors="ignore")
    return head + marker.decode() + tail


def compact_text(
    artifacts_dir: Path,
    content: str,
    *,
    kind: str,
    producer: str = "",
    source: str = "",
    threshold: int | None = None,
    metadata: dict | None = None,
) -> str | dict:
    """Return small text inline or store large text and return a compact descriptor."""
    cutoff = threshold if threshold is not None else inline_threshold()
    if len(content.encode("utf-8")) <= cutoff:
        return content
    record = store_text(
        artifacts_dir,
        content,
        kind=kind,
        producer=producer,
        source=source,
        metadata=metadata,
    )
    return {**record, "preview": preview_text(content)}


def format_compacted(compacted: str | dict, *, exit_code: int | None = None) -> str:
    """Render compacted output for an agent-facing CLI response."""
    if isinstance(compacted, str):
        return compacted
    prefix = f"Command exited {exit_code}.\n" if exit_code is not None else ""
    return (
        prefix
        + "Relevant output:\n"
        + compacted["preview"]
        + f"\nFull output: {compacted['artifact_id']}\n"
        + f"SHA-256: {compacted['sha256']}\n"
        + f"Size: {compacted['bytes']} bytes / {compacted['lines']} lines\n"
        + "Read: unity mcp unity-forum artifact_read "
        + f"'{{\"artifact_id\":\"{compacted['artifact_id']}\"}}'"
    )


def list_artifacts(artifacts_dir: Path, limit: int = 200) -> list[dict]:
    """List recent artifact records without reading their blobs."""
    _, records = _layout(artifacts_dir)
    found = []
    for path in records.glob("artifact-*.json"):
        try:
            found.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    found.sort(key=lambda item: item.get("created_at", 0), reverse=True)
    return found[:max(0, min(limit, 1000))]


def artifact_stats(artifacts_dir: Path) -> dict:
    """Return record count plus logical and deduplicated physical content sizes."""
    _, records = _layout(artifacts_dir)
    count = 0
    logical_bytes = 0
    blobs: dict[str, int] = {}
    for path in records.glob("artifact-*.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        count += 1
        size = int(record.get("bytes", 0) or 0)
        logical_bytes += size
        digest = record.get("sha256")
        if isinstance(digest, str):
            blobs[digest] = max(blobs.get(digest, 0), size)
    return {
        "count": count,
        "logical_bytes": logical_bytes,
        "stored_bytes": sum(blobs.values()),
        "unique_blobs": len(blobs),
    }
