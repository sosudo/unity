"""Unity Forum MCP Server.

File-backed forum for formalization agents. Each thread is stored as
<forum-dir>/<thread_id>.json. Config (dimensions, tags) lives in config.json.

Run via:
    python -m unity.forum.server --forum-dir <path>
or programmatically via run(forum_dir).

SECURITY MODEL: This server has no authentication. Any client can post as any
author, vote with any voter, redact/archive any post, and propose/approve any
dimension. It is designed for a single trusted Unity pipeline session; do not
expose the port to untrusted clients.

PERSISTENCE: Threads are written to <forum-dir>/*.json and persist until the
unity run dir is cleaned up. There is no automatic pruning. For multi-run
setups, use a separate --forum-dir per run or archive each run's forum/.
"""

import argparse
import fcntl
import json
import math
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastmcp import FastMCP

mcp = FastMCP("unity-forum")
FORUM_DIR: Path = Path("forum")

# @-mentions must be preceded by start-of-string or whitespace, so Lean code
# snippets like `@MeasureTheory.foo` inside post bodies do NOT register as
# mentions and pollute the ICRL ledger.
_MENTION_RE = re.compile(r'(?:^|\s)@([a-zA-Z][\w-]*)')
_DIM_NAME_RE = re.compile(r'^[a-z][a-z0-9_]*$')
_POST_ID_RE = re.compile(r'^[a-f0-9]{8}$')

DEFAULT_DIMENSIONS = [
    "correctness",
    "faithfulness",
]
DIMENSION_APPROVAL_THRESHOLD = 3   # net upvotes on a proposal post to auto-approve
DIMENSIONS_THREAD = "_dimensions"
ARCHIVE_THREAD = "_archive"

# Hot-score time term: signed log10(net_score) + timestamp / HOT_TIME_SCALE.
# 45000s = 12.5h means for short-lived pipeline forums, time dominates and
# "hot" ≈ "new"; for longer-lived forums, accumulated votes start to matter.
HOT_TIME_SCALE = 45000


# ── Locking ───────────────────────────────────────────────────────────────────

@contextmanager
def _thread_lock(thread_id: str):
    """Exclusive per-thread file lock so concurrent votes/posts are serialised."""
    FORUM_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = FORUM_DIR / f"{thread_id}.lock"
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def _config_lock():
    FORUM_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = FORUM_DIR / "_config.lock"
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ── Config ────────────────────────────────────────────────────────────────────

def _config_path() -> Path:
    return FORUM_DIR / "config.json"


def _default_config() -> dict:
    return {"dimensions": {"active": list(DEFAULT_DIMENSIONS), "pending": {}}, "tags": {}}


def _load_config() -> dict:
    path = _config_path()
    if not path.exists():
        return _default_config()
    try:
        cfg = json.loads(path.read_text())
        # Back-fill if a saved config has an empty active list (e.g. legacy file)
        if not cfg.get("dimensions", {}).get("active"):
            cfg.setdefault("dimensions", {})["active"] = list(DEFAULT_DIMENSIONS)
        return cfg
    except Exception:
        return _default_config()


def _save_config(config: dict) -> None:
    FORUM_DIR.mkdir(parents=True, exist_ok=True)
    _config_path().write_text(json.dumps(config, indent=2))


def _active_dimensions() -> list[str]:
    return _load_config()["dimensions"]["active"]


# ── Thread helpers ────────────────────────────────────────────────────────────

def _thread_path(thread_id: str) -> Path:
    return FORUM_DIR / f"{thread_id}.json"


def _load(thread_id: str) -> dict:
    path = _thread_path(thread_id)
    if not path.exists():
        raise ValueError(f"Thread '{thread_id}' does not exist. Call forum_create_thread first.")
    return json.loads(path.read_text())


def _save(data: dict) -> None:
    FORUM_DIR.mkdir(parents=True, exist_ok=True)
    _thread_path(data["thread_id"]).write_text(json.dumps(data, indent=2))


# ── Balances / ICRL ───────────────────────────────────────────────────────────

# Ledger keys are canonical (lowercase, separator-collapsed, common subagent
# suffixes stripped) so `FORMALIZER`, `Formalizer`, and `Formalizer-Subagent`
# all credit the same row. The first display form is preserved in
# `display_name` for human inspection.
_AUTHOR_SUFFIX_RE = re.compile(r'-(subagent|agent|node|worker)$')


def _canonical_author(name: str) -> str:
    """Canonicalize agent identity for ledger purposes."""
    n = (name or "").strip().lower()
    n = re.sub(r"[\s_-]+", "-", n)
    n = _AUTHOR_SUFFIX_RE.sub("", n)
    return n


def _balances_path() -> Path:
    return FORUM_DIR / "balances.json"


def _load_balances() -> dict:
    path = _balances_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_balances(balances: dict) -> None:
    FORUM_DIR.mkdir(parents=True, exist_ok=True)
    _balances_path().write_text(json.dumps(balances, indent=2))


def _credit(author: str, delta: float, event: str, thread_id: str, excerpt: str = "") -> dict:
    key = _canonical_author(author)
    balances = _load_balances()
    if key not in balances:
        balances[key] = {
            "display_name": author,
            "balance": 0.0,
            "history": [],
            "notifications": [],
        }
    rec = balances[key]
    rec.setdefault("display_name", author)
    rec["balance"] = round(rec["balance"] + delta, 2)
    rec["history"].append({
        "event": event,
        "delta": delta,
        "balance_after": rec["balance"],
        "thread_id": thread_id,
        "excerpt": excerpt,
        "timestamp": int(time.time()),
    })
    _save_balances(balances)
    return rec


def _push_notification(author: str, delta: float, event: str, thread_id: str, post_id: str, excerpt: str, *, only_existing: bool = False) -> None:
    key = _canonical_author(author)
    balances = _load_balances()
    if key not in balances:
        if only_existing:
            # Don't materialise a balance row just because someone @mentioned a
            # name we've never seen post. Prevents Lean code snippets and stray
            # @-strings from creating phantom agents.
            return
        balances[key] = {
            "display_name": author,
            "balance": 0.0,
            "history": [],
            "notifications": [],
        }
    rec = balances[key]
    rec.setdefault("display_name", author)
    rec["balance"] = round(rec["balance"] + delta, 2)
    rec["history"].append({
        "event": event,
        "delta": delta,
        "balance_after": rec["balance"],
        "thread_id": thread_id,
        "post_id": post_id,
        "excerpt": excerpt,
        "timestamp": int(time.time()),
    })
    rec["notifications"].append({
        "delta": delta,
        "event": event,
        "thread_id": thread_id,
        "post_id": post_id,
        "excerpt": excerpt,
        "balance_after": rec["balance"],
    })
    _save_balances(balances)


