"""Proof coordination and mechanical integration, without a discussion forum."""

import argparse
from pathlib import Path

from fastmcp import FastMCP

from . import artifacts, prove_state, worktree

mcp = FastMCP("unity-prove")
STATE_DIR = Path("forum")
PROJECT_ROOT = None


def configure(state_dir: Path, project_root: Path) -> None:
    global STATE_DIR, PROJECT_ROOT
    STATE_DIR = Path(state_dir)
    PROJECT_ROOT = Path(project_root)


def _root() -> Path:
    if PROJECT_ROOT is None:
        raise ValueError("prove coordination tools require a configured project root")
    return PROJECT_ROOT


def _artifact_dir() -> Path:
    return STATE_DIR.resolve().parent / "artifacts"


def _public(value):
    """Copy a proof-control response without agent-authored discussion fields."""
    if isinstance(value, list):
        return [_public(item) for item in value if not (
            isinstance(item, dict) and _discussion_event(item))]
    if not isinstance(value, dict):
        return value
    hidden = {"attempts", "assistants", "notes", "endorsements", "objections",
              "incorrect_reason", "evidence", "evidence_artifact_id", "evidence_sha256",
              "evidence_bytes", "review", "resolution"}
    if "kind" in value and ("event_id" in value or "timestamp" in value):
        hidden.add("reason")
    return {key: _public(item) for key, item in value.items() if key not in hidden}


def _discussion_event(event):
    kind = event.get("kind", "")
    return ("finding" in kind or kind in {
        "strategy_assisted", "candidate_endorsed", "candidate_objected",
        "candidate_objection_resolved"})


@mcp.tool()
def artifact_info(artifact_id: str) -> dict:
    """Return immutable diagnostic artifact metadata."""
    return artifacts.artifact_info(_artifact_dir(), artifact_id)


@mcp.tool()
def artifact_read(artifact_id: str, offset: int = 0, limit: int = artifacts.DEFAULT_READ_LIMIT) -> dict:
    """Read a bounded page of a diagnostic artifact."""
    return artifacts.read_artifact(_artifact_dir(), artifact_id, offset=offset, limit=limit)


@mcp.tool()
def register_strategy(author: str, description: str, decl: str = "", strategy_family: str = "") -> dict:
    """Register a proof strategy without claiming it."""
    return _public(prove_state.register_strategy(STATE_DIR, author, description, decl, strategy_family))


@mcp.tool()
def claim_strategy(strategy_id: str, author: str) -> dict:
    """Claim a strategy after synchronizing your worktree to main."""
    root = _root()
    if not worktree.worktree_matches_main(root, author):
        return {"status": "stale_worktree", "strategy_id": strategy_id,
                "message": "call sync_from_main before claiming another strategy",
                "main_sha": worktree.main_commit(root)}
    return _public(prove_state.claim_strategy(STATE_DIR, strategy_id, author, worktree.main_commit(root)))


@mcp.tool()
def unclaim_strategy(strategy_id: str, author: str, reason: str = "") -> dict:
    """Release an owned strategy."""
    return _public(prove_state.unclaim_strategy(STATE_DIR, strategy_id, author, reason))


@mcp.tool()
def mark_strategy_incorrect(strategy_id: str, author: str, reason: str, evidence: str = "") -> dict:
    """Close an invalid strategy with supporting evidence."""
    compacted = artifacts.compact_text(_artifact_dir(), evidence, kind="strategy_failure_evidence",
                                       producer=author, source="proof control",
                                       metadata={"strategy_id": strategy_id})
    if isinstance(compacted, str):
        prepared = {"evidence": compacted, "evidence_artifact_id": "", "evidence_sha256": "",
                    "evidence_bytes": len(compacted.encode("utf-8"))}
    else:
        prepared = {"evidence": artifacts.preview_text(evidence, 3500),
                    "evidence_artifact_id": compacted["artifact_id"],
                    "evidence_sha256": compacted["sha256"], "evidence_bytes": compacted["bytes"]}
    return _public(prove_state.mark_strategy_incorrect(STATE_DIR, strategy_id, author, reason, **prepared))


