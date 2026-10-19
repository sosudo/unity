"""Read-only restart closure checks for compiler-driven Optimize owned jobs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def assert_closed(root: Path) -> None:
    """Read-only conservative registry check; never kill a saved PID on resume.

    Old registrations do not contain sufficient birth identity to authorize a
    signal. Any currently existing PID/group is therefore a blocker, even if it
    may be unrelated PID reuse. Dead records remain as recoverable evidence.
    """
    directory = Path(root) / ".unity" / "jobs" / "optimize"
    receipts = Path(root) / ".unity" / "forum" / "optimize" / "processes"
    records = [(path, False) for path in directory.glob("*.json")]
    records.extend((path, True) for path in receipts.glob("*.json") if not path.name.endswith(".closed.json"))
    for path, worker_receipt in records:
        if path.is_symlink(): raise ValueError("Optimize job registry contains a redirected record")
        try:
            content = path.read_bytes()
            record = json.loads(content)
            pid = int(record["pid"])
            pgid = int(record.get("pgid") or 0)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError("cannot verify saved Optimize job closure: " + path.name) from exc
        if pid <= 0 or pgid < 0: raise ValueError("invalid Optimize process registration")
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
        for target, group in ((pid, False), (pgid, True)):
            if not target or (group and os.name != "posix"): continue
            try:
                (os.killpg if group else os.kill)(target, 0)
            except ProcessLookupError:
                continue
            except PermissionError as exc:
                raise ValueError("cannot verify saved Optimize process closure; no signal sent") from exc
            raise ValueError("a registered Optimize process or process group still exists; verify closure before resuming")
