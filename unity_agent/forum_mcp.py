"""Unity Forum MCP Server.

File-backed forum for formalization agents. Each thread is stored as
forum/<thread_id>.json. Config (dimensions, tags) lives in forum/config.json.

Run via:
    python -m unity_agent.forum_mcp --forum-dir <path>
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

_MENTION_RE = re.compile(r'@([\w][\w-]*)')
_DIM_NAME_RE = re.compile(r'^[a-z][a-z0-9_]*$')

DEFAULT_DIMENSIONS = [
    "correctness",
    "faithfulness",
    "style_alignment",
    "priority",
    "confidence",
    "feasibility",
]
DIMENSION_APPROVAL_THRESHOLD = 3   # net upvotes on a proposal post to auto-approve
DIMENSIONS_THREAD = "_dimensions"


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
    balances = _load_balances()
    if author not in balances:
        balances[author] = {"balance": 0.0, "history": [], "notifications": []}
    rec = balances[author]
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


def _push_notification(author: str, delta: float, event: str, thread_id: str, post_id: str, excerpt: str) -> None:
    balances = _load_balances()
    if author not in balances:
        balances[author] = {"balance": 0.0, "history": [], "notifications": []}
    rec = balances[author]
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
    balances = _load_balances()
    for author in list(balances.keys()):
        if author != exclude:
            _push_notification(author, delta, event, thread_id, post_id, excerpt)


def _drain_notifications(author: str) -> list:
    balances = _load_balances()
    if author not in balances:
        return []
    notifications = balances[author].get("notifications", [])
    balances[author]["notifications"] = []
    _save_balances(balances)
    return notifications


# ── Sorting ───────────────────────────────────────────────────────────────────

def _net_score(post: dict) -> int:
    return post.get("upvotes", 0) - post.get("downvotes", 0)


def _hot(post: dict) -> float:
    score = _net_score(post)
    sign = 1 if score > 0 else (-1 if score < 0 else 0)
    return math.log10(max(abs(score), 1)) * sign + post["timestamp"] / 45000


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
    with _thread_lock(thread_id):
        return _forum_post_locked(thread_id, author, content, reply_to or [])


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
    for m in _MENTION_RE.finditer(content):
        mentioned = m.group(1)
        if mentioned != author:
            _push_notification(mentioned, 0.0, "mention", thread_id, post["post_id"], content[:100])
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
def forum_redact(thread_id: str, post_id: str) -> str:
    """Mark a post as [REDACTED]. Content is hidden but the post remains in the graph."""
    with _thread_lock(thread_id):
        data = _load(thread_id)
        for post in data["posts"]:
            if post["post_id"] == post_id:
                post["redacted"] = True
                post["content"] = "[REDACTED]"
                _save(data)
                return f"Post '{post_id}' redacted."
    raise ValueError(f"Post '{post_id}' not found in thread '{thread_id}'.")


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
def forum_check_balance(author: str) -> dict:
    """Check your ICRL balance and full trajectory."""
    balances = _load_balances()
    if author not in balances:
        return {"author": author, "balance": 0.0, "history": [], "notifications": []}
    rec = balances[author]
    return {
        "author": author,
        "balance": rec["balance"],
        "history": rec["history"],
        "pending_notifications": rec.get("notifications", []),
    }


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
        [{"author": a, "balance": r["balance"]} for a, r in balances.items()],
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
def forum_set_dimensions(dimensions: list[str]) -> dict:
    """Set the canonical vote dimensions for this run (call once at pipeline start).

    Each dimension name must be lowercase alphanumeric with underscores (e.g. 'correctness').
    If not called, the default set is used: correctness, faithfulness, style_alignment,
    priority, confidence, feasibility.

    This replaces any previously active dimensions.
    """
    for d in dimensions:
        if not _DIM_NAME_RE.match(d):
            raise ValueError(f"Invalid dimension name '{d}'. Use lowercase letters, digits, underscores.")
    with _config_lock():
        config = _load_config()
        config["dimensions"]["active"] = list(dimensions)
        _save_config(config)
    return {"active_dimensions": dimensions}


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

    with _config_lock():
        config = _load_config()
        if name in config["dimensions"]["active"]:
            return {"status": "already_active", "name": name}
        if name in config["dimensions"]["pending"]:
            return {"status": "already_pending", "name": name}

        # Ensure _dimensions thread exists
        if not _thread_path(DIMENSIONS_THREAD).exists():
            _save({
                "thread_id": DIMENSIONS_THREAD,
                "title": "Dimension Proposals",
                "description": "Propose and vote on new vote dimensions. A proposal auto-activates at net +3.",
                "created_at": int(time.time()),
                "posts": [],
            })

        # Post the proposal
        with _thread_lock(DIMENSIONS_THREAD):
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
    """Add tag_name to the post's tags list in its thread file."""
    for path in FORUM_DIR.glob("*.json"):
        if path.name in ("balances.json", "config.json"):
            continue
        try:
            data = json.loads(path.read_text())
            for post in data["posts"]:
                if post["post_id"] == post_id:
                    tags = post.setdefault("tags", [])
                    if tag_name not in tags:
                        tags.append(tag_name)
                        with _thread_lock(data["thread_id"]):
                            path.write_text(json.dumps(data, indent=2))
                    return
        except Exception:
            continue


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

def main() -> None:
    global FORUM_DIR
    parser = argparse.ArgumentParser(description="Unity Forum MCP Server")
    parser.add_argument("--forum-dir", default="forum", help="Directory for forum thread files")
    args = parser.parse_args()
    FORUM_DIR = Path(args.forum_dir)
    FORUM_DIR.mkdir(parents=True, exist_ok=True)
    mcp.run()


if __name__ == "__main__":
    main()