def _notify_all(delta: float, event: str, thread_id: str, post_id: str, excerpt: str, exclude: str = "") -> None:
    """Push a notification to every known agent except `exclude`."""
    excl_key = _canonical_author(exclude) if exclude else ""
    balances = _load_balances()
    for key in list(balances.keys()):
        if key != excl_key:
            display = balances[key].get("display_name", key)
            _push_notification(display, delta, event, thread_id, post_id, excerpt)


def _drain_notifications(author: str) -> list:
    key = _canonical_author(author)
    balances = _load_balances()
    if key not in balances:
        return []
    notifications = balances[key].get("notifications", [])
    balances[key]["notifications"] = []
    _save_balances(balances)
    return notifications


# ── Sorting ───────────────────────────────────────────────────────────────────

def _net_score(post: dict) -> int:
    return post.get("upvotes", 0) - post.get("downvotes", 0)


def _hot(post: dict) -> float:
    """Hot score: signed log10(|score|) + timestamp / HOT_TIME_SCALE."""
    score = _net_score(post)
    sign = 1 if score > 0 else (-1 if score < 0 else 0)
    return math.log10(max(abs(score), 1)) * sign + post["timestamp"] / HOT_TIME_SCALE


def _sorted_posts(posts: list[dict], sort: str) -> list[dict]:
    if sort == "hot":
        return sorted(posts, key=_hot, reverse=True)
    if sort == "new":
        return sorted(posts, key=lambda p: p["timestamp"], reverse=True)
    if sort == "top":
        return sorted(posts, key=_net_score, reverse=True)
    raise ValueError("sort must be 'hot', 'new', or 'top'")


# ── Auto-approve helper ───────────────────────────────────────────────────────

def _maybe_auto_approve(thread_id: str, post_id: str, net_score: int) -> str | None:
    """If thread is _dimensions and net score crosses threshold, activate the dimension."""
    if thread_id != DIMENSIONS_THREAD:
        return None
    if net_score < DIMENSION_APPROVAL_THRESHOLD:
        return None
    with _config_lock():
        config = _load_config()
        pending = config["dimensions"]["pending"]
        for name, proposal in list(pending.items()):
            if proposal.get("proposal_post_id") == post_id:
                config["dimensions"]["active"].append(name)
                del pending[name]
                _save_config(config)
                return name
    return None


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def forum_create_thread(thread_id: str, title: str, description: str = "") -> str:
    """Create a new forum thread.

    Call once per chunk (thread_id='chunk-1', title=declaration name) and once
    for global cross-chunk discussion (thread_id='global', title='Global Discussion').
    The reserved thread '_dimensions' is managed automatically for dimension proposals.
    Returns a confirmation string.
    """
    if _thread_path(thread_id).exists():
        return f"Thread '{thread_id}' already exists."
    _save({
        "thread_id": thread_id,
        "title": title,
        "description": description,
        "created_at": int(time.time()),
        "posts": [],
    })
    return f"Thread '{thread_id}' created."


@mcp.tool()
def forum_post(
    thread_id: str,
    author: str,
    content: str,
    reply_to: list[str] | None = None,
) -> dict:
    """Post a message to a forum thread.

    reply_to is a list of post_ids this post responds to (supports multi-parent
    DAG structure — a synthesis post can reply to several arguments at once).
    Leave empty or omit for a top-level post.

    Returns the new post's metadata including icrl_balance and any pending
    icrl_notifications (vote feedback and @mention alerts since your last post).
    """
    if not author or not author.strip():
        raise ValueError("author must be a non-empty string")
    reply_to = reply_to or []
    bad = [pid for pid in reply_to if not _POST_ID_RE.match(pid)]
    if bad:
        raise ValueError(f"reply_to must contain 8-char lowercase hex post_ids; got {bad}")
    with _thread_lock(thread_id):
        return _forum_post_locked(thread_id, author, content, reply_to)


def _forum_post_locked(thread_id: str, author: str, content: str, reply_to: list[str]) -> dict:
    data = _load(thread_id)
    post = {
        "post_id": uuid.uuid4().hex[:8],
        "author": author,
        "content": content,
        "timestamp": int(time.time()),
        "upvotes": 0,
        "downvotes": 0,
        "votes_by_dimension": {},
        "voter_registry": {},
        "reply_to": reply_to,
        "tags": [],
        "redacted": False,
    }
    data["posts"].append(post)
    _save(data)
    seen_mentions: set[str] = set()
    for m in _MENTION_RE.finditer(content):
        mentioned = m.group(1)
        mentioned_key = _canonical_author(mentioned)
        if mentioned_key == _canonical_author(author) or mentioned_key in seen_mentions:
            continue
        seen_mentions.add(mentioned_key)
        # only_existing=True so phantom @-strings (Lean code that slipped past
        # _MENTION_RE, stray tokens) don't materialise ledger rows.
        _push_notification(
            mentioned, 0.0, "mention", thread_id, post["post_id"],
            content[:100], only_existing=True,
        )
    rec = _credit(author, 0.5, "forum_post", thread_id, content[:100])
    notifications = _drain_notifications(author)
    result = {k: v for k, v in post.items()}
    result["icrl_balance"] = rec["balance"]
    result["icrl_delta"] = +0.5
    if notifications:
        result["icrl_notifications"] = notifications
    return result


@mcp.tool()
def forum_vote(
    thread_id: str,
    post_id: str,
    vote: str,
    voter: str = "unknown",
    dimension: str | None = None,
) -> dict:
    """Vote on a post along a specific quality dimension.

    vote must be 'up', 'down', or 'remove'.
    dimension must be one of the active dimensions (check forum_list for the current set).
    If no dimensions have been configured yet, dimension may be omitted.

    Each voter holds at most one vote per (post, dimension) pair:
    - Same direction again: toggles off.
    - Opposite direction: swaps.
    - 'remove': explicitly clears your vote on that dimension.

    Returns updated vote counts (total and per-dimension) and your icrl_balance.
    """
    if vote not in ("up", "down", "remove"):
        raise ValueError("vote must be 'up', 'down', or 'remove'")
    if not voter or not voter.strip():
        raise ValueError("voter must be a non-empty string")
    if not _POST_ID_RE.match(post_id):
        raise ValueError(f"post_id must be 8-char lowercase hex; got '{post_id}'")
    active = _active_dimensions()
    if active:
        if dimension is None:
            raise ValueError(f"dimension is required. Active dimensions: {active}")
        if dimension not in active:
            raise ValueError(f"Unknown dimension '{dimension}'. Active: {active}")
    dim_key = dimension or "general"
    with _thread_lock(thread_id):
        return _forum_vote_locked(thread_id, post_id, vote, voter, dim_key)


