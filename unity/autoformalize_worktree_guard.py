"""Best-effort detection and targeted recovery of misplaced formalizer writes.

This is deliberately not a sandbox. Unknown or overlapping writers, changed-again
files, symlinks, and Git metadata changes are preserved but never auto-restored.
The controller must drain the offending worker and hold its merge lock before
calling ``recover``. No process is stopped and no file is restored by observation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


_EXCLUDED = {".git", ".unity", ".lake", "lake-packages", ".worktrees", "__pycache__"}
_WRITES = {"create", "modify", "delete", "edit", "write", "add", "update", "remove"}
_SUCCESS = {"completed", "complete", "success", "succeeded", "ok"}


@dataclass(frozen=True, eq=False)
class _Image:
    kind: str
    data: bytes
    mode: int = 0o644

    def __eq__(self, other) -> bool:
        # Git records only the executable permission bit. Preserve full original
        # permissions for recovery without mistaking a normal umask for a write.
        return (isinstance(other, _Image) and self.kind == other.kind and self.data == other.data
                and bool(self.mode & 0o111) == bool(other.mode & 0o111))

    def record(self) -> dict:
        return {"kind": self.kind, "mode": self.mode,
                "sha256": hashlib.sha256(self.data).hexdigest(),
                "bytes_base64": base64.b64encode(self.data).decode("ascii")}


@dataclass(frozen=True)
class _Change:
    before: _Image | None
    observed: _Image | None
    peer: bool = False


@dataclass(frozen=True)
class WorkspaceIncident:
    paths: tuple[str, ...]
    author: str | None
    artifact: Path
    message: str
    recoverable: bool
    changes: dict[str, _Change] = field(repr=False)
    metadata_changed: bool = False


class WorkspaceContamination(ValueError):
    def __init__(self, incident: WorkspaceIncident):
        self.incident = incident
        super().__init__(incident.message)


class WorkspaceGuard:
    def __init__(self, root: Path, artifacts_dir: Path, *, worktrees: dict | None = None,
                 build_dir: Path | str | None = None, poll_interval: float = 1.0):
        self.root = Path(root).resolve()
        self.artifacts_dir = Path(artifacts_dir).resolve()
        self.worktrees = {str(name): Path(path).resolve() for name, path in (worktrees or {}).items()}
        self.build_dir = None
        self.poll_interval = max(0.0, poll_interval)
        self._lock = threading.RLock()
        self._unity_depth = 0
        self._expected: dict[str, _Image] = {}
        self._tracked: set[str] = set()
        self._cache: dict[str, tuple[tuple, _Image]] = {}
        self._head = ""
        self._index = b""
        self._ready = False
        self._last_poll = -float("inf")
        self._incident: WorkspaceIncident | None = None
        self._incident_metadata: dict | None = None
        self._observations: dict[tuple[str, str, str], dict] = {}
        self._observed_changes: dict[str, _Change] = {}
        self._peer_changes: dict[str, _Change] = {}
        self.supports_safe_recovery = (
            hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
            and all(function in os.supports_dir_fd for function in
                    (os.open, os.stat, os.mkdir, os.unlink, os.rename, os.readlink)))
        self.set_build_dir(build_dir)

    def set_build_dir(self, build_dir: Path | str | None) -> None:
        """Use already-discovered Lake metadata; never run Lean while polling."""
        with self._lock:
            selected = (self.root / build_dir).resolve() if build_dir else None
            if selected is not None and (selected == self.root or not selected.is_relative_to(self.root)):
                raise ValueError("Build output exclusion must be a strict project subdirectory")
            self.build_dir = selected
            if selected is not None:
                self._expected = {name: image for name, image in self._expected.items()
                                  if not (self.root / name).is_relative_to(selected)}
                self._cache = {name: value for name, value in self._cache.items()
                               if not (self.root / name).is_relative_to(selected)}

    def _git(self, *args: str, input: bytes | None = None) -> bytes:
        result = subprocess.run(["git", *args], cwd=self.root, input=input,
                                capture_output=True, check=True, timeout=15)
        return result.stdout

    def _git_state(self) -> tuple[str, bytes]:
        head = self._git("rev-parse", "HEAD").decode().strip()
        return head, self._git("ls-files", "--stage", "-z")

    @staticmethod
    def _index_entries(index: bytes) -> dict[str, tuple[str, str]]:
        entries = {}
        for record in index.split(b"\0"):
            if not record:
                continue
            metadata, name = record.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split()
            if stage != "0" or mode not in {"100644", "100755", "120000"}:
                raise ValueError("Workspace guard requires a resolved regular-file Git index")
            entries[os.fsdecode(name)] = (mode, oid)
        return entries

    def _excluded(self, path: Path) -> bool:
        relative = path.relative_to(self.root)
        return (any(part in _EXCLUDED for part in relative.parts)
                or (self.build_dir is not None and path.is_relative_to(self.build_dir))
                or path.is_relative_to(self.artifacts_dir))

    @contextmanager
    def _parent(self, relative: str, *, create: bool = False, root: Path | None = None):
        """Use descriptor-relative, no-follow traversal, including during undo."""
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or any(part in {"..", "."} for part in parts):
            raise ValueError("Unsafe workspace-relative path")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(root or self.root, flags)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o755, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor, parts[-1]
        finally:
            os.close(descriptor)

    def _read(self, relative: str, *, cached: bool = False, root: Path | None = None) -> _Image | None:
        if not self.supports_safe_recovery:
            return self._read_portable(relative, root=root)
        try:
            with self._parent(relative, root=root) as (parent, name):
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    return _Image("symlink", os.fsencode(os.readlink(name, dir_fd=parent)), 0o777)
                if not stat.S_ISREG(info.st_mode):
                    return _Image("special", b"", stat.S_IMODE(info.st_mode))
                key = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                       info.st_ctime_ns, stat.S_IMODE(info.st_mode))
                old = self._cache.get(relative) if root is None else None
                if cached and old and old[0] == key:
                    return old[1]
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                with os.fdopen(descriptor, "rb") as handle:
                    opened = os.fstat(handle.fileno())
                    data = handle.read()
                    finished = os.fstat(handle.fileno())
                if (opened.st_ino != info.st_ino or opened.st_dev != info.st_dev
                        or opened.st_mtime_ns != finished.st_mtime_ns
                        or opened.st_ctime_ns != finished.st_ctime_ns
                        or opened.st_size != finished.st_size):
                    raise OSError(f"File changed during workspace snapshot: {relative}")
                image = _Image("file", data, stat.S_IMODE(info.st_mode))
                if root is None:
                    self._cache[relative] = (key, image)
                return image
        except FileNotFoundError:
            if root is None:
                self._cache.pop(relative, None)
            return None

    def _read_portable(self, relative: str, *, root: Path | None = None) -> _Image | None:
        """Read-only detection fallback; unsupported platforms never auto-undo.

        Before/after lstat checks avoid retaining a snapshot when a parent or
        target changed during the read. OS-level write isolation is not claimed.
        """
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or ".." in parts:
            raise ValueError("Unsafe workspace-relative path")
        path = root or self.root
        parents = []
        try:
            for part in parts[:-1]:
                path = path / part
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    return _Image("unsafe", b"Non-directory or symlink parent")
                parents.append((path, info.st_dev, info.st_ino, info.st_mtime_ns))
            path = path / parts[-1]
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                return _Image("symlink", os.fsencode(os.readlink(path)), 0o777)
            if not stat.S_ISREG(info.st_mode):
                return _Image("special", b"", stat.S_IMODE(info.st_mode))
            data = path.read_bytes()
            after = path.lstat()
            identity = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_size,
                                     item.st_mtime_ns, item.st_ctime_ns)
            if identity(info) != identity(after):
                raise OSError(f"File changed during workspace snapshot: {relative}")
            for parent, device, inode, modified in parents:
                current = parent.lstat()
                if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino, current.st_mtime_ns) != (device, inode, modified):
                    raise OSError(f"Parent changed during workspace snapshot: {relative}")
            return _Image("file", data, stat.S_IMODE(info.st_mode))
        except FileNotFoundError:
            return None

    def _scan(self) -> dict[str, _Image]:
        result = {}

        def failed(error):
            raise error

        for directory, directories, filenames in os.walk(self.root, followlinks=False, onerror=failed):
            base = Path(directory)
            kept = []
            for name in directories:
                path = base / name
                if self._excluded(path):
                    continue
                if path.is_symlink():
                    filenames.append(name)
                else:
                    kept.append(name)
            directories[:] = kept
            for name in filenames:
                path = base / name
                if self._excluded(path):
                    continue
                relative = path.relative_to(self.root).as_posix()
                image = self._read(relative, cached=True)
                if image is not None:
                    result[relative] = image
        return result

    def capture_baseline(self) -> None:
        """Call only before launching workers, after the controller's clean check."""
        with self._lock:
            if self._ready:
                raise RuntimeError("Workspace baseline is already established")
            head, index = self._git_state()
            entries = self._index_entries(index)
            images = self._scan()
            if (head, index) != self._git_state():
                raise ValueError("Git changed while establishing workspace baseline")
            self._expected, self._head, self._index = images, head, index
            self._tracked = set(entries)
            self._ready = True

    @contextmanager
    def unity_write(self):
        """Serialize short controller writes and observation, not long Lean builds."""
        with self._lock:
            self._unity_depth += 1
            try:
                yield
            finally:
                self._unity_depth -= 1

    def expect_git_index(self, *, expected_head: str | None = None) -> None:
        """Accept controller-owned index bytes, never arbitrary working-tree bytes.

        A commit must supply its exact expected HEAD explicitly. Without that
        argument even this controller-only operation refuses changed refs.
        """
        with self._lock:
            if not self._ready or not self._unity_depth:
                raise RuntimeError("Expected index updates require unity_write() and an established baseline")
            if self._incident is not None:
                raise WorkspaceContamination(self._incident)
            if self._observed_changes or self._peer_changes:
                # Do not absorb a previously observed temporary write into a new
                # candidate expectation, even if the working tree is clean now.
                self.assert_expected()
            head, index = self._git_state()
            if head != (self._head if expected_head is None else expected_head):
                self.assert_expected()
                raise ValueError("Unexpected main HEAD")
            entries = self._index_entries(index)
            objects = list(dict.fromkeys(oid for _, oid in entries.values()))
            payload = self._git("cat-file", "--batch", input=("\n".join(objects) + "\n").encode()) if objects else b""
            contents = {}
            offset = 0
            for oid in objects:
                end = payload.index(b"\n", offset)
                actual_oid, kind, size = payload[offset:end].decode().split()
                if actual_oid != oid or kind != "blob":
                    raise ValueError("Unexpected Git object while recording candidate source")
                size = int(size)
                contents[oid] = payload[end + 1:end + 1 + size]
                offset = end + size + 2
            expected = {name: image for name, image in self._expected.items() if name not in self._tracked}
            for name, (mode, oid) in entries.items():
                if not self._excluded(self.root / name):
                    expected[name] = _Image("symlink" if mode == "120000" else "file", contents[oid],
                                            0o777 if mode == "120000" else (0o755 if mode == "100755" else 0o644))
            self._expected, self._head, self._index, self._tracked = expected, head, index, set(entries)
            self.assert_expected()
            self._observations.clear()

    def _observed_path(self, observation: dict) -> Path | None:
        raw = observation.get("path")
        if not isinstance(raw, str) or not raw:
            return None  # Shell cwd alone is never proof of a write.
        path = Path(raw)
        if not path.is_absolute():
            cwd = observation.get("execution_cwd")
            if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                return None
            path = Path(cwd) / path
        # Normalize '..', but do not resolve symlinks (that could erase evidence).
        return Path(os.path.abspath(path))

    def observe_tool(self, observation: dict) -> None:
        """Consume exact editor-path events; never parse arbitrary shell strings."""
        with self._lock:
            if not self._ready:
                return
            agent, tool_id = observation.get("agent"), observation.get("tool_id")
            path = self._observed_path(observation)
            operation = str(observation.get("operation", "")).lower()
            event = observation.get("event")
            if (not agent or not tool_id or path is None or operation not in _WRITES
                    or event not in {"started", "completed"}):
                return
            own = self.worktrees.get(str(agent))
            if own is not None and path.is_relative_to(own):
                return
            peer = any(path.is_relative_to(tree) for name, tree in self.worktrees.items() if name != agent)
            if not peer and (not path.is_relative_to(self.root) or self._excluded(path)):
                return
            name = path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else str(path)
            key = (str(agent), str(tool_id), name)
            # Peer files are not snapshotted wholesale. Record an exact native
            # write as an incident, but never attempt to restore that checkout.
            if peer:
                tree = next(tree for name, tree in self.worktrees.items()
                            if name != agent and path.is_relative_to(tree))
                try:
                    current = self._read(path.relative_to(tree).as_posix(), root=tree)
                except OSError:
                    current = _Image("unsafe", b"")
                if event == "started":
                    self._observations.setdefault(key, {"agent": str(agent), "before": current,
                                                        "valid": False, "peer": True})
                if event == "completed" and str(observation.get("status", "")).lower() in _SUCCESS:
                    entry = self._observations.setdefault(key, {"agent": str(agent), "peer": True})
                    entry["valid"] = True
                    self._peer_changes.setdefault(name, _Change(entry.get("before"), current, peer=True))
                    self._preserve({name: self._peer_changes[name]})
                return
            try:
                current = self._read(name)
            except OSError:
                current = _Image("unsafe", b"")
            # Native notifications may arrive after the write, or even out of
            # order. Only Unity's baseline supplies authoritative original bytes.
            # A late started event must not erase already received completion.
            entry = self._observations.setdefault(key, {"agent": str(agent), "valid": False})
            if event == "completed" and "after" not in entry:
                entry["after"] = current
                entry["valid"] = str(observation.get("status", "")).lower() in _SUCCESS
                if str(observation.get("status", "")).lower() in _SUCCESS and current != self._expected.get(name):
                    # Retain exact completed writes even if the worker cleans up
                    # before polling. Coalesce paths only during poll: a native
                    # multi-file patch emits several completed-path observations.
                    change = _Change(self._expected.get(name), current)
                    self._observed_changes.setdefault(name, change)
                    self._preserve({name: change})

    def _author(self, changes: dict[str, _Change]) -> str | None:
        """Identify the worker to stop, independently of permission to undo."""
        authors = set()
        for path in changes:
            evidence = [item for (_, _, name), item in self._observations.items() if name == path]
            writers = {item["agent"] for item in evidence}
            if len(writers) != 1:
                return None
            authors.update(writers)
        return next(iter(authors)) if len(authors) == 1 else None

    def _classify(self, changes: dict[str, _Change], *, metadata: bool = False) -> tuple[str | None, bool]:
        author = None if metadata else self._author(changes)
        recoverable = self.supports_safe_recovery and bool(changes) and author is not None and not metadata and all(
            not change.peer and all(image is None or image.kind == "file"
                                    for image in (change.before, change.observed))
            and any(item.get("valid") and item.get("after") == change.observed
                    for (_, _, name), item in self._observations.items() if name == path)
            for path, change in changes.items())
        return author, recoverable

    def _preserve(self, changes: dict[str, _Change], *, metadata: dict | None = None) -> WorkspaceIncident:
        author, recoverable = self._classify(changes, metadata=bool(metadata))
        directory = self.artifacts_dir / "workspace-incidents" / uuid.uuid4().hex
        directory.mkdir(parents=True, mode=0o700)
        artifact = directory / "incident.json"
        paths = tuple(sorted(changes)) + ((".git/HEAD or index",) if metadata else ())
        message = ("Unauthorized workspace changes: " + ", ".join(paths[:20])
                   + (f" (worker {author})" if author else " (writer not safely attributable)")
                   + f". Preserved at {artifact}.")
        record = {"paths": paths, "author": author, "recoverable": recoverable, "message": message,
                  "metadata": metadata, "created_at": time.time(),
                  "changes": {name: {"before": change.before.record() if change.before else None,
                                     "observed": change.observed.record() if change.observed else None,
                                     "peer": change.peer} for name, change in changes.items()}}
        with artifact.open("x", encoding="utf-8") as handle:
            os.chmod(artifact, 0o600)
            json.dump(record, handle, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        return WorkspaceIncident(paths, author, artifact, message, recoverable, changes, bool(metadata))

    def _refresh_incident(self) -> WorkspaceIncident:
        """Reclassify delayed evidence without replacing any preserved bytes."""
        incident = self._incident
        assert incident is not None
        changes = dict(incident.changes)
        # Further native writes can arrive during cancellation. Retain new paths
        # (including peer writes), never replacing an earlier path's snapshot.
        for observed in (self._observed_changes, self._peer_changes):
            for name, change in observed.items():
                changes.setdefault(name, change)
        author, recoverable = self._classify(changes, metadata=incident.metadata_changed)
        if changes != incident.changes or (author, recoverable) != (incident.author, incident.recoverable):
            self._incident = self._preserve(changes, metadata=self._incident_metadata)
        return self._incident

    def poll_due(self, force: bool = False) -> WorkspaceIncident | None:
        with self._lock:
            if not self._ready:
                raise RuntimeError("Workspace baseline has not been established")
            if self._incident is not None:
                return self._refresh_incident()
            now = time.monotonic()
            if not force and not self._observed_changes and not self._peer_changes and now - self._last_poll < self.poll_interval:
                return None
            self._last_poll = now
            current = self._scan()
            head, index = self._git_state()
            changes = {name: _Change(self._expected.get(name), current.get(name))
                       for name in set(current) | set(self._expected)
                       if self._expected.get(name) != current.get(name)}
            for name, change in self._observed_changes.items():
                changes[name] = change  # Keep the first preserved postimage, even before the first poll.
            changes.update(self._peer_changes)
            metadata = None
            if (head, index) != (self._head, self._index):
                metadata = {"expected_head": self._head, "observed_head": head,
                            "expected_index_base64": base64.b64encode(self._index).decode(),
                            "observed_index_base64": base64.b64encode(index).decode()}
            if changes or metadata:
                self._incident_metadata = metadata
                self._incident = self._preserve(changes, metadata=metadata)
            return self._incident

    def assert_expected(self) -> None:
        incident = self.poll_due(force=True)
        if incident is not None:
            raise WorkspaceContamination(incident)

    def recover(self, incident: WorkspaceIncident) -> bool:
        """Targeted undo; caller must drain the offending worker/integration and lock merges."""
        with self._lock:
            if self._incident is not None:
                self._refresh_incident()
            if incident is not self._incident or not incident.recoverable:
                raise WorkspaceContamination(incident)
            if self._git_state() != (self._head, self._index):
                raise WorkspaceContamination(incident)
            current = self._scan()
            extra = {name: _Change(self._expected.get(name), current.get(name))
                     for name in set(current) | set(self._expected)
                     if name not in incident.changes and self._expected.get(name) != current.get(name)}
            if extra:
                # More writes can land between a latched event and cancellation.
                # Preserve them before touching any file from the first incident.
                self._preserve(extra)
                raise WorkspaceContamination(incident)
            # Validate the whole transaction before the first write. Descriptor
            # traversal refuses changed parent symlinks; no recursive cleanup.
            try:
                for name, change in incident.changes.items():
                    current = self._read(name)
                    if current != change.observed and current != change.before:
                        raise WorkspaceContamination(incident)
                for name, change in incident.changes.items():
                    # An agent may have restored the exact expected bytes before
                    # its cancellation was delivered. Nothing needs undoing.
                    if self._read(name) == change.before:
                        continue
                    with self._parent(name, create=change.before is not None) as (parent, leaf):
                        if self._read(name) != change.observed:
                            raise WorkspaceContamination(incident)
                        if change.before is None:
                            os.unlink(leaf, dir_fd=parent)
                        else:
                            temporary = f".unity-recover-{uuid.uuid4().hex}"
                            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                                 change.before.mode, dir_fd=parent)
                            try:
                                with os.fdopen(descriptor, "wb") as handle:
                                    handle.write(change.before.data)
                                    handle.flush()
                                    os.fsync(handle.fileno())
                                os.rename(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
                            finally:
                                try:
                                    os.unlink(temporary, dir_fd=parent)
                                except FileNotFoundError:
                                    pass
                        self._cache.pop(name, None)
            except OSError as exc:
                raise WorkspaceContamination(incident) from exc
            self._incident = None
            self._incident_metadata = None
            self._observed_changes.clear()
            self._peer_changes.clear()
            self._observations.clear()
            self.assert_expected()
            return True
