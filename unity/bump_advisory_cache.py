"""Optional private cache copies for advisory Bump workers, never validation.

The deadline is cooperative: kernel I/O can block beyond it. The caller must
join this operation before launching a worker or releasing its ownership.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path
import shutil
import stat
import subprocess
from threading import Event
import time
import uuid

from . import bump_tasks
from .bump_spawn import SpawnCleanupError

_MAX_BYTES = 256 * 1024 * 1024
_MAX_ENTRIES = 10000
_SECONDS = 3.0
_CHUNK = 1024 * 1024


class _Skip(Exception):
    pass


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args], cwd=root,
                            capture_output=True, text=True, timeout=1.0, check=False)
    if result.returncode:
        raise _Skip()
    return result.stdout.strip()


def _state_identity(paths, expected_base: str, expected_config: dict) -> bool:
    state = bump_tasks.load(paths.forum)
    return (state.get('main_sha') == expected_base and state.get('config') == expected_config
            and _git(paths.project_root, 'rev-parse', 'HEAD') == expected_base
            and not _git(paths.project_root, 'status', '--porcelain', '--untracked-files=all'))


def _snapshot(paths, tree: Path, expected_base: str, expected_config: dict, cancel: Event) -> bool:
    """Copy while holding the nonblocking Bump build lock; false means cold fallback."""
    deadline = time.monotonic() + _SECONDS
    stage = None
    dest_fd = None
    reservation = None
    lock = None
    def check():
        if cancel.is_set() or time.monotonic() >= deadline or (paths.unity / 'stop-requested').exists():
            raise _Skip()
    def copy_dir(src_fd: int, dst_fd: int):
        before = os.fstat(src_fd)
        entries = []
        with os.scandir(src_fd) as it:
            for entry in it:
                check()
                count[0] += 1
                if count[0] > _MAX_ENTRIES: raise _Skip()
                entries.append(entry.name)
        entries.sort()
        for name in entries:
            check()
            st = os.stat(name, dir_fd=src_fd, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=src_fd)
                try:
                    opened = os.fstat(fd)
                    if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino): raise _Skip()
                    os.mkdir(name, mode=0o700, dir_fd=dst_fd)
                    child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dst_fd)
                    try:
                        copy_dir(fd, child_fd)
                    finally:
                        os.close(child_fd)
                finally:
                    os.close(fd)
            elif stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                if st.st_size > _MAX_BYTES - size[0]: raise _Skip()
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=src_fd)
                try:
                    opened = os.fstat(fd)
                    if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                            st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns): raise _Skip()
                    output_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                        0o600, dir_fd=dst_fd)
                    with os.fdopen(output_fd, 'wb') as out:
                        os.fchmod(out.fileno(), stat.S_IMODE(opened.st_mode) & 0o777)
                        while True:
                            check()
                            data = os.read(fd, _CHUNK)
                            if not data: break
                            size[0] += len(data)
                            if size[0] > _MAX_BYTES: raise _Skip()
                            out.write(data)
                    ended = os.fstat(fd)
                    if (ended.st_size, ended.st_mtime_ns, ended.st_ctime_ns) != (
                            opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns): raise _Skip()
                    os.utime(name, ns=(opened.st_atime_ns, opened.st_mtime_ns),
                             dir_fd=dst_fd, follow_symlinks=False)
                finally:
                    os.close(fd)
            else:
                raise _Skip()
        after = os.fstat(src_fd)
        if (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns): raise _Skip()
    try:
        check()
        root = paths.project_root.resolve()
        tree = Path(tree).resolve()
        if tree == root or not tree.is_relative_to(root / '.worktrees'):
            raise _Skip()
        build_dir = (expected_config.get('scope') or {}).get('build_dir', '.lake/build')
        # Only the standard private output layout is supported; all other setups stay cold.
        if build_dir != '.lake/build': raise _Skip()
        lake_dir = tree / '.lake'
        if lake_dir.is_symlink() or not lake_dir.is_dir(): raise _Skip()
        dest_fd = os.open(lake_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.stat('build', dir_fd=dest_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise _Skip()
        lock_path = paths.unity / 'forum' / 'bump-build.lock'
        lock = lock_path.open('a+')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _Skip()
        if not _state_identity(paths, expected_base, expected_config): raise _Skip()
        if _git(tree, 'rev-parse', 'HEAD') != expected_base or _git(tree, 'status', '--porcelain', '--untracked-files=all'):
            raise _Skip()
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            lake_fd = os.open('.lake', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                src = os.open('build', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=lake_fd)
            finally:
                os.close(lake_fd)
        finally:
            os.close(root_fd)
        try:
            staged = '.advisory-seed-' + uuid.uuid4().hex
            os.mkdir(staged, mode=0o700, dir_fd=dest_fd)
            stage = staged
            stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dest_fd)
            try:
                size, count = [0], [0]
                copy_dir(src, stage_fd)
            finally:
                os.close(stage_fd)
        finally:
            os.close(src)
        check()
        if not _state_identity(paths, expected_base, expected_config): raise _Skip()
        if _git(tree, 'rev-parse', 'HEAD') != expected_base: raise _Skip()
        check()
        # Ensure the opened .lake is still the worktree's directory. Reserve
        # the fresh destination atomically before replacing our empty directory.
        expected_lake = os.fstat(dest_fd)
        current_lake = os.stat(lake_dir, follow_symlinks=False)
        if (expected_lake.st_dev, expected_lake.st_ino) != (current_lake.st_dev, current_lake.st_ino): raise _Skip()
        os.mkdir('build', mode=0o700, dir_fd=dest_fd)
        reservation = os.stat('build', dir_fd=dest_fd, follow_symlinks=False)
        os.rename(stage, 'build', src_dir_fd=dest_fd, dst_dir_fd=dest_fd)
        stage = None
        reservation = None
        return True
    except Exception:
        return False
    finally:
        try:
            if stage is not None:
                try:
                    shutil.rmtree(stage, dir_fd=dest_fd)
                except OSError as exc:
                    raise SpawnCleanupError("advisory cache staging cleanup failed") from exc
            if reservation is not None:
                try:
                    current = os.stat('build', dir_fd=dest_fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (reservation.st_dev, reservation.st_ino):
                        raise SpawnCleanupError("advisory cache destination reservation changed")
                    os.rmdir('build', dir_fd=dest_fd)
                except OSError as exc:
                    raise SpawnCleanupError("advisory cache destination cleanup failed") from exc
        finally:
            if lock is not None:
                lock.close()
            if dest_fd is not None:
                os.close(dest_fd)


async def seed(paths, tree: Path, base: str, config: dict, interrupt: asyncio.Event) -> bool:
    """Join the owned copy on cancellation; never leave a detached writer."""
    cancel = Event()
    job = asyncio.create_task(asyncio.to_thread(_snapshot, paths, tree, base, config, cancel))
    monitor = asyncio.create_task(interrupt.wait())
    interrupted = False
    try:
        while not job.done():
            try:
                done, _ = await asyncio.wait({job, monitor}, return_when=asyncio.FIRST_COMPLETED)
                if monitor in done:
                    cancel.set()
                    break
            except asyncio.CancelledError:
                interrupted = True
                cancel.set()
                break
        while not job.done():
            try:
                await asyncio.shield(job)
            except asyncio.CancelledError:
                interrupted = True
                cancel.set()
        result = job.result()
        if interrupted: raise asyncio.CancelledError
        return result
    finally:
        monitor.cancel()