def _forum_vote_locked(thread_id: str, post_id: str, vote: str, voter: str, dim_key: str) -> dict:
    data = _load(thread_id)
    for post in data["posts"]:
        if post["post_id"] == post_id:
            registry = post.setdefault("voter_registry", {})
            vbd = post.setdefault("votes_by_dimension", {})
            reg_key = f"{voter}:{dim_key}"
            existing = registry.get(reg_key)
            excerpt = post["content"][:100]
            author = post["author"]

            remove_old = existing is not None
            add_new = vote != "remove" and vote != existing

            if not remove_old and not add_new:
                balances = _load_balances()
                balance = balances.get(voter, {}).get("balance", 0.0)
                return {
                    "post_id": post_id,
                    "upvotes": post["upvotes"],
                    "downvotes": post["downvotes"],
                    "votes_by_dimension": vbd,
                    "your_vote": None,
                    "dimension": dim_key,
                    "icrl_balance": balance,
                    "icrl_delta": 0,
                    "action": "no_op",
                }

            dim_bucket = vbd.setdefault(dim_key, {"up": 0, "down": 0})
            author_delta = 0.0

            if remove_old:
                if existing == "up":
                    post["upvotes"] -= 1
                    dim_bucket["up"] -= 1
                    author_delta -= 1.0
                else:
                    post["downvotes"] -= 1
                    dim_bucket["down"] -= 1
                    author_delta += 1.0
                del registry[reg_key]

            if add_new:
                registry[reg_key] = vote
                if vote == "up":
                    post["upvotes"] += 1
                    dim_bucket["up"] += 1
                    author_delta += 1.0
                else:
                    post["downvotes"] += 1
                    dim_bucket["down"] += 1
                    author_delta -= 1.0

            _save(data)

            if author_delta != 0.0:
                event = "received_upvote" if author_delta > 0 else "received_downvote"
                _push_notification(author, author_delta, event, thread_id, post_id, excerpt)

            net = post["upvotes"] - post["downvotes"]
            approved_dim = _maybe_auto_approve(thread_id, post_id, net)

            current_vote = registry.get(reg_key)
            action = f"vote_{vote}" if add_new else f"removed_{existing}"
            rec = _credit(voter, 0.5, f"forum_{action}", thread_id)

            result = {
                "post_id": post_id,
                "upvotes": post["upvotes"],
                "downvotes": post["downvotes"],
                "votes_by_dimension": vbd,
                "your_vote": current_vote,
                "dimension": dim_key,
                "icrl_balance": rec["balance"],
                "icrl_delta": +0.5,
                "action": action,
            }
            if approved_dim:
                result["dimension_approved"] = approved_dim
            return result

    raise ValueError(f"Post '{post_id}' not found in thread '{thread_id}'.")


@mcp.tool()
def forum_archive(thread_id: str, post_id: str, reason: str, archiver: str) -> dict:
    """Archive a post: mark it as `[ARCHIVED]` in place, append an audit-trail
    entry to the `_archive` thread, and credit the archiver +0.5.

    Use this for cleanup of stale, mistaken, or superseded posts. Unlike a
    hard delete, the original post stays in the graph with its post_id and
    reply-links intact; the content is replaced with a short summary noting
    the archival. The audit entry on `_archive` records the original
    thread_id, archiver, reason, and a preview of the original content so
    future readers can reconstruct what was removed and why.
    """
    if not archiver or not archiver.strip():
        raise ValueError("archiver must be a non-empty string")
    if not _POST_ID_RE.match(post_id):
        raise ValueError(f"post_id must be 8-char lowercase hex; got '{post_id}'")
    if not reason or not reason.strip():
        raise ValueError("reason must be a non-empty string explaining why the post was archived")

    archived_excerpt = ""
    archived_author = ""
    with _thread_lock(thread_id):
        data = _load(thread_id)
        target = None
        for post in data["posts"]:
            if post["post_id"] == post_id:
                target = post
                break
        if target is None:
            raise ValueError(f"Post '{post_id}' not found in thread '{thread_id}'.")
        if target.get("archived") or target.get("redacted"):
            return {
                "post_id": post_id,
                "status": "already_archived",
                "archiver": archiver,
            }
        archived_excerpt = target.get("content", "")[:200]
        archived_author = target.get("author", "")
        target["archived"] = True
        target["archived_at"] = int(time.time())
        target["archived_by"] = archiver
        target["archive_reason"] = reason
        target["content"] = f"[ARCHIVED] {reason}"
        _save(data)

    # Audit-trail post on _archive (best-effort; failure shouldn't undo the archival)
    if not _thread_path(ARCHIVE_THREAD).exists():
        _save({
            "thread_id": ARCHIVE_THREAD,
            "title": "Archived Posts",
            "description": "Audit trail of posts archived via forum_archive. One entry per archival.",
            "created_at": int(time.time()),
            "posts": [],
        })
    audit_content = (
        f"Archived post `{post_id}` from thread `{thread_id}` "
        f"(originally by `{archived_author}`).\n"
        f"**Reason:** {reason}\n\n"
        f"**Original content preview:** {archived_excerpt!r}"
    )
    with _thread_lock(ARCHIVE_THREAD):
        _forum_post_locked(ARCHIVE_THREAD, archiver, audit_content, [])

    rec = _credit(archiver, 0.5, "forum_archive", thread_id, reason[:100])
    return {
        "post_id": post_id,
        "status": "archived",
        "archiver": archiver,
        "icrl_balance": rec["balance"],
        "icrl_delta": +0.5,
    }


# ── Scratchpad: structured attempt logging ────────────────────────────────────
# The forum doubles as an external working-memory for chunk formalization
# attempts so successor agents don't re-derive dead ends. forum_log_attempt
# writes a small structured record per attempt; forum_chunk_history reads
# just those records, newest-first, for one chunk.

_ATTEMPT_OUTCOMES = {
    "success",          # build succeeded, goal closed
    "compile_error",    # lake build failed
    "goal_unchanged",   # tactic ran but goal didn't move
    "timeout",          # build / lean LSP timed out
    "gave_up",          # agent decided this approach won't work
    "partial",          # progress but not closed
}


