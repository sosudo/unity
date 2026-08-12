"""Authoritative, run-scoped state and deterministic checks for ``unity prove``.

The Forum remains the human-facing conversation surface.  This module is the
control-plane underneath it: a small transactional proof-search graph shared by
the scheduler and every worktree.  State is compact and current; the append-only
event log is telemetry and is never injected into model context.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = 1
TASK_KINDS = {
    "library_search", "api_search", "proof_attempt", "formalization", "debugging",
    "decomposition", "verification", "review", "counterexample_search",
    "dependency_resolution",
}
TASK_TERMINAL = {"complete", "cancelled", "superseded", "dominated", "failed"}
SPECULATIVE_KINDS = {
    "library_search", "api_search", "proof_attempt", "formalization", "debugging",
    "decomposition", "counterexample_search", "dependency_resolution",
}
FORBIDDEN_DEFAULT = ("sorry", "admit", "native_decide")

_DECL_RE = re.compile(
    r"(?m)^[ \t]*(?P<kind>theorem|lemma|def|abbrev|opaque|instance|axiom)"
    r"[ \t]+(?P<name>[A-Za-z_][\w.']*)"
)
_AXIOM_RE = re.compile(r"(?m)^[ \t]*axiom[ \t]+([A-Za-z_][\w.']*)")
_SCOPE_RE = re.compile(r"(?m)^[ \t]*(?P<cmd>namespace|section|end)(?:[ \t]+(?P<name>[A-Za-z_][\w.']*))?[ \t]*$")


def _now() -> float:
    return time.time()


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _mask_lean_comments(text: str) -> str:
    """Blank Lean line/block comments while preserving offsets and newlines."""
    out = list(text)
    i, depth, string = 0, 0, False
    while i < len(text):
        if depth:
            if text.startswith("/-", i):
                out[i:i + 2] = "  "
                depth += 1
                i += 2
            elif text.startswith("-/", i):
                out[i:i + 2] = "  "
                depth -= 1
                i += 2
            else:
                if text[i] != "\n":
                    out[i] = " "
                i += 1
        elif string:
            if text[i] == "\\":
                out[i] = " "
                if i + 1 < len(text) and text[i + 1] != "\n":
                    out[i + 1] = " "
                i += 2
            elif text[i] == '"':
                string = False
                i += 1
            else:
                if text[i] != "\n":
                    out[i] = " "
                i += 1
        elif text.startswith("--", i):
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            out[i:end] = " " * (end - i)
            i = end
        elif text.startswith("/-", i):
            out[i:i + 2] = "  "
            depth = 1
            i += 2
        elif text[i] == '"':
            string = True
            i += 1
        else:
            i += 1
    return "".join(out)


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(value, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _git(root: Path, *args: str, check: bool = True, timeout: float = 30) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, timeout=timeout,
    )
    if check and proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout or "git failed").strip())
    return proc.stdout.strip()


def _git_blob(root: Path, commit: str, path: str) -> bytes:
    proc = subprocess.run(
        ["git", "show", f"{commit}:{path}"], cwd=root, capture_output=True, timeout=30,
    )
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout or b"git show failed").decode(errors="replace").strip())
    return proc.stdout


def active_run_dir(unity_dir: Path) -> Path | None:
    """Resolve the active shared run, ignoring stale or malformed pointers."""
    pointer = Path(unity_dir) / "current-run.json"
    try:
        raw = json.loads(pointer.read_text())
        path = Path(raw["path"]).resolve()
        if path.is_dir() and path.parent == (Path(unity_dir) / "runs").resolve():
            return path
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        pass
    return None


def split_declarations(text: str) -> list[dict[str, Any]]:
    """Return named top-level-ish Lean declaration blocks.

    This is deliberately a conservative source locator, not a Lean parser.  The
    verifier additionally compiles the immutable commit.  Declaration boundaries
    are used for target discovery, signature preservation and forbidden-token
    checks; false positives fail closed rather than making a candidate trusted.
    """
    masked = _mask_lean_comments(text)
    matches = list(_DECL_RE.finditer(masked))
    scopes: list[tuple[str, str]] = []
    scope_events = list(_SCOPE_RE.finditer(masked))
    event_i = 0
    out: list[dict[str, Any]] = []
    for i, match in enumerate(matches):
        while event_i < len(scope_events) and scope_events[event_i].start() < match.start():
            event = scope_events[event_i]
            cmd, name = event.group("cmd"), event.group("name") or ""
            if cmd in {"namespace", "section"}:
                scopes.append((cmd, name))
            elif scopes:
                if name:
                    index = next((j for j in range(len(scopes) - 1, -1, -1)
                                  if scopes[j][1] == name), len(scopes) - 1)
                    del scopes[index:]
                else:
                    scopes.pop()
            event_i += 1
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[match.start():end].rstrip()
        local_name = match.group("name")
        namespace = ".".join(name for kind, name in scopes if kind == "namespace" and name)
        full_name = f"{namespace}.{local_name}" if namespace else local_name
        out.append({
            "kind": match.group("kind"),
            "name": full_name,
            "local_name": local_name,
            "start": text.count("\n", 0, match.start()) + 1,
            "end": text.count("\n", 0, end) + 1,
            "block": block,
            "signature": canonical_signature(block),
        })
    return out


def canonical_signature(block: str) -> str:
    """Canonicalize the declaration header while allowing axiom -> theorem."""
    match = _DECL_RE.search(block)
    if not match:
        return ""
    body = block[match.end("kind"):]
    marker = body.find(":=")
    if marker >= 0:
        body = body[:marker]
    # ``where`` can introduce a declaration body without :=.
    body = re.split(r"(?m)^\s*where\b", body, maxsplit=1)[0]
    # Strip comments because formatting/docs are not part of the target type.
    body = re.sub(r"/-.*?-/", " ", body, flags=re.S)
    body = re.sub(r"--[^\n]*", " ", body)
    return re.sub(r"\s+", " ", body).strip()


def discover_goals(project_root: Path, targets: str = "All") -> list[dict[str, Any]]:
    """Mechanically discover named sorry/axiom targets in project Lean sources."""
    wanted = {_norm(x) for x in re.split(r"[,\n]", targets) if x.strip()}
    all_targets = not wanted or wanted == {"all"}
    goals: list[dict[str, Any]] = []
    skip = {".git", ".lake", ".worktrees", ".unity"}
    for path in sorted(project_root.rglob("*.lean")):
        if any(part in skip for part in path.relative_to(project_root).parts):
            continue
        text = path.read_text(errors="replace")
        for decl in split_declarations(text):
            is_target = decl["kind"] == "axiom" or bool(
                re.search(r"\bsorry\b", _mask_lean_comments(decl["block"])))
            if not is_target:
                continue
            names = {_norm(decl["name"]), _norm(decl["name"].split(".")[-1])}
            if not all_targets and not (wanted & names):
                continue
            rel = str(path.relative_to(project_root))
            goal_id = f"goal-{_sha(rel + ':' + decl['name'])[:12]}"
            goals.append({
                "id": goal_id,
                "declaration": decl["name"],
                "file": rel,
                "kind": decl["kind"],
                "line": decl["start"],
                "signature": decl["signature"],
                "signature_hash": _sha(decl["signature"]),
                "source_hash": _sha(text),
                "status": "open",
                "created_at": _now(),
                "updated_at": _now(),
                "best_candidate": None,
                "accepted_candidate": None,
                "dependencies": [],
                "_search_block": _mask_lean_comments(decl["block"]),
            })
    # Cheap exact dependency discovery: if one unresolved declaration is named in
    # another target's source block, gate its initial proof task. Semantic
    # decomposition remains dynamic and does not require an LLM pre-pass.
    for goal in goals:
        block = goal.pop("_search_block")
        for other in goals:
            if other["id"] == goal["id"]:
                continue
            names = {other["declaration"], other["declaration"].split(".")[-1]}
            if any(re.search(rf"(?<![\w.']){re.escape(name)}(?![\w'])", block) for name in names):
                goal["dependencies"].append(other["id"])
    return goals


def missing_requested_targets(project_root: Path, targets: str) -> list[str]:
    requested = [x.strip() for x in re.split(r"[,\n]", targets) if x.strip()]
    if not requested or {_norm(x) for x in requested} == {"all"}:
        return []
    known: set[str] = set()
    skip = {".git", ".lake", ".worktrees", ".unity"}
    for path in project_root.rglob("*.lean"):
        if any(part in skip for part in path.relative_to(project_root).parts):
            continue
        for decl in split_declarations(path.read_text(errors="replace")):
            known.update({_norm(decl["name"]), _norm(decl["name"].split(".")[-1])})
    return [name for name in requested if _norm(name) not in known]


class RuntimeStore:
    """File-backed transactional state for one prove run."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir).resolve()
        self.state_dir = self.run_dir / "state"
        self.path = self.state_dir / "runtime.json"
        self.lock_path = self.run_dir / "runtime.lock"
        self.events_path = self.run_dir / "logs" / "events.jsonl"

    @classmethod
    def create(
        cls, unity_dir: Path, project_root: Path, goals: list[dict[str, Any]], *,
        run_id: str | None = None, review_quorum: int = 1,
    ) -> "RuntimeStore":
        stamp = time.strftime("%Y%m%d-%H%M%S")
        run_id = run_id or f"prove-{stamp}-{uuid.uuid4().hex[:6]}"
        run_dir = Path(unity_dir) / "runs" / run_id
        for name in ("state", "forum", "artifacts", "candidates", "logs", "verification"):
            (run_dir / name).mkdir(parents=True, exist_ok=True)
        store = cls(run_dir)
        base_commit = _git(project_root, "rev-parse", "HEAD")
        baseline_axioms = sorted(store._axioms_at(project_root, base_commit))
        now = _now()
        state = {
            "schema": SCHEMA_VERSION,
            "run_id": run_id,
            "command": "prove",
            "phase": "PROVE",
            "project_root": str(Path(project_root).resolve()),
            "base_commit": base_commit,
            "created_at": now,
            "updated_at": now,
            "status": "running",
            "policy": {
                "review_quorum": max(0, int(review_quorum)),
                "forbidden": [x.strip() for x in os.getenv(
                    "UNITY_PROVE_FORBIDDEN", ",".join(FORBIDDEN_DEFAULT)).split(",") if x.strip()],
                "allowed_axioms": [x.strip() for x in os.getenv(
                    "UNITY_PROVE_ALLOWED_AXIOMS", "propext,Classical.choice,Quot.sound").split(",")
                    if x.strip()],
                "lease_seconds": int(os.getenv("UNITY_PROVE_LEASE_SECONDS", "300")),
                "task_wall_seconds": int(os.getenv("UNITY_PROVE_TASK_WALL_SECONDS", "1800")),
            },
            "baseline_axioms": baseline_axioms,
            "goals": {g["id"]: g for g in goals},
            "tasks": {},
            "findings": {},
            "artifacts": {},
            "candidates": {},
            "workers": {},
            "telemetry": {
                "duplicate_claim_attempts": 0,
                "duplicate_claims_prevented": 0,
                "cancelled_tasks": 0,
                "dominated_tasks": 0,
                "candidate_revisions": 0,
                "brief_calls": 0,
                "brief_bytes_max": 0,
                "workers_peak": 0,
                "workers_timeline": [],
                "first_candidate_at": None,
                "first_verified_at": None,
                "post_solution_model_calls": 0,
                "model_calls": 0,
                "tokens_total": 0,
                "tokens_before_first_verified": 0,
                "tokens_after_first_verified": 0,
                "cost_total_usd": 0.0,
                "cost_before_first_verified_usd": 0.0,
                "cost_after_first_verified_usd": 0.0,
                "by_model": {},
            },
        }
        _atomic_json(store.path, state)
        _atomic_json(Path(unity_dir) / "current-run.json", {"run_id": run_id, "path": str(run_dir)})
        store.event("run_created", goals=len(goals), base_commit=base_commit)
        initial_tasks: dict[str, str] = {}
        for goal in goals:
            created = store.create_task(
                goal["id"], "proof_attempt",
                f"Coordinate an independent proof search for {goal['declaration']}",
                creator="runtime", strategy_key="self-organized-attempt-1",
                coordination_slot=True,
            )
            initial_tasks[goal["id"]] = created["task"]["id"]
        def wire_dependencies(current: dict) -> None:
            for goal in goals:
                task = current["tasks"][initial_tasks[goal["id"]]]
                task["dependencies"] = [initial_tasks[d] for d in goal.get("dependencies", [])
                                        if d in initial_tasks]
        store._mutate(wire_dependencies)
        return store

    @contextmanager
    def _lock(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def load(self) -> dict:
        with self._lock():
            return json.loads(self.path.read_text())

    def _mutate(self, fn: Callable[[dict], Any]) -> Any:
        with self._lock():
            state = json.loads(self.path.read_text())
            result = fn(state)
            state["updated_at"] = _now()
            _atomic_json(self.path, state)
            return result

    def event(self, kind: str, **fields: Any) -> None:
        entry = {"ts": _now(), "kind": kind, **fields}
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock():
            with self.events_path.open("a") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")

    def create_task(
        self, goal_id: str, kind: str, description: str, creator: str, *,
        strategy_key: str = "", dependencies: list[str] | None = None,
        parent_task: str | None = None, parent_candidate: str | None = None,
        parent_objection: str | None = None, redundant: bool = False,
        coordination_slot: bool = False,
    ) -> dict:
        if kind not in TASK_KINDS:
            raise ValueError(f"kind must be one of {sorted(TASK_KINDS)}")
        strategy = _norm(strategy_key or description)
        now = _now()

        def op(state: dict) -> dict:
            if goal_id not in state["goals"]:
                raise ValueError(f"unknown goal {goal_id}")
            fingerprint = f"{goal_id}:{kind}:{strategy}"
            for task in state["tasks"].values():
                if (not redundant and task["fingerprint"] == fingerprint
                        and task["status"] not in TASK_TERMINAL):
                    return {"ok": False, "conflict": task, "reason": "duplicate_task"}
            task_id = _id("task")
            task = {
                "id": task_id, "goal_id": goal_id, "kind": kind,
                "description": description[:500], "strategy_key": strategy,
                "fingerprint": fingerprint, "dependencies": dependencies or [],
                "status": "pending", "creator": creator, "owner": None,
                "created_at": now, "updated_at": now, "claimed_at": None,
                "lease_expires_at": None, "last_progress_at": now,
                "progress": "", "meaningful_progress": 0,
                "artifacts": [], "findings": [], "model_calls": 0,
                "tool_calls": 0, "tokens": 0, "cost_usd": 0.0, "wall_seconds": 0.0,
                "parent_task": parent_task, "parent_candidate": parent_candidate,
                "parent_objection": parent_objection, "redundant": redundant,
                "coordination_slot": coordination_slot,
                "planned_at": None if coordination_slot else now,
                "supersession_reason": "", "claim_history": [], "strategy_history": [],
            }
            state["tasks"][task_id] = task
            return {"ok": True, "task": task}

        result = self._mutate(op)
        self.event("task_created" if result["ok"] else "duplicate_task_prevented",
                   goal_id=goal_id, task_kind=kind,
                   task_id=(result.get("task") or result["conflict"])["id"])
        return result

    def plan_task(
        self, task_id: str, owner: str, kind: str, strategy_key: str,
        description: str = "",
    ) -> dict:
        """Atomically turn a claimed coordination slot into self-chosen work.

        Strategy selection belongs to the agents and the Forum, not the scheduler.
        Exact structured collisions are rejected while both tasks are live so two
        workers cannot unknowingly adopt the same plan.
        """
        if kind not in TASK_KINDS:
            raise ValueError(f"kind must be one of {sorted(TASK_KINDS)}")
        strategy = _norm(strategy_key)
        if not strategy:
            raise ValueError("strategy_key must be non-empty")
        now = _now()

        def op(state: dict) -> dict:
            task = state["tasks"].get(task_id)
            if not task:
                raise ValueError(f"unknown task {task_id}")
            if task["status"] != "claimed" or task.get("owner") != owner:
                return {"ok": False, "reason": "not_owner", "conflict": {
                    "task_id": task_id, "owner": task.get("owner"), "status": task["status"],
                }}
            fingerprint = f"{task['goal_id']}:{kind}:{strategy}"
            for other in state["tasks"].values():
                if (other["id"] != task_id and other["fingerprint"] == fingerprint
                        and other["status"] not in TASK_TERMINAL):
                    state["telemetry"]["duplicate_claim_attempts"] += 1
                    state["telemetry"]["duplicate_claims_prevented"] += 1
                    return {"ok": False, "reason": "strategy_conflict", "conflict": {
                        "task_id": other["id"], "owner": other.get("owner"),
                        "status": other["status"], "strategy_key": other["strategy_key"],
                        "lease_expires_at": other.get("lease_expires_at"),
                    }}
            old = {"kind": task["kind"], "strategy_key": task["strategy_key"],
                   "description": task["description"], "at": now}
            task.setdefault("strategy_history", []).append(old)
            task.update(kind=kind, strategy_key=strategy, fingerprint=fingerprint,
                        coordination_slot=False, planned_at=now, updated_at=now,
                        last_progress_at=now)
            if description.strip():
                task["description"] = description.strip()[:500]
            task["meaningful_progress"] += 1
            task["lease_expires_at"] = now + state["policy"]["lease_seconds"]
            return {"ok": True, "task": task}

        result = self._mutate(op)
        self.event("task_planned" if result["ok"] else "task_strategy_conflict",
                   task_id=task_id, owner=owner, task_kind=kind,
                   strategy_key=strategy, reason=result.get("reason", ""))
        return result

    def claim_task(
        self, task_id: str, owner: str, *, lease_seconds: int | None = None,
        independent: bool = False,
    ) -> dict:
        now = _now()

        def op(state: dict) -> dict:
            task = state["tasks"].get(task_id)
            if not task:
                raise ValueError(f"unknown task {task_id}")
            lease = lease_seconds or state["policy"]["lease_seconds"]
            if task["status"] == "claimed" and (task.get("lease_expires_at") or 0) <= now:
                task["claim_history"].append({"owner": task["owner"], "status": "stale", "at": now})
                task.update(status="stale", owner=None, lease_expires_at=None, updated_at=now)
            if task["status"] == "claimed":
                if task["owner"] == owner:
                    return {"ok": True, "task": task, "idempotent": True}
                state["telemetry"]["duplicate_claim_attempts"] += 1
                state["telemetry"]["duplicate_claims_prevented"] += 1
                if independent:
                    clone_id = _id("task")
                    clone = {**task, "id": clone_id, "fingerprint": task["fingerprint"] + ":redundant:" + clone_id,
                             "status": "claimed", "owner": owner, "redundant": True,
                             "created_at": now, "updated_at": now, "claimed_at": now,
                             "lease_expires_at": now + lease, "claim_history": [],
                             "model_calls": 1, "tool_calls": 0, "tokens": 0,
                             "cost_usd": 0.0, "wall_seconds": 0.0,
                             "strategy_history": list(task.get("strategy_history", []))}
                    clone["claim_history"].append({"owner": owner, "status": "claimed", "at": now})
                    state["tasks"][clone_id] = clone
                    return {"ok": True, "task": clone, "redundant_of": task_id}
                return {"ok": False, "reason": "claim_conflict", "conflict": {
                    "task_id": task_id, "owner": task["owner"], "status": task["status"],
                    "lease_expires_at": task["lease_expires_at"],
                }}
            if task["status"] in TASK_TERMINAL or task["status"] == "cancelled":
                return {"ok": False, "reason": "task_not_claimable", "conflict": {
                    "task_id": task_id, "owner": task.get("owner"), "status": task["status"]}}
            unmet = []
            for dep in task["dependencies"]:
                dep_task = state["tasks"].get(dep, {})
                dep_goal = state["goals"].get(dep_task.get("goal_id", ""), {})
                if dep_task.get("status") != "complete" and dep_goal.get("status") != "closed":
                    unmet.append(dep)
            if unmet:
                return {"ok": False, "reason": "dependencies_unmet", "dependencies": unmet}
            task.update(status="claimed", owner=owner, claimed_at=now,
                        lease_expires_at=now + lease, updated_at=now)
            task["model_calls"] += 1
            task["claim_history"].append({"owner": owner, "status": "claimed", "at": now})
            return {"ok": True, "task": task}

        result = self._mutate(op)
        self.event("task_claimed" if result["ok"] else "task_claim_conflict",
                   task_id=task_id, owner=owner, reason=result.get("reason", ""))
        return result

    def heartbeat(
        self, task_id: str, owner: str, progress: str = "", *, meaningful: bool = False,
        tool_calls: int = 0, tokens: int = 0, artifacts: list[str] | None = None,
    ) -> dict:
        now = _now()

        def op(state: dict) -> dict:
            task = state["tasks"].get(task_id)
            if not task:
                raise ValueError(f"unknown task {task_id}")
            if task["status"] != "claimed" or task["owner"] != owner:
                return {"ok": False, "cancelled": task["status"] in {"cancelled", "dominated", "superseded"},
                        "status": task["status"], "reason": task.get("supersession_reason", "")}
            task["lease_expires_at"] = now + state["policy"]["lease_seconds"]
            task["updated_at"] = now
            task["progress"] = progress[:500]
            task["tool_calls"] += max(0, tool_calls)
            task["tokens"] += max(0, tokens)
            task["artifacts"].extend((artifacts or [])[:20])
            if meaningful:
                task["meaningful_progress"] += 1
                task["last_progress_at"] = now
            return {"ok": True, "status": "claimed", "lease_expires_at": task["lease_expires_at"]}

        return self._mutate(op)

    def finish_task(self, task_id: str, owner: str, status: str = "complete", reason: str = "") -> dict:
        if status not in {"complete", "failed", "pending"}:
            raise ValueError("status must be complete | failed | pending")

        def op(state: dict) -> dict:
            task = state["tasks"].get(task_id)
            if not task:
                raise ValueError(f"unknown task {task_id}")
            if task.get("owner") not in (None, owner):
                return {"ok": False, "reason": "not_owner", "owner": task.get("owner")}
            if task["status"] in {"cancelled", "dominated", "superseded"}:
                return {"ok": False, "cancelled": True, "status": task["status"]}
            task.update(status=status, owner=None, lease_expires_at=None, updated_at=_now())
            if reason:
                task["progress"] = reason[:500]
            return {"ok": True, "status": status}

        result = self._mutate(op)
        self.event("task_finished", task_id=task_id, owner=owner, status=status, reason=reason[:160])
        return result

    def cancel_task(self, task_id: str, reason: str, status: str = "cancelled") -> dict:
        if status not in {"cancelled", "superseded", "dominated"}:
            raise ValueError("invalid cancellation status")

        def op(state: dict) -> dict:
            task = state["tasks"].get(task_id)
            if not task:
                raise ValueError(f"unknown task {task_id}")
            if task["status"] in TASK_TERMINAL:
                return {"ok": True, "idempotent": True, "status": task["status"]}
            task.update(status=status, owner=None, lease_expires_at=None, updated_at=_now(),
                        supersession_reason=reason[:500])
            state["telemetry"]["cancelled_tasks"] += 1
            if status == "dominated":
                state["telemetry"]["dominated_tasks"] += 1
            return {"ok": True, "status": status}

        result = self._mutate(op)
        self.event("task_cancelled", task_id=task_id, status=status, reason=reason[:160])
        return result

    def expire_stale_claims(self) -> list[str]:
        now = _now()

        def op(state: dict) -> list[str]:
            expired = []
            for task in state["tasks"].values():
                if task["status"] == "claimed" and (task.get("lease_expires_at") or 0) <= now:
                    task["claim_history"].append({"owner": task["owner"], "status": "stale", "at": now})
                    task.update(status="stale", owner=None, lease_expires_at=None, updated_at=now)
                    expired.append(task["id"])
            return expired

        expired = self._mutate(op)
        for task_id in expired:
            self.event("task_stale", task_id=task_id)
        return expired

    def add_finding(
        self, goal_id: str, task_id: str, author: str, kind: str, key: str,
        statement: str, confidence: str = "tentative", evidence: str = "",
        supersedes_tasks: list[str] | None = None,
    ) -> dict:
        if confidence not in {"tentative", "high", "verified", "refuted"}:
            raise ValueError("confidence must be tentative | high | verified | refuted")
        now = _now()

        def op(state: dict) -> dict:
            if goal_id not in state["goals"]:
                raise ValueError(f"unknown goal {goal_id}")
            finding_id = _id("finding")
            normalized = _norm(key)
            supersedes = []
            for finding in state["findings"].values():
                if (finding["goal_id"] == goal_id and finding["key"] == normalized
                        and finding["status"] == "live"):
                    finding.update(status="superseded", superseded_by=finding_id, updated_at=now)
                    supersedes.append(finding["id"])
            finding = {
                "id": finding_id, "goal_id": goal_id, "task_id": task_id,
                "author": author, "kind": kind, "key": normalized,
                "title": key[:160], "statement": statement[:1200],
                "confidence": confidence, "status": "live", "evidence": evidence[:1000],
                "created_at": now, "updated_at": now, "supersedes": supersedes,
                "superseded_by": None,
            }
            state["findings"][finding_id] = finding
            if task_id in state["tasks"]:
                state["tasks"][task_id]["findings"].append(finding_id)
                state["tasks"][task_id]["last_progress_at"] = now
                state["tasks"][task_id]["meaningful_progress"] += 1
            if confidence in {"high", "verified"}:
                for obsolete_id in supersedes_tasks or []:
                    obsolete = state["tasks"].get(obsolete_id)
                    if (obsolete and obsolete["goal_id"] == goal_id
                            and obsolete["status"] not in TASK_TERMINAL):
                        obsolete.update(status="dominated", owner=None, lease_expires_at=None,
                                        supersession_reason=f"finding {finding_id}: {statement[:200]}",
                                        updated_at=now)
                        state["telemetry"]["cancelled_tasks"] += 1
                        state["telemetry"]["dominated_tasks"] += 1
            return finding

        finding = self._mutate(op)
        self.event("finding_added", finding_id=finding["id"], goal_id=goal_id,
                   task_id=task_id, confidence=confidence)
        return finding

    def put_artifact(self, task_id: str, author: str, kind: str, content: str,
                     source_ref: str = "") -> dict:
        """Content-address large/repeated output so prompts can carry only a reference."""
        raw = content.encode()
        digest = hashlib.sha256(raw).hexdigest()
        artifact_id = f"artifact-{digest[:16]}"
        path = self.run_dir / "artifacts" / f"{digest}.txt"
        if not path.exists():
            path.write_bytes(raw)
        now = _now()
        def op(state: dict) -> dict:
            artifact = state["artifacts"].get(artifact_id)
            if artifact is None:
                artifact = {"id": artifact_id, "sha256": digest, "kind": kind,
                            "author": author, "task_id": task_id, "source_ref": source_ref[:500],
                            "path": str(path.relative_to(self.run_dir)), "bytes": len(raw),
                            "created_at": now}
                state["artifacts"][artifact_id] = artifact
            if task_id in state["tasks"] and artifact_id not in state["tasks"][task_id]["artifacts"]:
                state["tasks"][task_id]["artifacts"].append(artifact_id)
            return {**artifact, "deduplicated": artifact["created_at"] != now}
        result = self._mutate(op)
        self.event("artifact_stored", artifact_id=artifact_id, task_id=task_id,
                   bytes=len(raw), deduplicated=result["deduplicated"])
        return result

    def get_artifact(self, artifact_id: str, max_chars: int = 12_000) -> dict:
        state = self.load()
        artifact = state["artifacts"].get(artifact_id)
        if not artifact:
            raise ValueError(f"unknown artifact {artifact_id}")
        text = (self.run_dir / artifact["path"]).read_text(errors="replace")
        clipped = len(text) > max_chars
        return {**artifact, "content": text[:max_chars], "clipped": clipped}

    def submit_candidate(
        self, goal_id: str, author: str, commit_sha: str, *,
        task_id: str | None = None, declarations: list[str] | None = None, notes: str = "",
        parent_candidate: str | None = None, parent_objection: str | None = None,
    ) -> dict:
        state = self.load()
        goal = state["goals"].get(goal_id)
        if not goal:
            raise ValueError(f"unknown goal {goal_id}")
        if goal["status"] == "closed":
            raise ValueError(f"goal {goal_id} is already closed")
        root = Path(state["project_root"])
        exact_commit = _git(root, "rev-parse", "--verify", f"{commit_sha}^{{commit}}")
        tree_hash = _git(root, "rev-parse", f"{exact_commit}^{{tree}}")
        try:
            source_bytes = _git_blob(root, exact_commit, goal["file"])
        except RuntimeError as exc:
            raise ValueError(f"candidate does not contain target file {goal['file']}: {exc}") from exc
        source = source_bytes.decode(errors="replace")
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        identity = _sha(f"{goal_id}\0{author}\0{exact_commit}\0{source_hash}")
        candidate_id = f"candidate-{identity[:16]}"
        run_ref = f"refs/unity/{state['run_id']}/candidates/{candidate_id}"
        _git(root, "update-ref", run_ref, exact_commit)
        # A revision may be based on an objected candidate.  Review artifacts must
        # show the complete delta from the run baseline, not merely the tip commit.
        diff = _git(root, "diff", "--binary", f"{state['base_commit']}..{exact_commit}", timeout=60)
        artifact_dir = self.run_dir / "candidates" / candidate_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "commit.patch").write_text(diff)
        (artifact_dir / "source.lean").write_bytes(source_bytes)
        now = _now()

        def op(current: dict) -> dict:
            existing = current["candidates"].get(candidate_id)
            if existing:
                return existing
            verification_task_id = _id("task")
            current["tasks"][verification_task_id] = {
                "id": verification_task_id, "goal_id": goal_id, "kind": "verification",
                "description": f"Deterministically verify immutable {candidate_id}",
                "strategy_key": candidate_id, "fingerprint": f"{goal_id}:verification:{candidate_id}",
                "dependencies": [], "status": "claimed", "creator": "runtime", "owner": "runtime",
                "created_at": now, "updated_at": now, "claimed_at": now,
                "lease_expires_at": None, "last_progress_at": now, "progress": "",
                "meaningful_progress": 0, "artifacts": [], "findings": [], "model_calls": 0,
                "tool_calls": 0, "tokens": 0, "cost_usd": 0.0,
                "wall_seconds": 0.0, "parent_task": task_id,
                "parent_candidate": candidate_id, "parent_objection": None, "redundant": False,
                "supersession_reason": "", "claim_history": [
                    {"owner": "runtime", "status": "claimed", "at": now}],
            }
            candidate = {
                "id": candidate_id, "goal_id": goal_id, "author": author,
                "task_id": task_id,
                "commit_sha": exact_commit, "tree_hash": tree_hash,
                "git_ref": run_ref,
                "source_hash": source_hash, "file": goal["file"],
                "declarations": declarations or [goal["declaration"]],
                "artifact": str((artifact_dir / "commit.patch").relative_to(self.run_dir)),
                "notes": notes[:1000], "submitted_at": now, "updated_at": now,
                "status": "submitted", "verification": None,
                "verification_task": verification_task_id,
                "endorsements": [], "objections": [],
                "parent_candidate": parent_candidate, "parent_objection": parent_objection,
                "superseded_by": None,
            }
            current["candidates"][candidate_id] = candidate
            if task_id in current["tasks"]:
                task = current["tasks"][task_id]
                if task["goal_id"] != goal_id:
                    raise ValueError(f"task {task_id} belongs to a different goal")
                task["last_progress_at"] = now
                task["meaningful_progress"] += 1
            current["telemetry"]["candidate_revisions"] += 1
            if current["telemetry"]["first_candidate_at"] is None:
                current["telemetry"]["first_candidate_at"] = now
            return candidate

        candidate = self._mutate(op)
        _atomic_json(artifact_dir / "identity.json", {
            k: candidate[k] for k in ("id", "goal_id", "author", "commit_sha", "tree_hash", "source_hash", "file")
        })
        self.event("candidate_submitted", candidate_id=candidate_id, goal_id=goal_id,
                   author=author, commit_sha=exact_commit, source_hash=source_hash)
        return candidate

    def _axioms_at(self, root: Path, commit: str) -> set[str]:
        proc = subprocess.run(
            ["git", "grep", "-h", "-E", r"^[[:space:]]*axiom[[:space:]]+", commit, "--", "*.lean"],
            cwd=root, text=True, capture_output=True, timeout=30,
        )
        return set(_AXIOM_RE.findall(proc.stdout)) if proc.returncode in (0, 1) else set()

    def verify_candidate(self, candidate_id: str, *, build_timeout: int = 900) -> dict:
        """Verify the exact submitted commit in a fresh detached worktree."""
        state = self.load()
        candidate = state["candidates"].get(candidate_id)
        if not candidate:
            raise ValueError(f"unknown candidate {candidate_id}")
        if candidate["status"] != "submitted":
            return candidate.get("verification") or {"passed": candidate["status"] == "machine_verified"}
        goal = state["goals"][candidate["goal_id"]]
        root = Path(state["project_root"])
        started = _now()
        checks: dict[str, Any] = {}
        messages: list[str] = []
        artifact_dir = self.run_dir / "candidates" / candidate_id

        try:
            source_bytes = _git_blob(root, candidate["commit_sha"], goal["file"])
            source = source_bytes.decode(errors="replace")
            forbidden = state["policy"].get("forbidden", list(FORBIDDEN_DEFAULT))
            forbidden_re = (re.compile(r"\b(?:" + "|".join(re.escape(x) for x in forbidden) + r")\b")
                            if forbidden else re.compile(r"(?!x)x"))
            checks["source_hash"] = hashlib.sha256(source_bytes).hexdigest() == candidate["source_hash"]
            declarations = {d["name"]: d for d in split_declarations(source)}
            decl = declarations.get(goal["declaration"])
            checks["target_exists"] = decl is not None
            checks["signature_preserved"] = bool(decl and _sha(decl["signature"]) == goal["signature_hash"])
            checks["target_not_axiom"] = bool(decl and decl["kind"] != "axiom")
            checks["target_no_forbidden"] = bool(
                decl and not forbidden_re.search(_mask_lean_comments(decl["block"])))
            added = "\n".join(line[1:] for line in _git(
                root, "diff", "--unified=0", f"{state['base_commit']}..{candidate['commit_sha']}",
                "--", "*.lean", check=False, timeout=60,
            ).splitlines() if line.startswith("+") and not line.startswith("+++"))
            checks["diff_no_forbidden"] = not forbidden_re.search(_mask_lean_comments(added))
            new_axioms = self._axioms_at(root, candidate["commit_sha"]) - set(state.get("baseline_axioms", []))
            checks["no_new_axioms"] = not new_axioms
            if new_axioms:
                messages.append(f"new axioms: {sorted(new_axioms)}")
        except Exception as exc:
            messages.append(f"source checks failed: {exc}")

        build_output = ""
        build_rc: int | None = None
        verify_dir = self.run_dir / "verification" / candidate_id
        if verify_dir.exists():
            shutil.rmtree(verify_dir)
        try:
            add = subprocess.run(
                ["git", "worktree", "add", "--detach", str(verify_dir), candidate["commit_sha"]],
                cwd=root, text=True, capture_output=True, timeout=60,
            )
            if add.returncode:
                raise RuntimeError(add.stderr.strip() or "could not create verification worktree")
            # Ask Lean itself for the exact target's axiom dependencies. Appending
            # the command to the target file avoids guessing arbitrary Lake module
            # roots; restore the exact bytes before the normal project build.
            target_path = verify_dir / goal["file"]
            exact_bytes = target_path.read_bytes()
            with target_path.open("ab") as fh:
                fh.write(f"\n#print axioms _root_.{goal['declaration']}\n".encode())
            axiom_proc = subprocess.run(
                ["lake", "env", "lean", goal["file"]], cwd=verify_dir,
                text=True, capture_output=True, timeout=build_timeout,
            )
            axiom_output = (axiom_proc.stdout or "") + (axiom_proc.stderr or "")
            target_path.write_bytes(exact_bytes)
            checks["axioms_inspected"] = axiom_proc.returncode == 0
            mentioned: set[str] = set()
            for listing in re.findall(r"depends on axioms:\s*\[([^\]]*)\]", axiom_output):
                mentioned.update(re.findall(r"[A-Za-z_]\w*(?:\.\w+)*", listing))
            allowed_axioms = set(state["policy"].get(
                "allowed_axioms", ["propext", "Classical.choice", "Quot.sound"]))
            unexpected = sorted(mentioned - allowed_axioms)
            checks["no_unexpected_axioms"] = not unexpected and "sorryAx" not in axiom_output
            if unexpected or "sorryAx" in axiom_output:
                messages.append(f"unexpected target axioms: {unexpected or ['sorryAx']}")
            proc = subprocess.run(
                ["lake", "build"], cwd=verify_dir, text=True, capture_output=True,
                timeout=build_timeout,
            )
            build_rc = proc.returncode
            build_output = axiom_output + "\n" + (proc.stdout or "") + (proc.stderr or "")
            checks["lake_build"] = build_rc == 0
        except subprocess.TimeoutExpired as exc:
            messages.append(f"lake build timed out after {build_timeout}s")
            build_output = ((exc.stdout or "") if isinstance(exc.stdout, str) else "") + ((exc.stderr or "") if isinstance(exc.stderr, str) else "")
            checks["lake_build"] = False
        except Exception as exc:
            messages.append(f"lake build failed to run: {exc}")
            checks["lake_build"] = False
        finally:
            if verify_dir.exists():
                subprocess.run(["git", "worktree", "remove", "--force", str(verify_dir)],
                               cwd=root, capture_output=True)
            subprocess.run(["git", "worktree", "prune"], cwd=root, capture_output=True)

        (artifact_dir / "verification.log").write_text(build_output)
        passed = bool(checks) and all(checks.values())
        record = {
            "candidate_id": candidate_id, "commit_sha": candidate["commit_sha"],
            "source_hash": candidate["source_hash"], "started_at": started,
            "finished_at": _now(), "passed": passed, "checks": checks,
            "build_returncode": build_rc,
            "log_artifact": str((artifact_dir / "verification.log").relative_to(self.run_dir)),
            "summary": (messages + [line for line in build_output.splitlines()[-8:] if line.strip()])[-12:],
        }

        def op(current: dict) -> None:
            cand = current["candidates"][candidate_id]
            # Identity fields are never rewritten here.
            cand["verification"] = record
            cand["status"] = "machine_verified" if passed else "failed"
            cand["updated_at"] = _now()
            verification_task = current["tasks"].get(cand.get("verification_task"))
            if verification_task:
                verification_task.update(
                    status="complete" if passed else "failed", owner=None,
                    updated_at=_now(), progress="deterministic verification passed" if passed
                    else "deterministic verification failed")
            goal_state = current["goals"][cand["goal_id"]]
            if passed:
                parent_id = cand.get("parent_candidate")
                parent = current["candidates"].get(parent_id) if parent_id else None
                if parent and parent["status"] not in {"accepted", "rejected"}:
                    parent["status"] = "superseded"
                    parent["superseded_by"] = candidate_id
                    parent["updated_at"] = _now()
                goal_state["best_candidate"] = candidate_id
                goal_state["status"] = "review"
                goal_state["updated_at"] = _now()
                if current["telemetry"]["first_verified_at"] is None:
                    current["telemetry"]["first_verified_at"] = _now()
                for task in current["tasks"].values():
                    if (task["goal_id"] == cand["goal_id"] and task["kind"] in SPECULATIVE_KINDS
                            and task["status"] not in TASK_TERMINAL):
                        task.update(status="dominated", owner=None, lease_expires_at=None,
                                    supersession_reason=f"verified candidate {candidate_id}", updated_at=_now())
                        current["telemetry"]["cancelled_tasks"] += 1
                        current["telemetry"]["dominated_tasks"] += 1
                self._evaluate_locked(current, cand)
                if cand["status"] == "reviewable":
                    self._create_review_locked(current, cand)

        self._mutate(op)
        self.event("candidate_verified" if passed else "candidate_verification_failed",
                   candidate_id=candidate_id, goal_id=candidate["goal_id"], checks=checks)
        return record

    def _create_review_locked(self, state: dict, candidate: dict) -> dict | None:
        fingerprint = f"{candidate['goal_id']}:review:{candidate['id']}"
        for task in state["tasks"].values():
            if task["fingerprint"] == fingerprint and task["status"] not in TASK_TERMINAL:
                return task
        now = _now()
        task_id = _id("task")
        task = {
            "id": task_id, "goal_id": candidate["goal_id"], "kind": "review",
            "description": f"Independently review immutable {candidate['id']}",
            "strategy_key": candidate["id"], "fingerprint": fingerprint,
            "dependencies": [], "status": "pending", "creator": "runtime", "owner": None,
            "created_at": now, "updated_at": now, "claimed_at": None,
            "lease_expires_at": None, "last_progress_at": now, "progress": "",
            "meaningful_progress": 0, "artifacts": [], "findings": [], "model_calls": 0,
            "tool_calls": 0, "tokens": 0, "cost_usd": 0.0, "wall_seconds": 0.0,
            "parent_task": None, "parent_candidate": candidate["id"],
            "parent_objection": None, "redundant": False, "supersession_reason": "",
            "claim_history": [],
        }
        state["tasks"][task_id] = task
        return task

    def endorse(self, candidate_id: str, author: str, evidence: str = "") -> dict:
        now = _now()

        def op(state: dict) -> dict:
            cand = state["candidates"].get(candidate_id)
            if not cand:
                raise ValueError(f"unknown candidate {candidate_id}")
            if cand["status"] not in {"machine_verified", "reviewable", "blocked", "acceptable"}:
                return {"ok": False, "reason": f"candidate is {cand['status']}"}
            if _norm(author) == _norm(cand["author"]):
                return {"ok": False, "reason": "candidate author is not an independent reviewer"}
            if not any(_norm(e["author"]) == _norm(author) for e in cand["endorsements"]):
                cand["endorsements"].append({"author": author, "evidence": evidence[:1000],
                                             "commit_sha": cand["commit_sha"],
                                             "source_hash": cand["source_hash"], "at": now})
            self._evaluate_locked(state, cand)
            return {"ok": True, "candidate_id": candidate_id, "status": cand["status"],
                    "endorsements": cand["endorsements"]}

        result = self._mutate(op)
        self.event("candidate_endorsed", candidate_id=candidate_id, author=author,
                   accepted_for_review=result.get("ok", False))
        return result

    def object(self, candidate_id: str, author: str, reason: str, evidence: str = "") -> dict:
        if not reason.strip():
            raise ValueError("an objection needs a concrete reason")
        now = _now()

        def op(state: dict) -> dict:
            cand = state["candidates"].get(candidate_id)
            if not cand:
                raise ValueError(f"unknown candidate {candidate_id}")
            if cand["status"] not in {"machine_verified", "reviewable", "blocked", "acceptable"}:
                return {"ok": False, "reason": f"candidate is {cand['status']}"}
            objection_id = _id("objection")
            objection = {"id": objection_id, "author": author, "reason": reason[:1000],
                          "evidence": evidence[:1000], "status": "open", "created_at": now,
                          "resolved_at": None, "resolution": "", "candidate_id": candidate_id,
                          "commit_sha": cand["commit_sha"], "source_hash": cand["source_hash"]}
            cand["objections"].append(objection)
            cand["status"] = "blocked"
            cand["updated_at"] = now
            strategy = _norm(f"fix-{candidate_id}-{reason[:80]}")
            task_id = _id("task")
            task = {
                "id": task_id, "goal_id": cand["goal_id"], "kind": "debugging",
                "description": f"Address objection {objection_id}: {reason[:300]}",
                "strategy_key": strategy, "fingerprint": f"{cand['goal_id']}:debugging:{strategy}",
                "dependencies": [], "status": "pending", "creator": author, "owner": None,
                "created_at": now, "updated_at": now, "claimed_at": None,
                "lease_expires_at": None, "last_progress_at": now, "progress": "",
                "meaningful_progress": 0, "artifacts": [], "findings": [], "model_calls": 0,
                "tool_calls": 0, "tokens": 0, "cost_usd": 0.0, "wall_seconds": 0.0,
                "parent_task": None, "parent_candidate": candidate_id,
                "parent_objection": objection_id, "redundant": False,
                "supersession_reason": "", "claim_history": [],
            }
            state["tasks"][task_id] = task
            return {"ok": True, "candidate_id": candidate_id, "status": "blocked",
                    "objection": objection, "task": task}

        result = self._mutate(op)
        if result.get("ok"):
            self.event("candidate_objected", candidate_id=candidate_id, author=author,
                       objection_id=result["objection"]["id"], task_id=result["task"]["id"])
        return result

    def resolve_objection(self, candidate_id: str, objection_id: str, author: str, resolution: str) -> dict:
        def op(state: dict) -> dict:
            cand = state["candidates"].get(candidate_id)
            if not cand:
                raise ValueError(f"unknown candidate {candidate_id}")
            if cand["status"] not in {"machine_verified", "reviewable", "blocked", "acceptable"}:
                return {"ok": False, "reason": f"candidate is {cand['status']}"}
            found = None
            for objection in cand["objections"]:
                if objection["id"] == objection_id:
                    found = objection
                    if objection["status"] == "open":
                        objection.update(status="resolved", resolved_at=_now(), resolution=resolution[:1000],
                                         resolved_by=author)
            if not found:
                raise ValueError(f"unknown objection {objection_id}")
            self._evaluate_locked(state, cand)
            return {"ok": True, "candidate_id": candidate_id, "status": cand["status"]}

        result = self._mutate(op)
        if result.get("ok"):
            self.event("objection_resolved", candidate_id=candidate_id,
                       objection_id=objection_id, author=author)
        return result

    def _evaluate_locked(self, state: dict, cand: dict) -> None:
        if not cand.get("verification", {}).get("passed"):
            return
        open_objections = [o for o in cand["objections"] if o["status"] == "open"]
        independent = { _norm(e["author"]) for e in cand["endorsements"]
                        if _norm(e["author"]) != _norm(cand["author"]) }
        if open_objections:
            cand["status"] = "blocked"
        elif len(independent) >= state["policy"]["review_quorum"]:
            cand["status"] = "acceptable"
        else:
            cand["status"] = "reviewable"
        cand["updated_at"] = _now()

    def accept_candidate(self, candidate_id: str) -> dict:
        """Deterministically merge an acceptable immutable commit and close its goal."""
        def reserve(current: dict) -> dict:
            candidate = current["candidates"].get(candidate_id)
            if not candidate:
                raise ValueError(f"unknown candidate {candidate_id}")
            if candidate["status"] != "acceptable":
                return {"ok": False, "reason": f"candidate is {candidate['status']}"}
            if current["goals"][candidate["goal_id"]]["status"] == "closed":
                candidate["status"] = "superseded"
                return {"ok": False, "reason": "goal already closed"}
            candidate["status"] = "accepting"
            candidate["updated_at"] = _now()
            return {"ok": True, "candidate": dict(candidate)}
        reservation = self._mutate(reserve)
        if not reservation["ok"]:
            return reservation
        state = self.load()
        cand = reservation["candidate"]
        root = Path(state["project_root"])
        merge_error = ""
        try:
            already = subprocess.run(
                ["git", "merge-base", "--is-ancestor", cand["commit_sha"], "HEAD"], cwd=root,
                capture_output=True,
            ).returncode == 0
            if not already:
                merge_base = _git(root, "merge-base", "HEAD", cand["commit_sha"])
                commits = _git(root, "rev-list", "--reverse", f"{merge_base}..{cand['commit_sha']}").splitlines()
                proc = subprocess.run(
                    ["git", "cherry-pick", *commits], cwd=root,
                    text=True, capture_output=True,
                )
                if proc.returncode:
                    subprocess.run(["git", "cherry-pick", "--abort"], cwd=root, capture_output=True)
                    merge_error = (proc.stderr or proc.stdout or "merge failed")[-2000:]
        except Exception as exc:
            subprocess.run(["git", "cherry-pick", "--abort"], cwd=root, capture_output=True)
            merge_error = str(exc)[-2000:]
        if merge_error:
            def block(current: dict) -> None:
                current["candidates"][candidate_id]["status"] = "blocked"
                current["candidates"][candidate_id]["merge_error"] = merge_error
                current["goals"][cand["goal_id"]]["status"] = "open"
            self._mutate(block)
            self.create_task(cand["goal_id"], "dependency_resolution",
                             f"Rebase {candidate_id} after merge failure: {merge_error[:240]}",
                             "runtime", strategy_key=f"rebase-{candidate_id}",
                             parent_candidate=candidate_id)
            self.event("candidate_merge_failed", candidate_id=candidate_id, reason=merge_error[:500])
            return {"ok": False, "reason": merge_error}

        def close(current: dict) -> dict:
            candidate = current["candidates"][candidate_id]
            if candidate["status"] != "accepting":
                return {"ok": False, "reason": f"candidate became {candidate['status']}"}
            candidate["status"] = "accepted"
            candidate["accepted_at"] = _now()
            goal = current["goals"][candidate["goal_id"]]
            goal.update(status="closed", accepted_candidate=candidate_id, updated_at=_now())
            cancelled = []
            for task in current["tasks"].values():
                if task["goal_id"] == goal["id"] and task["status"] not in TASK_TERMINAL:
                    task.update(status="cancelled", owner=None, lease_expires_at=None,
                                supersession_reason=f"goal closed by {candidate_id}", updated_at=_now())
                    current["telemetry"]["cancelled_tasks"] += 1
                    cancelled.append(task["id"])
            if all(g["status"] == "closed" for g in current["goals"].values()):
                current["status"] = "closing"
            return {"ok": True, "goal_id": goal["id"], "cancelled": cancelled,
                    "commit_sha": candidate["commit_sha"]}

        result = self._mutate(close)
        if not result["ok"]:
            return result
        self.event("candidate_accepted", candidate_id=candidate_id, goal_id=result["goal_id"],
                   cancelled_tasks=len(result["cancelled"]))
        return result

    def set_worker(self, agent: str, task_id: str | None, status: str, model: str = "") -> None:
        now = _now()
        def op(state: dict) -> None:
            state["workers"][agent] = {"agent": agent, "task_id": task_id, "status": status,
                                       "model": model, "updated_at": now}
            active = sum(1 for w in state["workers"].values() if w["status"] == "active")
            telemetry = state["telemetry"]
            telemetry["workers_peak"] = max(telemetry["workers_peak"], active)
            timeline = telemetry["workers_timeline"]
            if not timeline or timeline[-1]["active"] != active:
                timeline.append({"ts": now, "active": active})
                del timeline[:-200]
        self._mutate(op)

    def record_task_usage(self, task_id: str, seconds: float, stats: dict | None = None,
                          *, model: str = "", agent: str = "") -> None:
        stats = stats or {}
        def op(state: dict) -> None:
            task = state["tasks"].get(task_id)
            if not task:
                return
            task["wall_seconds"] += max(0.0, seconds)
            usage = stats.get("usage") or {}
            tokens = int(usage.get("total_tokens") or 0)
            cost = float(stats.get("cost_usd") or 0.0)
            task["tokens"] += tokens
            task["cost_usd"] = round(task.get("cost_usd", 0.0) + cost, 8)
            telemetry = state["telemetry"]
            telemetry["model_calls"] += 1
            telemetry["tokens_total"] += tokens
            telemetry["cost_total_usd"] = round(telemetry["cost_total_usd"] + cost, 8)
            first_verified = telemetry.get("first_verified_at")
            is_post = bool(first_verified and (task.get("claimed_at") or 0) >= first_verified)
            bucket = "after_first_verified" if is_post else "before_first_verified"
            telemetry[f"tokens_{bucket}"] += tokens
            telemetry[f"cost_{bucket}_usd"] = round(telemetry[f"cost_{bucket}_usd"] + cost, 8)
            if is_post:
                telemetry["post_solution_model_calls"] += 1
            model_key = model or "unknown"
            row = telemetry["by_model"].setdefault(
                model_key, {"model_calls": 0, "tokens": 0, "cost_usd": 0.0, "agents": {}})
            row["model_calls"] += 1
            row["tokens"] += tokens
            row["cost_usd"] = round(row["cost_usd"] + cost, 8)
            if agent:
                row["agents"][agent] = row["agents"].get(agent, 0) + 1
        self._mutate(op)

    def record_brief_size(self, size: int) -> None:
        def op(state: dict) -> None:
            state["telemetry"]["brief_calls"] += 1
            state["telemetry"]["brief_bytes_max"] = max(
                state["telemetry"]["brief_bytes_max"], max(0, size))
        self._mutate(op)

    def mark_status(self, status: str) -> None:
        now = _now()
        def op(state: dict) -> None:
            state["status"] = status
            if status == "running":
                # A process may die after reserving acceptance but before recording
                # merge completion. Deterministic setup has already required a clean
                # Git tree; retrying from acceptable is therefore idempotent.
                for candidate in state["candidates"].values():
                    if candidate["status"] == "accepting":
                        candidate["status"] = "acceptable"
                        candidate["updated_at"] = now
            if status in {"complete", "exhausted", "stopped"}:
                state["finished_at"] = now
                first = state["telemetry"].get("first_verified_at")
                state["telemetry"]["post_solution_seconds"] = round(now - first, 3) if first else None
                state["telemetry"]["time_to_first_candidate"] = (
                    round(state["telemetry"]["first_candidate_at"] - state["created_at"], 3)
                    if state["telemetry"].get("first_candidate_at") else None)
                state["telemetry"]["time_to_first_verified"] = (
                    round(first - state["created_at"], 3) if first else None)
                state["telemetry"]["task_count"] = len(state["tasks"])
                state["telemetry"]["useful_findings"] = len(state["findings"])
        self._mutate(op)


def state_for_forum(forum_dir: Path) -> RuntimeStore | None:
    """Find the prove store adjacent to a run-scoped forum directory."""
    store = RuntimeStore(Path(forum_dir).resolve().parent)
    return store if store.path.exists() else None
