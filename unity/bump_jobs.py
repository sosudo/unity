"""Owned subprocesses for the ``unity bump`` control plane.

Long-running deterministic checks must outlive neither their bump runtime nor
their cancellation request.  This registry intentionally covers Unity-owned
jobs only; model shell commands are not treated as authoritative checks.
"""

from __future__ import annotations

import fcntl
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path
from threading import Event


class JobCancelled(ValueError):
    """A controller check stopped cooperatively; callers must roll back main."""


class JobIdentityError(ValueError):
    """Saved live jobs cannot be safely attributed; continuation must stop."""


def _process_identity(pid: int) -> dict | None:
    """Read native process birth identity, never command text or environments.

    None means unavailable, not permission to signal. Linux boot identity makes
    start ticks safe across reboots; Darwin supplies microsecond birth time via
    its documented proc_bsdinfo struct (not ps's one-second lstart display).
    """
    if type(pid) is not int or pid <= 0:
        return None
    try:
        if sys.platform.startswith("linux"):
            data = Path(f"/proc/{pid}/stat").read_text()
            fields = data[data.rfind(")") + 2:].split()
            ticks = int(fields[19])  # stat field 22; first post-comm field is 3.
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            if ticks <= 0 or not boot:
                return None
            return {"platform": "linux", "pid": pid, "startticks": ticks, "boot_id": boot}
        if sys.platform == "darwin":
            class BSDInfo(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint32) for name in (
                    "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid",
                    "svuid", "svgid", "reserved")] + [
                    ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
                    *[(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "pjobc", "tdev", "tpgid")],
                    ("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]
            lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            read = lib.proc_pidinfo
            read.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
            read.restype = ctypes.c_int
            value = BSDInfo()
            count = read(pid, 3, 0, ctypes.byref(value), ctypes.sizeof(value))
            if count != ctypes.sizeof(value) or value.pid != pid or not value.start_sec:
                return None
            return {"platform": "darwin", "pid": pid, "start_sec": value.start_sec,
                    "start_usec": value.start_usec, "uid": value.uid}
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    return None


def _pid_present(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_running(group: int) -> bool:
    """Bounded read-only confirmation; zombies are not executing descendants."""
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        result = subprocess.run(["ps", "-axo", "pid=,pgid=,stat="], capture_output=True,
                                text=True, timeout=2)
        if result.returncode:
            return True
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 3:
                return True
            if int(fields[1]) == group and not fields[2].startswith("Z"):
                return True
        return False
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return True


def _discard_dead_job(path: Path, record: dict) -> None:
    group = record.get("pgid")
    if os.name == "posix" and type(group) is int and group > 0 and _group_running(group):
        raise JobIdentityError(f"Bump group {group} remains live without its registered child; ownership must be inspected before continuing")
    path.unlink(missing_ok=True)


def _registered_job_live(record: dict) -> bool:
    """Fresh proof of exact child AND group ownership before each signal."""
    pid = record.get("pid")
    if type(pid) is not int or pid <= 0:
        raise JobIdentityError("Bump job record has no valid process identity; refusing to signal it")
    current = _process_identity(pid)
    if current is None and not _pid_present(pid):
        return False
    if not isinstance(record.get("process_identity"), dict) or current != record["process_identity"]:
        raise JobIdentityError(f"Bump job PID {pid} is live but its saved birth identity is missing, changed, or unverifiable; refusing to signal it")
    if os.name == "posix":
        group = record.get("pgid")
        if type(group) is not int or group <= 0:
            raise JobIdentityError(f"Bump job PID {pid} has no verified process group; refusing to signal it")
        try:
            actual = os.getpgid(pid)
        except ProcessLookupError:
            return False
        if actual != group:
            raise JobIdentityError(f"Bump job PID {pid} changed process group; refusing to signal it")
        leader = _process_identity(group)
        if not isinstance(record.get("group_identity"), dict) or leader != record["group_identity"]:
            raise JobIdentityError(f"Bump job group {group} has a missing, changed, or unverifiable leader birth identity; refusing to signal it")
    return True


_cancellation: ContextVar[Event | None] = ContextVar("bump_job_cancellation", default=None)


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
        raise JobCancelled("bump verification cancelled")


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
    path = Path(project_root) / ".unity" / "jobs" / "bump"
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _build_lock(project_root: Path):
    """Serialize authoritative bump builds across controller processes."""
    path = Path(project_root) / ".unity" / "forum" / "bump-build.lock"
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
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


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
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    output_stream=None,
) -> subprocess.CompletedProcess:
    """Run a registered job; interactive LSP keeps live stdio and its client's group."""
    if passthrough_stdio and serialize_build:
        raise ValueError("an interactive server must not hold the build lock")
    if output_stream is not None and passthrough_stdio:
        raise ValueError("file-backed output cannot also be interactive")
    # Internal checks must not re-enter the worker's Lake guard while
    # holding the same build lock.
    args = list(args)
    if args and args[0] == "lake":
        real_lake = os.environ.get("UNITY_REAL_LAKE", "").strip()
        if real_lake:
            executable = Path(real_lake)
            if (
                not executable.is_absolute()
                or not executable.is_file()
                or not os.access(executable, os.X_OK)
            ):
                raise ValueError("Invalid UNITY_REAL_LAKE executable")
            args[0] = real_lake
    project_root = Path(project_root).resolve()
    cwd = Path(cwd or project_root).resolve()
    child_env = dict(os.environ if env is None else env)
    for key in ("LEAN_PATH", "LEAN_SRC_PATH", "LEAN_SYSROOT", "LAKE_HOME", "LAKE_PACKAGES_DIR"):
        child_env.pop(key, None)
    if (cwd / "lean-toolchain").is_file():
        child_env["ELAN_TOOLCHAIN"] = (cwd / "lean-toolchain").read_text().strip()
    if env is None:
        # Workers explicitly pass their run/author environment. Controller
        # calls must not inherit a global cache from the invoking shell.
        child_env["LAKE_CACHE_DIR"] = str(cwd / ".unity" / "bump-cache" / "lake")
        child_env["XDG_CACHE_HOME"] = str(cwd / ".unity" / "bump-cache" / "xdg")
    if timeout is not None and timeout <= 0:
        raise ValueError("job timeout must be positive")
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
            env=child_env,
            stdin=subprocess.PIPE if input is not None else None,
            stdout=output_stream if output_stream is not None else (None if passthrough_stdio else subprocess.PIPE),
            stderr=subprocess.STDOUT if output_stream is not None else (None if passthrough_stdio else subprocess.PIPE),
            text=True,
            errors="replace",
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
            "process_identity": _process_identity(proc.pid),
            "group_identity": _process_identity(os.getpgrp() if client_group else proc.pid)
                              if os.name == "posix" else None,
        }
        if passthrough_stdio:
            record["passthrough_stdio"] = True
        try:
            temporary = record_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(record, sort_keys=True))
            os.replace(temporary, record_path)
            check_cancelled()  # Closes the cancellation-before-registration race.
            if _cancellation.get() is None and timeout is None:
                stdout, stderr = proc.communicate(input=input) if input is not None else proc.communicate()
            else:
                pending_input = input
                deadline = time.monotonic() + timeout if timeout is not None else None
                while True:
                    check_cancelled()
                    if deadline is not None and time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(args, timeout)
                    try:
                        stdout, stderr = proc.communicate(input=pending_input, timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        pending_input = None
                check_cancelled()
            return subprocess.CompletedProcess(args, proc.returncode, stdout or "", stderr or "")
        except BaseException:
            _cancel_process(proc, client_group=client_group)
            raise
        finally:
            record_path.unlink(missing_ok=True)
            record_path.with_suffix(".tmp").unlink(missing_ok=True)


def terminate(project_root: Path, *, owner: str | None = None) -> int:
    """Signal only freshly birth-verified jobs; retain ambiguous live evidence."""
    directory = _jobs_dir(project_root)
    records: list[tuple[Path, dict]] = []
    for path in directory.glob("*.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise JobIdentityError("Bump job registration is unreadable; preserve it and inspect ownership before continuing") from exc
        if not isinstance(record, dict):
            raise JobIdentityError("Bump job registration is malformed; no process was signaled")
        if owner is None or record.get("owner") == owner:
            if not _registered_job_live(record):
                # A dead group leader can leave descendants; never infer their
                # ownership from a recycled numeric PGID after a restart.
                _discard_dead_job(path, record)
                continue
            records.append((path, record))

    for sig in (signal.SIGTERM, signal.SIGKILL):
        for path, record in records:
            # ``run`` removes the record after reaping the process. Avoid
            # signalling a rapidly reused PID during the hard-kill pass.
            if sig == signal.SIGKILL and not path.exists():
                continue
            if not path.exists():
                continue
            if not _registered_job_live(record):
                _discard_dead_job(path, record)
                continue
            pid = record["pid"]
            try:
                if os.name == "posix" and record.get("pgid"):
                    os.killpg(int(record["pgid"]), sig)
                else:
                    os.kill(pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise JobIdentityError(f"Permission denied stopping verified Bump PID {pid}; job registration retained") from exc
        if sig == signal.SIGTERM and records:
            time.sleep(0.25)

    # A sent signal is not evidence of exit. Even if the owner removed the
    # record, verify no running member of that owned group remains before a
    # resumed controller may dispatch replacement workers.
    deadline = time.monotonic() + 1.0
    while True:
        remaining = [(path, row) for path, row in records
                     if (_group_running(row["pgid"]) if os.name == "posix"
                         else _pid_present(row["pid"]))]
        if not remaining:
            for path, _ in records:
                path.unlink(missing_ok=True)
            break
        if time.monotonic() >= deadline:
            for path, row in remaining:
                if not path.exists():
                    path.write_text(json.dumps(row, sort_keys=True))
            raise JobIdentityError("Bump cancellation could not confirm all owned jobs/groups stopped; registrations retained and continuation blocked")
        time.sleep(0.05)
    return len(records)