@mcp.tool()
def forum_log_attempt(
    chunk_id: str,
    author: str,
    what: str,
    outcome: str,
    error: str = "",
    notes: str = "",
) -> dict:
    """Log a structured attempt at a chunk's formalization or proof.

    Writes to the chunk's thread (`chunk-<chunk_id>`, auto-created) as an
    `attempt`-tagged post with the structured fields stored on the post.

    `outcome` must be one of: success, compile_error, goal_unchanged,
    timeout, gave_up, partial.

    `what` is a one-line description of the approach (e.g. "induction on n;
    simp [List.foldr_cons] in inductive step"). Be concrete so future
    readers can pattern-match.

    `error` and `notes` are optional. Include the verbatim error head if
    outcome is compile_error so search across attempts can match it.

    Earns +0.2 ICRL (lower than forum_post so frequent fine-grained logs
    are encouraged). Returns the post_id and current balance.
    """
    if outcome not in _ATTEMPT_OUTCOMES:
        raise ValueError(
            f"outcome must be one of {sorted(_ATTEMPT_OUTCOMES)}; got {outcome!r}"
        )
    if not author or not author.strip():
        raise ValueError("author must be a non-empty string")
    if not chunk_id or not chunk_id.strip():
        raise ValueError("chunk_id must be a non-empty string")
    if not what or not what.strip():
        raise ValueError("what must describe what was tried")

    thread_id = f"chunk-{chunk_id}"
    if not _thread_path(thread_id).exists():
        _save({
            "thread_id": thread_id,
            "title": f"Chunk {chunk_id}",
            "description": f"Per-chunk coordination + attempt log for {chunk_id}.",
            "created_at": int(time.time()),
            "posts": [],
        })

    body_parts = [f"**Attempt** — outcome: `{outcome}`", "", f"**Tried:** {what}"]
    if error:
        body_parts.extend(["", f"**Error:**\n```\n{error}\n```"])
    if notes:
        body_parts.extend(["", f"**Notes:** {notes}"])
    body = "\n".join(body_parts)

    with _thread_lock(thread_id):
        data = _load(thread_id)
        post = {
            "post_id": uuid.uuid4().hex[:8],
            "author": author,
            "content": body,
            "timestamp": int(time.time()),
            "upvotes": 0,
            "downvotes": 0,
            "votes_by_dimension": {},
            "voter_registry": {},
            "reply_to": [],
            "tags": ["attempt"],
            "archived": False,
            "attempt": {
                "chunk_id": chunk_id,
                "what": what,
                "outcome": outcome,
                "error": error,
                "notes": notes,
            },
        }
        data["posts"].append(post)
        _save(data)

    rec = _credit(author, 0.2, "forum_log_attempt", thread_id, what[:100])
    return {
        "post_id": post["post_id"],
        "thread_id": thread_id,
        "chunk_id": chunk_id,
        "outcome": outcome,
        "icrl_balance": rec["balance"],
        "icrl_delta": +0.2,
    }


@mcp.tool()
def forum_chunk_history(chunk_id: str, limit: int = 10) -> dict:
    """Return the latest structured attempts for a chunk, newest first.

    Filters the chunk's thread (`chunk-<chunk_id>`) for `attempt`-tagged
    posts and returns only those, with the structured fields surfaced.
    `limit` caps how many are returned (default 10).

    Call this BEFORE planning a new approach on a chunk that's been worked
    on before — if your intended tactic is already marked failed in the
    history, pick a different one or read the failure notes first.
    """
    if not chunk_id or not chunk_id.strip():
        raise ValueError("chunk_id must be a non-empty string")
    if limit <= 0:
        raise ValueError("limit must be positive")

    thread_id = f"chunk-{chunk_id}"
    if not _thread_path(thread_id).exists():
        return {
            "chunk_id": chunk_id,
            "thread_id": thread_id,
            "attempt_count": 0,
            "attempts": [],
        }

    data = _load(thread_id)
    attempts = []
    for post in reversed(data.get("posts", [])):
        if "attempt" not in post.get("tags", []):
            continue
        if post.get("archived"):
            continue
        record = post.get("attempt")
        if not record:
            continue
        attempts.append({
            "post_id": post["post_id"],
            "author": post["author"],
            "timestamp": post["timestamp"],
            "chunk_id": record.get("chunk_id", chunk_id),
            "what": record.get("what", ""),
            "outcome": record.get("outcome", ""),
            "error": record.get("error", ""),
            "notes": record.get("notes", ""),
        })
        if len(attempts) >= limit:
            break
    return {
        "chunk_id": chunk_id,
        "thread_id": thread_id,
        "attempt_count": len(attempts),
        "attempts": attempts,
    }


@mcp.tool()
def forum_read(thread_id: str, sort: str = "hot") -> dict:
    """Read a forum thread.

    sort: 'hot' (default), 'new', or 'top'.
    Returns thread metadata, active dimensions, and posts with per-dimension vote breakdowns.
    """
    data = _load(thread_id)
    return {
        "thread_id": data["thread_id"],
        "title": data["title"],
        "description": data["description"],
        "created_at": data["created_at"],
        "post_count": len(data["posts"]),
        "active_dimensions": _active_dimensions(),
        "posts": _sorted_posts(data["posts"], sort),
        "sort": sort,
    }


@mcp.tool()
def forum_check_balance(author: str, drain: bool = True) -> dict:
    """Check your ICRL balance and full trajectory.

    By default, pending notifications are drained when returned (read-once
    semantics). Pass drain=False to peek without consuming — useful for
    a watchdog or read-only audit that should not affect agent state.
    """
    if not author or not author.strip():
        raise ValueError("author must be a non-empty string")
    key = _canonical_author(author)
    balances = _load_balances()
    if key not in balances:
        return {"author": author, "balance": 0.0, "history": [], "pending_notifications": []}
    rec = balances[key]
    result = {
        "author": rec.get("display_name", author),
        "balance": rec["balance"],
        "history": rec["history"],
        "pending_notifications": list(rec.get("notifications", [])),
    }
    if drain and result["pending_notifications"]:
        rec["notifications"] = []
        _save_balances(balances)
    return result


