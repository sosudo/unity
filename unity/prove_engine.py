"""Internal mechanical proof engine; this module exposes no MCP server."""

import json
from pathlib import Path

from . import prove_state, worktree

STATE_DIR = Path("forum")
PROJECT_ROOT = None


def configure(state_dir: Path, project_root: Path) -> None:
    global STATE_DIR, PROJECT_ROOT
    STATE_DIR = Path(state_dir)
    PROJECT_ROOT = Path(project_root)


def _root() -> Path:
    if PROJECT_ROOT is None:
        raise ValueError("proof engine requires a configured project root")
    return PROJECT_ROOT


def sync_to_main(candidate_id: str, author: str) -> dict:
    """Mechanically review and merge an immutable candidate."""
    started = prove_state.begin_merge(STATE_DIR, candidate_id, author)
    if started.get("idempotent") or started.get("conflict"):
        return started
    candidate = started["candidate"]
    declaration = prove_state.load_state(STATE_DIR)["declarations"][candidate["decl"]]
    try:
        merged = worktree.sync_candidate_to_main(_root(), candidate, declaration)
    except Exception as exc:
        merged = {"ok": False, "error": f"candidate merge raised: {exc}"}
    finished = prove_state.finish_merge(
        STATE_DIR, candidate_id, success=bool(merged.get("ok")), blocked=bool(merged.get("blocked")),
        blocker=({"kind": merged.get("blocker_kind", "integration_blocked"),
                  "message": merged.get("error", ""), "main_sha": merged.get("main_sha", ""),
                  "git_status": merged.get("git_status", [])} if merged.get("blocked") else None),
        author=author, main_sha=merged.get("main_sha", ""), error=merged.get("error", ""),
        build=merged.get("build"), verification=merged.get("verification"))
    retry = ("Resolve the recorded main-checkout blocker, preserving legitimate changes, then call "
             f"sync_to_main again for the same candidate {candidate_id}.")
    return {**finished, "sync": merged, **({"retry": retry} if merged.get("blocked") else {})}


def sync_from_main(author: str, reason: str = "") -> dict:
    """Discard the caller's worktree attempt and synchronize it to main."""
    root = _root()
    released = prove_state.release_author_claims(STATE_DIR, author, reason or "synchronizing from main")
    synced = worktree.force_sync_from_main(root, author)
    result = prove_state.record_sync(STATE_DIR, author, synced["main_sha"], released)
    return {**result, "worktree": synced["worktree"]}

def status_snapshot() -> dict:
    state = prove_state.load_state(STATE_DIR)
    declarations = {}
    for name, item in state.get("declarations", {}).items():
        public = {k: v for k, v in item.items() if k != "type_contract"}
        contract = item.get("type_contract")
        public["type_contract_sha256"] = contract.get("sha256") if isinstance(contract, dict) else None
        declarations[name] = public
    candidates = {key: {k: v for k, v in item.items()
                        if k not in {"notes", "endorsements", "objections"}}
                  for key, item in state.get("candidates", {}).items()}
    return {"revision": state.get("revision", 0), "main_sha": state.get("main_sha"),
            "declarations": declarations, "candidates": candidates}


def write_status(paths) -> None:
    """Publish a controller-written snapshot for the critic and retrospective."""
    path = paths.unity / "prove-status.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(status_snapshot(), indent=2) + "\n")
    temporary.replace(path)


def build_brief(author: str) -> str:
    state = status_snapshot()
    lines = ["Controller proof state:"]
    for name, item in state["declarations"].items():
        lines.append(f"  {name}: {item.get('status', 'unresolved')}")
    for item in state["candidates"].values():
        if item.get("author") == author:
            lines.append(f"  Your candidate {item['candidate_id']}: {item['status']}")
            if item.get("merge_blocker"):
                lines.append(f"    {item['merge_blocker'].get('message', '')[:800]}")
    return "\n".join(lines)[:12000]
