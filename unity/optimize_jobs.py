"""Owned subprocesses for the ``unity optimize`` control plane.

Long-running deterministic checks must outlive neither their optimize runtime nor
their cancellation request.  This registry intentionally covers Unity-owned
jobs only; model shell commands are not treated as authoritative checks.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import time
import uuid
import codecs
import selectors
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path
from threading import Event, get_ident


class JobCancelled(ValueError):
    """A controller check stopped cooperatively; callers must roll back main."""


_cancellation: ContextVar[Event | None] = ContextVar("optimize_job_cancellation", default=None)
_held_build_locks: ContextVar[tuple] = ContextVar("optimize_build_locks", default=())


@contextmanager
def cancellation_scope(event: Event | None):
    """Apply one integration's cancellation token to all its nested checks."""
    token = _cancellation.set(event)
    try:
        yield
    finally:
        _cancellation.reset(token)


def cancellation_disabled():
    """Rollback must run even after the check that required it was cancelled."""
    return cancellation_scope(None)


def check_cancelled() -> None:
    event = _cancellation.get()
    if event is not None and event.is_set():
        raise JobCancelled("optimize verification cancelled")


def _signal_process(proc, sig: int, *, client_group: bool = False) -> None:
    if (os.name != "posix" or client_group) and proc.poll() is not None:
        return
    try:
        if os.name == "posix" and not client_group:
            os.killpg(proc.pid, sig)
        else:
            proc.send_signal(sig)
    except ProcessLookupError:
        pass


def _cancel_process(proc, *, client_group: bool = False) -> None:
    _signal_process(proc, signal.SIGTERM, client_group=client_group)
    try:
        proc.communicate(timeout=0.25)
    except subprocess.TimeoutExpired:
        _signal_process(proc, signal.SIGKILL, client_group=client_group)
        proc.communicate()


def _jobs_dir(project_root: Path) -> Path:
    path = Path(project_root) / ".unity" / "jobs" / "optimize"
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _build_lock(project_root: Path):
    """Serialize authoritative optimize builds across controller processes."""
    path = Path(project_root) / ".unity" / "forum" / "optimize-build.lock"
    identity = (str(path.resolve()), get_ident())
    if identity in _held_build_locks.get():
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        while True:
            check_cancelled()
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                event = _cancellation.get()
                event.wait(0.1) if event is not None else time.sleep(0.1)
        token = _held_build_locks.set((*_held_build_locks.get(), identity))
        try:
            yield
        finally:
            _held_build_locks.reset(token)
            fcntl.flock(handle, fcntl.LOCK_UN)


def _stream_output(proc, callback, input_text=None):
    """Deliver incremental stdout/stderr without blocking cancellation on a line."""
    if input_text is not None:
        proc.stdin.write(input_text)
        proc.stdin.close()
    chunks = {"stdout": [], "stderr": []}
    decoders = {name: codecs.getincrementaldecoder("utf-8")("replace") for name in chunks}
    with selectors.DefaultSelector() as selector:
        for name in chunks:
            selector.register(getattr(proc, name), selectors.EVENT_READ, name)
        while selector.get_map():
            check_cancelled()
            for key, _ in selector.select(timeout=0.1):
                payload = os.read(key.fileobj.fileno(), 65536)
                name = key.data
                text = decoders[name].decode(payload, final=not payload)
                if text:
                    chunks[name].append(text)
                    callback(name, text)
                if not payload:
                    selector.unregister(key.fileobj)
        while proc.poll() is None:
            check_cancelled()
            time.sleep(0.05)
    check_cancelled()
    return "".join(chunks["stdout"]), "".join(chunks["stderr"])