@mcp.tool()
def forum_list() -> dict:
    """List all forum threads, active dimensions, pending proposals, tags, and ICRL leaderboard."""
    FORUM_DIR.mkdir(parents=True, exist_ok=True)
    threads = []
    for path in sorted(FORUM_DIR.glob("*.json")):
        if path.name in ("balances.json", "config.json"):
            continue
        try:
            data = json.loads(path.read_text())
            last_activity = max(
                (p["timestamp"] for p in data["posts"]),
                default=data["created_at"],
            )
            threads.append({
                "thread_id": data["thread_id"],
                "title": data["title"],
                "description": data["description"],
                "post_count": len(data["posts"]),
                "last_activity": last_activity,
                "pinned": data["thread_id"] == DIMENSIONS_THREAD,
            })
        except Exception:
            continue

    config = _load_config()
    balances = _load_balances()
    leaderboard = sorted(
        [
            {"author": r.get("display_name", a), "balance": r["balance"]}
            for a, r in balances.items()
        ],
        key=lambda x: x["balance"],
        reverse=True,
    ) if balances else []

    return {
        "threads": threads,
        "active_dimensions": config["dimensions"]["active"],
        "pending_dimensions": {
            name: {"description": p["description"], "proposed_by": p["proposed_by"]}
            for name, p in config["dimensions"]["pending"].items()
        },
        "tags": {
            name: {"description": t["description"], "post_count": len(t["post_ids"])}
            for name, t in config.get("tags", {}).items()
        },
        "leaderboard": leaderboard,
    }


# ── Dimension management ───────────────────────────────────────────────────────

@mcp.tool()
def forum_set_dimensions(dimensions: list[str], allow_orphan: bool = False) -> dict:
    """Set the canonical vote dimensions for this run (call once at pipeline start).

    Each dimension name must be lowercase alphanumeric with underscores (e.g. 'correctness').
    If not called, the default set is used: correctness, faithfulness, style_alignment,
    priority, confidence, feasibility.

    This replaces any previously active dimensions. If a previously-active
    dimension has cast votes and is not in the new list, the call is rejected
    unless allow_orphan=True. Orphaned dimensions stay in old posts' vote
    histories but new votes use only the new active set.
    """
    for d in dimensions:
        if not _DIM_NAME_RE.match(d):
            raise ValueError(f"Invalid dimension name '{d}'. Use lowercase letters, digits, underscores.")
    with _config_lock():
        config = _load_config()
        prev = set(config["dimensions"]["active"])
        new = set(dimensions)
        removed = prev - new
        if removed and not allow_orphan:
            orphaned = [d for d in removed if _dimension_has_votes(d)]
            if orphaned:
                raise ValueError(
                    f"Dimensions {orphaned} have cast votes; pass allow_orphan=True to override."
                )
        config["dimensions"]["active"] = list(dimensions)
        _save_config(config)
    return {"active_dimensions": dimensions}


def _dimension_has_votes(dim: str) -> bool:
    """True if any post anywhere has a non-empty vote tally for `dim`."""
    for path in FORUM_DIR.glob("*.json"):
        if path.name in ("balances.json", "config.json"):
            continue
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        for post in data.get("posts", []):
            bucket = post.get("votes_by_dimension", {}).get(dim)
            if bucket and (bucket.get("up", 0) > 0 or bucket.get("down", 0) > 0):
                return True
    return False


@mcp.tool()
def forum_propose_dimension(name: str, description: str, proposed_by: str) -> dict:
    """Propose a new vote dimension for adoption by the community.

    Creates a post in the '_dimensions' thread visible to all agents, who will be
    notified and can vote and reply. If the proposal post reaches a net score of
    {threshold} upvotes, the dimension is automatically activated.

    name must be lowercase alphanumeric with underscores.
    """.format(threshold=DIMENSION_APPROVAL_THRESHOLD)
    if not _DIM_NAME_RE.match(name):
        raise ValueError(f"Invalid dimension name '{name}'. Use lowercase letters, digits, underscores.")
    if not proposed_by or not proposed_by.strip():
        raise ValueError("proposed_by must be a non-empty string")

    # Ensure the _dimensions thread exists before taking any lock. Idempotent;
    # a concurrent racer either finds the file already there (skip) or both
    # write equivalent content. `_save` does a single write so torn reads on
    # the subsequent _forum_post_locked are impossible.
    if not _thread_path(DIMENSIONS_THREAD).exists():
        _save({
            "thread_id": DIMENSIONS_THREAD,
            "title": "Dimension Proposals",
            "description": "Propose and vote on new vote dimensions. A proposal auto-activates at net +3.",
            "created_at": int(time.time()),
            "posts": [],
        })

    # Lock order: thread → config. Matches forum_vote's order so the two
    # operations cannot deadlock on the _dimensions thread.
    with _thread_lock(DIMENSIONS_THREAD):
        with _config_lock():
            config = _load_config()
            if name in config["dimensions"]["active"]:
                return {"status": "already_active", "name": name}
            if name in config["dimensions"]["pending"]:
                return {"status": "already_pending", "name": name}

            proposal_post = _forum_post_locked(
                DIMENSIONS_THREAD,
                proposed_by,
                f"**Dimension proposal: `{name}`**\n\n{description}\n\n"
                f"Upvote to adopt, downvote to reject. Auto-activates at net +{DIMENSION_APPROVAL_THRESHOLD}.",
                [],
            )

            proposal_post_id = proposal_post["post_id"]
            config["dimensions"]["pending"][name] = {
                "description": description,
                "proposed_by": proposed_by,
                "proposal_post_id": proposal_post_id,
                "timestamp": int(time.time()),
            }
            _save_config(config)

    # Notify all known agents
    _notify_all(
        0.0, "dimension_proposed", DIMENSIONS_THREAD, proposal_post_id,
        f"New dimension proposed: '{name}' — {description[:80]}",
        exclude=proposed_by,
    )

    return {
        "status": "pending",
        "name": name,
        "proposal_post_id": proposal_post_id,
        "message": f"Proposal posted to '{DIMENSIONS_THREAD}'. All agents notified. Auto-activates at net +{DIMENSION_APPROVAL_THRESHOLD}.",
    }


@mcp.tool()
def forum_approve_dimension(name: str) -> dict:
    """Manually activate a pending dimension proposal (coordinator / main agent use).

    Agents may also activate a dimension by upvoting its proposal post to net +{threshold}.
    """.format(threshold=DIMENSION_APPROVAL_THRESHOLD)
    with _config_lock():
        config = _load_config()
        pending = config["dimensions"]["pending"]
        if name not in pending:
            active = config["dimensions"]["active"]
            if name in active:
                return {"status": "already_active", "name": name}
            raise ValueError(f"No pending proposal for dimension '{name}'.")
        config["dimensions"]["active"].append(name)
        del pending[name]
        _save_config(config)
    return {"status": "activated", "name": name, "active_dimensions": config["dimensions"]["active"]}


