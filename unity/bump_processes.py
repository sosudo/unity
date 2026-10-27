"""Read-only restart closure checks for compiler-driven Bump owned jobs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def _closed_host_worker_session(receipts: Path, token: str, namespace: int, boot: str) -> bool:
    """Bind a worker token to every exact host birth and its closure."""
    matches = []
    for path in receipts.glob("*.json"):
        if path.name.endswith(".closed.json"):
            continue
        if path.is_symlink():
            return False
        try:
            data = path.read_bytes()
            row = json.loads(data)
        except (OSError, ValueError):
            return False
        if row.get("worker_session") != token:
            continue
        closure_path = path.with_suffix(".closed.json")
        if closure_path.is_symlink():
            return False
        try:
            closure = json.loads(closure_path.read_text())
        except (OSError, ValueError):
            return False
        pgid = row.get("pgid")
        matches.append(row.get("pid_namespace") == namespace and row.get("boot_id") == boot
                       and type(pgid) is int and pgid > 1 and row.get("pid") == pgid == row.get("sid")
                       and bool(row.get("start_ticks") or row.get("birth"))
                       and closure.get("receipt_sha256") == hashlib.sha256(data).hexdigest()
                       and closure.get("pgid") == pgid and closure.get("group_empty") is True)
    return bool(matches) and all(matches)


def _no_nested_pid_namespaces(proc: Path = Path("/proc")) -> bool:
    """Conservative proof: full proc view contains no descendant PID namespace.

    An extant descendant namespace has a visible init; init death kills its
    namespace members. Any nested process or visibility uncertainty blocks.
    """
    try:
        if os.readlink(proc / "self") != str(os.getpid()):
            return False
        mount = []
        for line in (proc / "self/mountinfo").read_text().splitlines():
            left, right = line.split(" - ", 1)
            fields, fs = left.split(), right.split()
            if len(fields) >= 6 and len(fs) >= 3 and fields[4] == "/proc":
                mount.append((fields, fs))
        if len(mount) != 1:
            return False
        fields, fs = mount[0]
        options = set(fields[5].split(",")) | set(fs[2].split(","))
        if fields[3] != "/" or fs[0] != "proc" or "subset=pid" in options or any(
                option.startswith("hidepid=") and option != "hidepid=0" for option in options):
            return False
        self_pid = str(os.getpid())
        for entry in proc.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                status = (entry / "status").read_text()
            except FileNotFoundError:
                if entry.exists():
                    return False  # A visible PID without status is indeterminate.
                continue  # An exited task cannot create another namespace.
            lines = [line for line in status.splitlines() if line.startswith("NSpid:")]
            if len(lines) != 1:
                return False
            chain = lines[0].split()[1:]
            if len(chain) != 1 or chain[0] != entry.name:
                return False
            if entry.name == self_pid and chain != [self_pid]:
                return False
        return (proc / self_pid / "status").is_file()
    except (OSError, ValueError, IndexError):
        return False


def assert_closed(root: Path) -> None:
    """Read-only conservative registry check; never kill a saved PID on resume.

    Legacy and foreign-namespace registrations lack sufficient host closure
    evidence and remain blockers for explicit audit; no saved PID is signalled.
    Matching local identities and surviving groups are also blockers.
    """
    directory = Path(root) / ".unity" / "jobs" / "bump"
    receipts = Path(root) / ".unity" / "forum" / "bump" / "processes"
    records = [(path, False) for path in directory.glob("*.json")]
    records.extend((path, True) for path in receipts.glob("*.json") if not path.name.endswith(".closed.json"))
    for path, worker_receipt in records:
        if path.is_symlink(): raise ValueError("Bump job registry contains a redirected record")
        try:
            content = path.read_bytes()
            record = json.loads(content)
            pid = int(record["pid"])
            pgid = int(record.get("pgid") or 0)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError("cannot verify saved Bump job closure: " + path.name) from exc
        if pid <= 0 or pgid < 0: raise ValueError("invalid Bump process registration")
        if worker_receipt:
            if record.get("sid") != pgid or pgid != pid or not (record.get("start_ticks") or record.get("birth")):
                raise ValueError("worker receipt lacks complete process birth/session identity")
            closure_path = path.with_suffix(".closed.json")
            if closure_path.exists():
                if closure_path.is_symlink(): raise ValueError("worker closure receipt is redirected")
                closure = json.loads(closure_path.read_text())
                if (closure.get("receipt_sha256") != hashlib.sha256(content).hexdigest()
                        or closure.get("pgid") != pgid or closure.get("group_empty") is not True):
                    raise ValueError("worker closure receipt does not bind its exact birth receipt")
                continue
        else:
            from . import bump_jobs
            if (type(record.get("pid_namespace")) is not int or record["pid_namespace"] <= 0
                    or type(record.get("start_ticks")) is not int or record["start_ticks"] <= 0
                    or not isinstance(record.get("boot_id"), str) or not record["boot_id"] or pgid <= 0):
                raise ValueError("legacy Bump job lacks namespace and birth identity; audit required")
            try:
                local_namespace = os.stat("/proc/self/ns/pid").st_ino
                local_boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            except OSError as exc:
                raise ValueError("cannot verify Bump PID namespace") from exc
            if record["pid_namespace"] != local_namespace or record["boot_id"] != local_boot:
                token = record.get("worker_session")
                if (record["boot_id"] == local_boot and record["pid_namespace"] != local_namespace
                        and isinstance(token, str) and token
                        and _closed_host_worker_session(receipts, token, local_namespace, local_boot)
                        and _no_nested_pid_namespaces()):
                    continue
                raise ValueError("foreign Bump job needs verified namespace closure; audit required")
            identity = bump_jobs.process_identity(pid)
            if identity is None and Path(f"/proc/{pid}").exists():
                raise ValueError("cannot verify saved Bump process birth")
            if identity and all(record.get(key) == value for key, value in identity.items()):
                raise ValueError("a registered Bump process still exists; verify closure before resuming")
            # PID absent or reused: it is never a signal target. A surviving
            # process group is still a blocker regardless of its provenance.
            pid = 0
        for target, group in ((pid, False), (pgid, True)):
            if not target or (group and os.name != "posix"): continue
            try:
                (os.killpg if group else os.kill)(target, 0)
            except ProcessLookupError:
                continue
            except PermissionError as exc:
                raise ValueError("cannot verify saved Bump process closure; no signal sent") from exc
            raise ValueError("a registered Bump process or process group still exists; verify closure before resuming")