@mcp.tool()
def emit_candidate(strategy_id: str, author: str, commit_sha: str, decl: str = "",
                   notes: str = "", supersedes: str = "") -> dict:
    """Submit an exact committed candidate from an owned strategy."""
    resolved = worktree.verify_candidate_commit(_root(), author, commit_sha)
    return _public(prove_state.emit_candidate(STATE_DIR, strategy_id, author, resolved, decl, notes, supersedes))


@mcp.tool()
def sync_to_main(candidate_id: str, author: str) -> dict:
    """Mechanically review and merge an immutable candidate."""
    started = prove_state.begin_merge(STATE_DIR, candidate_id, author)
    if started.get("idempotent") or started.get("conflict"):
        return _public(started)
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
    return _public({**finished, "sync": merged, **({"retry": retry} if merged.get("blocked") else {})})


@mcp.tool()
def sync_from_main(author: str, reason: str = "") -> dict:
    """Discard the caller's worktree attempt and synchronize it to main."""
    root = _root()
    released = prove_state.release_author_claims(STATE_DIR, author, reason or "synchronizing from main")
    synced = worktree.force_sync_from_main(root, author)
    result = prove_state.record_sync(STATE_DIR, author, synced["main_sha"], released)
    return _public({**result, "worktree": synced["worktree"]})


@mcp.tool()
def prove_status(decl: str = "", include_type_contract: bool = False) -> dict:
    """Return proof status, with structural type contracts omitted by default."""
    state = prove_state.load_state(STATE_DIR)
    def public_declaration(item):
        result = dict(item)
        contract = item.get("type_contract")
        result["type_contract_sha256"] = contract.get("sha256") if isinstance(contract, dict) else None
        if not include_type_contract:
            result.pop("type_contract", None)
        return result
    state = _public(state)
    events = state.get("events", [])
    if not decl:
        return {**state, "findings": {}, "events": events, "declarations": {
            name: public_declaration(item) for name, item in state["declarations"].items()}}
    if decl not in state["declarations"]:
        raise ValueError(f"unknown prove declaration '{decl}'")
    return {"revision": state["revision"], "main_sha": state.get("main_sha"),
            "declaration": public_declaration(state["declarations"][decl]),
            "strategies": [item for item in state["strategies"].values() if item.get("decl") in ("", decl)],
            "candidates": [item for item in state["candidates"].values() if item.get("decl") == decl],
            "events": [item for item in events if item.get("decl") in (None, "", decl)][-50:]}


def build_brief(author: str) -> str:
    """Return a compact digest of proof goals, strategies and candidates."""
    state = prove_status()
    lines = [f"Proof goals: {sum(x.get('status') == 'solved' for x in state['declarations'].values())}/{len(state['declarations'])} solved."]
    for decl, item in state["declarations"].items():
        if item.get("status") != "solved":
            lines.append(f"  {decl}: {item.get('status', 'unresolved')}")
    for item in state["strategies"].values():
        if item.get("status") in {"registered", "claimed", "paused"}:
            lines.append(f"  Strategy {item['strategy_id']} [{item.get('decl') or 'general'}] {item['status']} owner={item.get('owner') or 'unclaimed'}: {item['description'][:120]}")
    for item in state["candidates"].values():
        lines.append(f"  Candidate {item['candidate_id']} [{item['decl']}] {item['status']} commit={item['commit_sha'][:12]}")
        if item.get("merge_blocker"):
            lines.append(f"    Merge blocker: {item['merge_blocker'].get('message', '')[:500]}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--project-root", required=True)
    args = parser.parse_args()
    configure(Path(args.state_dir), Path(args.project_root))
    mcp.run()


if __name__ == "__main__":
    main()