# ── Tags ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def forum_tag(
    name: str,
    post_ids: list[str],
    description: str = "",
    tagger: str = "unknown",
) -> dict:
    """Create or update a named concept tag linking posts across threads.

    Tags are hyperedges: one named concept (e.g. 'IFT-bridge-gap') that groups
    related posts regardless of which thread they live in. Each post_id in the
    list is added to the tag (duplicates ignored). Existing tags are extended,
    not replaced — to build up a tag incrementally across multiple calls.

    name may contain letters, digits, hyphens, and underscores.
    """
    if not re.match(r'^[\w-]+$', name):
        raise ValueError("Tag name may only contain letters, digits, hyphens, and underscores.")
    if not tagger or not tagger.strip():
        raise ValueError("tagger must be a non-empty string")
    invalid = [pid for pid in post_ids if not _POST_ID_RE.match(pid)]
    if invalid:
        raise ValueError(f"post_ids must be 8-char lowercase hex; got invalid: {invalid}")
    with _config_lock():
        config = _load_config()
        tags = config.setdefault("tags", {})
        if name not in tags:
            tags[name] = {
                "description": description or "",
                "post_ids": [],
                "created_by": tagger,
                "created_at": int(time.time()),
                "updated_at": int(time.time()),
            }
        tag = tags[name]
        existing = set(tag["post_ids"])
        added = [pid for pid in post_ids if pid not in existing]
        tag["post_ids"] = list(existing | set(post_ids))
        tag["updated_at"] = int(time.time())
        if description:
            tag["description"] = description
        _save_config(config)

    # Stamp tag onto the post objects in their threads
    for pid in added:
        _stamp_tag_on_post(pid, name)

    return {
        "tag": name,
        "description": tag["description"],
        "post_count": len(tag["post_ids"]),
        "added": len(added),
    }


def _stamp_tag_on_post(post_id: str, tag_name: str) -> None:
    """Add tag_name to the post's tags list in its thread file.

    Two-phase: (1) cheap out-of-lock scan to locate the owning thread,
    (2) re-read under the per-thread lock before mutating, so concurrent
    forum_post writes between scan and mutation aren't clobbered.
    """
    for path in FORUM_DIR.glob("*.json"):
        if path.name in ("balances.json", "config.json"):
            continue
        try:
            preview = json.loads(path.read_text())
        except Exception:
            continue
        if not any(p["post_id"] == post_id for p in preview.get("posts", [])):
            continue
        thread_id = preview["thread_id"]
        with _thread_lock(thread_id):
            try:
                data = json.loads(path.read_text())  # re-read inside the lock
            except Exception:
                return
            for post in data["posts"]:
                if post["post_id"] == post_id:
                    tags = post.setdefault("tags", [])
                    if tag_name not in tags:
                        tags.append(tag_name)
                        path.write_text(json.dumps(data, indent=2))
                    return


@mcp.tool()
def forum_get_tag(name: str) -> dict:
    """Retrieve all posts associated with a tag, across all threads.

    Returns tag metadata and the full content of every tagged post,
    sorted by hot score.
    """
    config = _load_config()
    tags = config.get("tags", {})
    if name not in tags:
        raise ValueError(f"Tag '{name}' does not exist.")
    tag = tags[name]
    post_ids = set(tag["post_ids"])
    posts = []
    for path in sorted(FORUM_DIR.glob("*.json")):
        if path.name in ("balances.json", "config.json"):
            continue
        try:
            data = json.loads(path.read_text())
            for post in data["posts"]:
                if post["post_id"] in post_ids:
                    posts.append({**post, "thread_id": data["thread_id"], "thread_title": data["title"]})
        except Exception:
            continue
    posts = sorted(posts, key=_hot, reverse=True)
    return {
        "tag": name,
        "description": tag["description"],
        "created_by": tag["created_by"],
        "created_at": tag["created_at"],
        "post_count": len(posts),
        "posts": posts,
    }


# ── Entry point ───────────────────────────────────────────────────────────────



# ── Typed workspace (speech acts + ledger + brief) ────────────────────────────
# Free-form posts remain (forum_post = NOTE). The acts below are structured
# artifacts with guaranteed consumers: DECISION/HANDOFF are read at every later
# phase (legacy tags preserved), OBSTACLE/QUESTION surface in forum_brief and the
# dispatch digest, RESULT feeds consensus/merging, the ledger feeds sign-ups.

def _ensure_thread(thread_id: str, title: str) -> None:
    if not _thread_path(thread_id).exists():
        _save({"thread_id": thread_id, "title": title, "description": "",
               "created_at": int(time.time()), "posts": []})


def _typed_post(thread_id: str, title: str, author: str, act: str, fields: dict,
                content: str, reply_to: list[str] | None = None) -> dict:
    with _thread_lock(thread_id):
        _ensure_thread(thread_id, title)
        post = _forum_post_locked(thread_id, author, content, reply_to or [])
        data = _load(thread_id)
        for p in data["posts"]:
            if p["post_id"] == post["post_id"]:
                p["act"] = act
                p["fields"] = fields
                break
        _save(data)
    return {"post_id": post["post_id"], "thread_id": thread_id, "act": act}


def _acts(thread_id: str, act: str | None = None) -> list[dict]:
    if not _thread_path(thread_id).exists():
        return []
    posts = _load(thread_id).get("posts", [])
    return [p for p in posts if not act or p.get("act") == act]


def _chunk_thread(chunk: str) -> str:
    return chunk if chunk.startswith("chunk-") else f"chunk-{chunk}"


@mcp.tool()
def forum_claim(chunk: str, author: str, strategy: str = "") -> dict:
    """Claim a chunk you are signing up to work on, stating your intended strategy.
    Other agents see open claims in forum_brief and avoid duplicating the chunk/strategy.
    Your later forum_result on this chunk auto-links to (and closes) this claim."""
    tid = _chunk_thread(chunk)
    return _typed_post(tid, chunk, author, "claim",
                       {"chunk": chunk, "strategy": strategy, "status": "open"},
                       f"CLAIM {chunk}" + (f" — strategy: {strategy}" if strategy else ""))


