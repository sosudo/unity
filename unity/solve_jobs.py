"""Owned subprocesses for the ``unity solve`` control plane.

Long-running deterministic checks must outlive neither their solve runtime nor
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
from contextlib import contextmanager
from pathlib import Path


def _jobs_dir(project_root: Path) -> Path:
    path = Path(project_root) / ".unity" / "jobs" / "solve"
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _build_lock(project_root: Path):
    """Serialize authoritative solve builds across controller processes."""
    path = Path(project_root) / ".unity" / "forum" / "solve-build.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
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
) -> subprocess.CompletedProcess:
    """Run and register one deterministic solve job in its own process group."""
    project_root = Path(project_root).resolve()
    cwd = Path(cwd or project_root).resolve()
    job_id = uuid.uuid4().hex
    record_path = _jobs_dir(project_root) / f"{job_id}.json"

    @contextmanager
    def maybe_locked():
        if serialize_build:
            with _build_lock(project_root):
                yield
        else:
            yield

    with maybe_locked():
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=os.name == "posix",
        )
        record = {
            "job_id": job_id,
            "pid": proc.pid,
            "pgid": proc.pid if os.name == "posix" else None,
            "owner": owner,
            "task_id": task_id,
            "command": args,
            "cwd": str(cwd),
            "started_at": time.time(),
        }
        temporary = record_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, sort_keys=True))
        os.replace(temporary, record_path)
        try:
            stdout, stderr = proc.communicate()
            return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
        finally:
            record_path.unlink(missing_ok=True)


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