def run(
    project_root: Path,
    args: list[str],
    *,
    cwd: Path | None = None,
    owner: str = "Unity",
    task_id: str = "",
    serialize_build: bool = False,
    timings: dict | None = None,
    passthrough_stdio: bool = False,
    input: str | None = None,
    on_output=None,
) -> subprocess.CompletedProcess:
    """Run a registered job; interactive LSP keeps live stdio and its client's group."""
    if passthrough_stdio and serialize_build:
        raise ValueError("an interactive server must not hold the build lock")
    if passthrough_stdio and on_output is not None:
        raise ValueError("interactive stdio cannot also be captured as build events")
    if on_output is not None and input is not None:
        raise ValueError("streamed build events do not support a buffered stdin payload")
    # Internal checks must not re-enter the worker's Lake guard while
    # holding the same build lock.
    args = list(args)
    if args and args[0] in {"lake", "lean"}:
        # Controller-owned commands bypass worker shims using the configured
        # real executable. Optimize has no migration/setup receipt.
        key = "UNITY_REAL_LAKE" if args[0] == "lake" else "UNITY_REAL_LEAN"
        selected = os.environ.get(key, "").strip()
        if selected:
            executable = Path(selected)
            if (not executable.is_absolute() or not executable.is_file()
                    or not os.access(executable, os.X_OK)):
                raise ValueError("Invalid " + key + " executable")
            args[0] = selected
    project_root = Path(project_root).resolve()
    cwd = Path(cwd or project_root).resolve()
    job_id = uuid.uuid4().hex
    record_path = _jobs_dir(project_root) / f"{job_id}.json"

    @contextmanager
    def maybe_locked():
        queued = time.monotonic() if timings is not None else 0.0
        acquired = None
        try:
            with _build_lock(project_root) if serialize_build else nullcontext():
                if timings is not None:
                    acquired = time.monotonic()
                    timings["lock_wait_seconds"] = acquired - queued
                yield
        finally:
            if timings is not None:
                finished = time.monotonic()
                if acquired is None:
                    timings["lock_wait_seconds"] = finished - queued
                    timings["process_seconds"] = 0.0
                else:
                    timings["process_seconds"] = finished - acquired

    with maybe_locked():
        check_cancelled()
        # leanclient starts the Lake shim as a process-group leader, then kills
        # that group on close. Keep the real server inside it, not in a detached
        # group. Other callers still get a private, safely cancellable job group.
        client_group = (
            passthrough_stdio and os.name == "posix" and os.getpgrp() == os.getpid()
        )
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            stdin=subprocess.PIPE if input is not None else None,
            stdout=None if passthrough_stdio else subprocess.PIPE,
            stderr=None if passthrough_stdio else subprocess.PIPE,
            text=True,
            start_new_session=os.name == "posix" and not client_group,
        )
        record = {
            "job_id": job_id,
            "pid": proc.pid,
            "pgid": (os.getpgrp() if client_group else proc.pid) if os.name == "posix" else None,
            "owner": owner,
            "task_id": task_id,
            "command": args,
            "cwd": str(cwd),
            "started_at": time.time(),
        }
        if passthrough_stdio:
            record["passthrough_stdio"] = True
        try:
            temporary = record_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(record, sort_keys=True))
            os.replace(temporary, record_path)
            check_cancelled()  # Closes the cancellation-before-registration race.
            if on_output is not None:
                stdout, stderr = _stream_output(proc, on_output, input)
            elif _cancellation.get() is None:
                stdout, stderr = proc.communicate(input=input) if input is not None else proc.communicate()
            else:
                pending_input = input
                while True:
                    check_cancelled()
                    try:
                        stdout, stderr = proc.communicate(input=pending_input, timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        pending_input = None
                check_cancelled()
            return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
        except BaseException:
            _cancel_process(proc, client_group=client_group)
            raise
        finally:
            if on_output is not None:
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()
            record_path.unlink(missing_ok=True)
            record_path.with_suffix(".tmp").unlink(missing_ok=True)


def terminate(project_root: Path, *, owner: str | None = None) -> int:
    """Terminate registered jobs, optionally restricted to one worker owner."""
    directory = _jobs_dir(project_root)
    records: list[tuple[Path, dict]] = []
    for path in directory.glob("*.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            continue
        if owner is None or record.get("owner") == owner:
            # LSP clients may SIGKILL their whole group, including the shim
            # before its finally block runs. Prune that stale registration;
            # never signal a different group after a PID has been reused.
            if record.get("passthrough_stdio") and os.name == "posix":
                try:
                    live_group = os.getpgid(int(record["pid"]))
                except ProcessLookupError:
                    path.unlink(missing_ok=True)
                    continue
                if live_group != record.get("pgid"):
                    path.unlink(missing_ok=True)
                    continue
            records.append((path, record))

    for sig in (signal.SIGTERM, signal.SIGKILL):
        for path, record in records:
            # ``run`` removes the record after reaping the process. Avoid
            # signalling a rapidly reused PID during the hard-kill pass.
            if sig == signal.SIGKILL and not path.exists():
                continue
            pid = int(record.get("pid") or 0)
            if pid <= 0:
                continue
            try:
                if os.name == "posix" and record.get("pgid"):
                    os.killpg(int(record["pgid"]), sig)
                else:
                    os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        if sig == signal.SIGTERM and records:
            time.sleep(0.25)

    for path, _ in records:
        path.unlink(missing_ok=True)
    return len(records)