@mcp.tool()
def forum_result(chunk: str, author: str, status: str, build_ok: bool = False,
                 decl_names: list[str] | None = None, error_sig: str = "",
                 notes: str = "") -> dict:
    """Report the outcome of your work on a chunk. status: 'done' | 'partial' | 'failed'.
    build_ok means the chunk builds sorry-free. Auto-links to your open claim, closes it,
    and (if build_ok) resolves the chunk's open obstacles. Teammates endorse/object on
    this result and the primary merges from forum_consensus."""
    tid = _chunk_thread(chunk)
    reply = []
    with _thread_lock(tid):
        _ensure_thread(tid, chunk)
        data = _load(tid)
        for p in data["posts"]:
            f = p.get("fields") or {}
            if p.get("act") == "claim" and p["author"] == author and f.get("status") == "open":
                reply = [p["post_id"]]
                f["status"] = "resolved"
            if build_ok and p.get("act") == "obstacle" and (p.get("fields") or {}).get("status") == "open":
                p["fields"]["status"] = "resolved"
        _save(data)
    content = (f"RESULT {chunk}: {status}" + (" [builds sorry-free]" if build_ok else "")
               + (f" decls={decl_names}" if decl_names else "")
               + (f" error: {error_sig[:160]}" if error_sig else "")
               + (f" — {notes[:300]}" if notes else ""))
    return _typed_post(tid, chunk, author, "result",
                       {"chunk": chunk, "status": status, "build_ok": build_ok,
                        "decl_names": decl_names or [], "error_sig": error_sig,
                        "endorsements": [], "objections": []},
                       content, reply)


@mcp.tool()
def forum_obstacle(chunk: str, author: str, goal_state: str,
                   tried: list[str] | None = None, hypothesis: str = "") -> dict:
    """Report a concrete obstacle on a chunk: the goal state you cannot close, what you
    tried, and your hypothesis. Open obstacles are pushed to every teammate's brief so
    anyone with the missing lemma/technique can respond. Resolved automatically when a
    build_ok result lands on the chunk."""
    tid = _chunk_thread(chunk)
    content = (f"OBSTACLE {chunk}: {goal_state[:300]}"
               + (f" | tried: {', '.join(tried or [])[:200]}" if tried else "")
               + (f" | hypothesis: {hypothesis[:200]}" if hypothesis else ""))
    return _typed_post(tid, chunk, author, "obstacle",
                       {"chunk": chunk, "goal_state": goal_state, "tried": tried or [],
                        "hypothesis": hypothesis, "status": "open"}, content)


@mcp.tool()
def forum_question(author: str, body: str, to: str = "", chunk: str = "") -> dict:
    """Ask the team (or a specific agent via `to`) a question. Open questions addressed
    to an agent appear in their brief; answer with forum_answer(question_id, ...)."""
    return _typed_post("qa", "Questions & Answers", author, "question",
                       {"to": to, "chunk": chunk, "status": "open"},
                       f"QUESTION{f' @{to}' if to else ''}{f' [{chunk}]' if chunk else ''}: {body}")


@mcp.tool()
def forum_answer(question_id: str, author: str, body: str) -> dict:
    """Answer an open question by its post_id (auto-threads to it and closes it)."""
    with _thread_lock("qa"):
        _ensure_thread("qa", "Questions & Answers")
        data = _load("qa")
        for p in data["posts"]:
            if p["post_id"] == question_id and p.get("act") == "question":
                (p.get("fields") or {}).update(status="answered")
        _save(data)
    return _typed_post("qa", "Questions & Answers", author, "answer",
                       {"question_id": question_id}, f"ANSWER: {body}", [question_id])


@mcp.tool()
def forum_decision(author: str, topic: str, choice: str, rationale: str = "") -> dict:
    """Record a binding cross-cutting decision (one per topic; a newer decision on the
    same topic supersedes). Injected into every later phase's briefs; also carries the
    legacy 'decision' tag so forum_get_tag('decision') keeps working."""
    r = _typed_post("global", "Global Discussion", author, "decision",
                    {"topic": topic, "choice": choice, "rationale": rationale},
                    f"DECISION [{topic}]: {choice}" + (f" — {rationale[:300]}" if rationale else ""))
    forum_tag(name="decision", post_ids=[r["post_id"]], tagger=author)
    return r


@mcp.tool()
def forum_handoff(author: str, phase: str, changed: str, open_items: str = "",
                  commitments: str = "") -> dict:
    """End-of-phase handoff: what changed on disk, what is open, what later phases must
    honor. Injected into later phases' briefs; carries the legacy 'phase-handoff' tag."""
    r = _typed_post("global", "Global Discussion", author, "handoff",
                    {"phase": phase, "changed": changed, "open": open_items,
                     "commitments": commitments},
                    f"HANDOFF [{phase}]: {changed[:300]}"
                    + (f" | open: {open_items[:200]}" if open_items else "")
                    + (f" | commitments: {commitments[:200]}" if commitments else ""))
    forum_tag(name="phase-handoff", post_ids=[r["post_id"]], tagger=author)
    return r


def _mutate_result(chunk: str, result_id: str, fn) -> bool:
    tid = _chunk_thread(chunk)
    ok = False
    with _thread_lock(tid):
        if not _thread_path(tid).exists():
            return False
        data = _load(tid)
        for p in data["posts"]:
            if p["post_id"] == result_id and p.get("act") == "result":
                fn(p.setdefault("fields", {}))
                ok = True
        _save(data)
    return ok


@mcp.tool()
def forum_endorse(chunk: str, result_id: str, author: str) -> dict:
    """Endorse a result on a chunk (you checked it: correct + faithful). Counts toward
    merge quorum in forum_consensus; also records a legacy correctness upvote."""
    forum_vote(_chunk_thread(chunk), result_id, "up", author, "correctness")
    if not _mutate_result(chunk, result_id, lambda f: f.setdefault("endorsements", []).append(author)
                          if author not in f.get("endorsements", []) else None):
        raise ValueError(f"no result {result_id} on {chunk}")
    return {"result_id": result_id, "endorsed_by": author}


@mcp.tool()
def forum_object(chunk: str, result_id: str, author: str, reason: str) -> dict:
    """Object to a result with a concrete reason. An OPEN objection blocks the merge of
    that result until resolved (forum_resolve_objection) — objections are how the team
    stops an unfaithful or broken chunk from landing."""
    forum_vote(_chunk_thread(chunk), result_id, "down", author, "correctness")
    if not _mutate_result(chunk, result_id, lambda f: f.setdefault("objections", []).append(
            {"by": author, "reason": reason, "status": "open"})):
        raise ValueError(f"no result {result_id} on {chunk}")
    return {"result_id": result_id, "objection_by": author}


@mcp.tool()
def forum_resolve_objection(chunk: str, result_id: str, objector: str, resolution: str) -> dict:
    """Mark an objection resolved (by the objector after their concern is addressed, or
    by the primary with justification)."""
    def fix(f):
        for o in f.get("objections", []):
            if o["by"] == objector and o["status"] == "open":
                o["status"] = "resolved"
                o["resolution"] = resolution
    if not _mutate_result(chunk, result_id, fix):
        raise ValueError(f"no result {result_id} on {chunk}")
    return {"result_id": result_id, "resolved": objector}


