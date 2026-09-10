"""Autoformalize-only write containment: Seatbelt or Landlock ABI >= 3.

The controller embeds its policy in launch argv through ``command`` and an
isolated Python bootstrap, never an unrestricted fallback.
Landlock is allow-only: shared targets must be outside writable roots.

This is not hostile-process isolation: reads, network, IPC and inherited file
descriptors remain available; Linux does not restrict chmod/chown/utime/xattr.
Use close_fds=True and expose trusted mutation services separately.
https://docs.kernel.org/userspace-api/landlock.html
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path


class SandboxUnavailable(RuntimeError):
    """The requested policy cannot be enforced; never run the child anyway."""


def _absolute(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"sandbox paths must be absolute: {value!r}")
    if "\x00" in str(path):
        raise ValueError("sandbox paths cannot contain NUL")
    return Path(os.path.abspath(path))


@dataclass(frozen=True)
class WriteSandboxPolicy:
    write_roots: tuple[Path, ...] = ()
    protected_paths: tuple[Path, ...] = ()

    def __post_init__(self):
        roots = tuple(dict.fromkeys(_absolute(p).resolve(strict=True) for p in self.write_roots))
        for root in roots:
            if root == Path(root.anchor) or not root.is_dir():
                raise ValueError(f"writable root must be an existing non-root directory: {root}")
        protected = tuple(dict.fromkeys(_absolute(p) for p in self.protected_paths))
        object.__setattr__(self, "write_roots", roots)
        object.__setattr__(self, "protected_paths", protected)

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "write_roots": [str(p) for p in self.write_roots],
            "protected_paths": [str(p) for p in self.protected_paths],
        }

    @classmethod
    def from_dict(cls, value: dict) -> WriteSandboxPolicy:
        if not isinstance(value, dict) or set(value) != {"version", "write_roots", "protected_paths"}:
            raise ValueError("invalid sandbox policy fields")
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError("unsupported sandbox policy version")
        for key in ("write_roots", "protected_paths"):
            if not isinstance(value[key], list) or not all(isinstance(p, str) for p in value[key]):
                raise ValueError(f"{key} must be a list of absolute paths")
        return cls(tuple(value["write_roots"]), tuple(value["protected_paths"]))


def _protected_forms(policy: WriteSandboxPolicy) -> tuple[Path, ...]:
    # Protect both a symlink object and its actual shared destination on macOS.
    return tuple(dict.fromkeys(
        form for path in policy.protected_paths
        for form in (path, path.resolve(strict=False))
    ))


def _seatbelt_profile(policy: WriteSandboxPolicy) -> str:
    def quoted(path: Path) -> str:
        # SBPL strings use backslash escaping. JSON escaping covers quotes,
        # backslashes and control characters without allowing policy injection.
        return json.dumps(str(path), ensure_ascii=False)

    roots = [f"(subpath {quoted(p)})" for p in policy.write_roots]
    # /dev/null is a discard sink, not permission to write general /dev files.
    roots.append('(literal "/dev/null")')
    permitted = "(require-any " + " ".join(roots) + ")"
    protected = _protected_forms(policy)
    if protected:
        excluded = "(require-any " + " ".join(f"(subpath {quoted(p)})" for p in protected) + ")"
        permitted = f"(require-all {permitted} (require-not {excluded}))"
    return (
        "(version 1)\n(allow default)\n(deny file-write*)\n"
        f"(allow file-write* {permitted})\n"
    )


def _linux_validate_policy(policy: WriteSandboxPolicy) -> None:
    for protected in _protected_forms(policy):
        for root in policy.write_roots:
            if protected.is_relative_to(root) or root.is_relative_to(protected):
                raise SandboxUnavailable(
                    "Landlock cannot exclude protected paths overlapping a writable root; "
                    f"use separate real shared targets: {protected} / {root}"
                )


def _linux_libc():
    # Linux generic syscall numbering matches these supported 64-bit ABIs.
    # Do not guess syscall numbers on a different architecture.
    if platform.machine().lower() not in {"x86_64", "amd64", "aarch64", "arm64", "riscv64"}:
        raise SandboxUnavailable("unsupported Linux architecture for Landlock syscalls")
    return ctypes.CDLL(None, use_errno=True)


def _syscall(libc, number: int, *args) -> int:
    result = libc.syscall(ctypes.c_long(number), *args)
    if result < 0:
        error = ctypes.get_errno()
        raise SandboxUnavailable(f"Landlock syscall {number} failed: {os.strerror(error)}")
    return int(result)


def _linux_abi(libc) -> int:
    abi = _syscall(libc, 444, ctypes.c_void_p(), ctypes.c_size_t(0), ctypes.c_uint(1))
    if abi < 3:
        raise SandboxUnavailable("Landlock ABI >= 3 is required to deny file truncation")
    return abi


def check_support(policy: WriteSandboxPolicy):
    """Read-only capability probe; return libc on Linux for the runner."""
    if sys.platform == "darwin":
        if not os.access("/usr/bin/sandbox-exec", os.X_OK):
            raise SandboxUnavailable("macOS sandbox-exec is unavailable")
    elif sys.platform == "linux":
        _linux_validate_policy(policy)
        libc = _linux_libc()
        _linux_abi(libc)
        return libc
    else:
        raise SandboxUnavailable(f"write sandbox is unsupported on {sys.platform}")


def command(policy: WriteSandboxPolicy, argv: list[str]) -> list[str]:
    """Return a launch argv after validating policy and local OS support."""
    if (not argv or not all(isinstance(arg, str) and "\x00" not in arg for arg in argv)
            or not argv[0] or argv[0].startswith("-")):
        raise ValueError("sandbox command requires a nonempty executable argv")
    payload = json.dumps(policy.to_dict())
    if len(payload) > 65536:
        raise ValueError("sandbox policy exceeds 64 KiB")
    check_support(policy)
    # This interpreter starts before containment. Never import the runner from
    # the child's writable cwd or PYTHONPATH; require Unity to be installed in
    # the controller's trusted interpreter environment.
    return [sys.executable, "-I", "-B", "-m", "unity.autoformalize_sandbox",
            "--policy-json", payload, "--", *argv]


def _landlock_restrict(policy: WriteSandboxPolicy, libc) -> None:
    """Restrict this single-threaded runner, then its exec/fork descendants."""
    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]

    class PathBeneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    write_file = 1 << 1
    truncate = 1 << 14
    # Remove/make directory entries (bits 4..12), cross-directory refer (13),
    # and truncate (14). Read and execute rights deliberately remain unhandled.
    rights = write_file | sum(1 << bit for bit in range(4, 15))
    attr = Ruleset(rights)
    ruleset_fd = _syscall(libc, 444, ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint(0))
    try:
        for path, allowed in [*((root, rights) for root in policy.write_roots),
                              (Path("/dev/null"), write_file | truncate)]:
            path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathBeneath(allowed, path_fd)
                _syscall(libc, 445, ctypes.c_int(ruleset_fd), ctypes.c_int(1), ctypes.byref(rule), ctypes.c_uint(0))
            finally:
                os.close(path_fd)
        if libc.prctl(ctypes.c_int(38), ctypes.c_ulong(1), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)):
            raise SandboxUnavailable(f"PR_SET_NO_NEW_PRIVS failed: {os.strerror(ctypes.get_errno())}")
        _syscall(libc, 446, ctypes.c_int(ruleset_fd), ctypes.c_uint(0))
    finally:
        os.close(ruleset_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-json", required=True)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    child = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not child or not child[0] or child[0].startswith("-"):
        parser.error("a child executable argv is required after --")
    try:
        if len(args.policy_json) > 65536:
            raise ValueError("sandbox policy exceeds 64 KiB")
        policy = WriteSandboxPolicy.from_dict(json.loads(args.policy_json))
        libc = check_support(policy)
        if sys.platform == "darwin":
            os.execv("/usr/bin/sandbox-exec", ["sandbox-exec", "-p", _seatbelt_profile(policy), "--", *child])
        else:
            _landlock_restrict(policy, libc)
            os.execvp(child[0], child)
    except (OSError, ValueError, SandboxUnavailable) as exc:
        print(f"autoformalize write sandbox refused to launch: {exc}", file=sys.stderr)
        return 125
    return 125  # exec must not return; never fall back to an unrestricted child.


if __name__ == "__main__":
    raise SystemExit(main())