@mcp.tool()
def forum_consensus(chunk: str) -> dict:
    """Merge dashboard for a chunk: every result with its endorsement count and open
    objections. The primary merges the winning result only if it has >=1 endorsement and
    0 open objections (override allowed but must be posted as a DECISION)."""
    out = []
    for p in _acts(_chunk_thread(chunk), "result"):
        f = p.get("fields") or {}
        out.append({"result_id": p["post_id"], "author": p["author"],
                    "status": f.get("status"), "build_ok": f.get("build_ok"),
                    "endorsements": f.get("endorsements", []),
                    "open_objections": [o for o in f.get("objections", []) if o["status"] == "open"]})
    return {"chunk": chunk, "results": out}


@mcp.tool()
def ledger_add(author: str, kind: str, title: str, content: str, evidence: str,
               goal_shape: str = "", chunk: str = "") -> dict:
    """Add a verified knowledge artifact to the run ledger. kind: 'lemma' (a compiling
    declaration teammates can reuse), 'tactic' (a recipe + the goal shape it closes), or
    'failure' (an approach that provably doesn't work and why). `evidence` is REQUIRED —
    build output, error text, or the compiling snippet itself; unverified folklore is
    rejected. Ledger entries reach whoever signs up for related chunks."""
    if kind not in ("lemma", "tactic", "failure"):
        raise ValueError("kind must be lemma | tactic | failure")
    if not evidence or not evidence.strip():
        raise ValueError("evidence is required (build output / error text / compiling snippet)")
    return _typed_post("ledger", "Run Ledger", author, "ledger",
                       {"kind": kind, "title": title, "goal_shape": goal_shape,
                        "chunk": chunk, "evidence": evidence[:2000]},
                       f"LEDGER[{kind}] {title}: {content[:500]}")


@mcp.tool()
def ledger_get(query: str = "", chunk: str = "", limit: int = 8) -> dict:
    """Retrieve ledger entries, newest first, optionally filtered by substring match on
    title/goal_shape/content (`query`) and/or by chunk."""
    entries = []
    for p in reversed(_acts("ledger", "ledger")):
        f = p.get("fields") or {}
        hay = f"{f.get('title','')} {f.get('goal_shape','')} {p.get('content','')}".lower()
        if query and query.lower() not in hay:
            continue
        if chunk and f.get("chunk") != chunk:
            continue
        entries.append({"id": p["post_id"], "author": p["author"], "kind": f.get("kind"),
                        "title": f.get("title"), "goal_shape": f.get("goal_shape"),
                        "content": p.get("content"), "evidence": f.get("evidence", "")[:500]})
        if len(entries) >= limit:
            break
    return {"entries": entries}


def build_brief(author: str, chunk: str = "") -> str:
    """Compact digest of the workspace for one agent (also used by the dispatch
    injection). Empty string when there is nothing to show (e.g. a fresh run)."""
    lines: list[str] = []
    decisions = _acts("global", "decision")
    if decisions:
        latest: dict[str, dict] = {}
        for p in decisions:
            latest[(p.get("fields") or {}).get("topic", "?")] = p
        lines.append("Binding decisions:")
        lines += [f"  - {p['content'][:180]}" for p in list(latest.values())[-6:]]
    handoffs = _acts("global", "handoff")
    if handoffs:
        lines.append("Latest handoff:")
        lines.append(f"  - {handoffs[-1]['content'][:220]}")
    if chunk:
        tid = _chunk_thread(chunk)
        obstacles = [p for p in _acts(tid, "obstacle") if (p.get("fields") or {}).get("status") == "open"]
        if obstacles:
            lines.append(f"Open obstacles on {chunk}:")
            lines += [f"  - {p['content'][:200]}" for p in obstacles[-4:]]
        claims = [p for p in _acts(tid, "claim") if (p.get("fields") or {}).get("status") == "open"]
        if claims:
            lines.append(f"Open claims on {chunk}: " +
                         ", ".join(f"{p['author']} ({(p.get('fields') or {}).get('strategy','')[:40]})"
                                   for p in claims[-4:]))
    me = _canonical_author(author)
    open_qs = [p for p in _acts("qa", "question")
               if (p.get("fields") or {}).get("status") == "open"
               and (_canonical_author((p.get("fields") or {}).get("to", "")) == me
                    or (chunk and (p.get("fields") or {}).get("chunk") == chunk))]
    if open_qs:
        lines.append("Open questions for you:")
        lines += [f"  - [{p['post_id']}] {p['content'][:180]}" for p in open_qs[-4:]]
    ledger = _acts("ledger", "ledger")
    if ledger:
        picks = [p for p in ledger if chunk and (p.get("fields") or {}).get("chunk") == chunk] or ledger
        lines.append("Ledger (verified reusable knowledge; ledger_get for more):")
        lines += [f"  - {p['content'][:180]}" for p in picks[-5:]]
    return "\n".join(lines)


@mcp.tool()
def forum_brief(author: str, chunk: str = "") -> str:
    """One-call compact digest: binding decisions, the latest handoff, open obstacles and
    claims on your chunk, questions addressed to you, and relevant ledger entries. Call
    this instead of reading raw threads."""
    return build_brief(author, chunk) or "(workspace empty — nothing binding yet)"


@mcp.tool()
def forum_stats() -> dict:
    """Communication telemetry: post counts by act type and by author (for benchmark
    ablations)."""
    by_act: dict[str, int] = {}
    by_author: dict[str, int] = {}
    for tp in FORUM_DIR.glob("*.json"):
        if tp.name.startswith("_") or tp.name in ("config.json", "balances.json"):
            continue
        try:
            posts = json.loads(tp.read_text()).get("posts", [])
        except (json.JSONDecodeError, OSError):
            continue
        for p in posts:
            by_act[p.get("act", "note")] = by_act.get(p.get("act", "note"), 0) + 1
            by_author[p.get("author", "?")] = by_author.get(p.get("author", "?"), 0) + 1
    return {"by_act": by_act, "by_author": by_author}


# ── entrypoint (must stay at end of module so all tools above are registered) ──

def run(forum_dir: Path) -> None:
    """Start the MCP server against `forum_dir` (programmatic entry point)."""
    global FORUM_DIR
    FORUM_DIR = Path(forum_dir)
    FORUM_DIR.mkdir(parents=True, exist_ok=True)
    mcp.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Unity Forum MCP Server")
    parser.add_argument("--forum-dir", default="forum", help="Directory for forum thread files")
    args = parser.parse_args()
    run(args.forum_dir)


if __name__ == "__main__":
    main()
