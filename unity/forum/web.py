"""Unity Forum Web UI.

A view-only web interface for the unity forum and dependency DAG.
Reads <forum-dir>/*.json and <root-dir>/dag.json directly. Served by `unity serve`.

    python -m unity.forum.web --forum-dir ./forum --root-dir . --port 8080
or programmatically via run(forum_dir, root_dir, port).
"""

import argparse
import asyncio
import json
import subprocess
import math
import re
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

app = FastAPI(title="unity-forum")
FORUM_DIR: Path = Path("forum")
ROOT_DIR: Path = Path(".")

_GRAPH_PALETTE = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _hot(post: dict) -> float:
    score = post.get("upvotes", 0) - post.get("downvotes", 0)
    sign = 1 if score > 0 else (-1 if score < 0 else 0)
    return math.log10(max(abs(score), 1)) * sign + post["timestamp"] / 45000


def _sorted_posts(posts: list[dict], sort: str) -> list[dict]:
    if sort == "hot":
        return sorted(posts, key=_hot, reverse=True)
    if sort == "new":
        return sorted(posts, key=lambda p: p["timestamp"], reverse=True)
    if sort == "top":
        return sorted(posts, key=lambda p: p.get("upvotes", 0) - p.get("downvotes", 0), reverse=True)
    return posts


def _load_thread(thread_id: str) -> dict | None:
    path = FORUM_DIR / f"{thread_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


_DEFAULT_DIMENSIONS = ["correctness", "faithfulness"]
# Pre-Forum-2.0 default set; configs still carrying exactly these six display as the
# current two (mirrors the migration in forum/server.py).
_LEGACY_DIMENSIONS = ["correctness", "faithfulness", "style_alignment",
                      "priority", "confidence", "feasibility"]


def _load_config() -> dict:
    path = FORUM_DIR / "config.json"
    if not path.exists():
        return {"dimensions": {"active": list(_DEFAULT_DIMENSIONS), "pending": {}}, "tags": {}}
    try:
        cfg = json.loads(path.read_text())
        if not cfg.get("dimensions", {}).get("active"):
            cfg.setdefault("dimensions", {})["active"] = list(_DEFAULT_DIMENSIONS)
        if sorted(cfg["dimensions"]["active"]) == sorted(_LEGACY_DIMENSIONS):
            cfg["dimensions"]["active"] = list(_DEFAULT_DIMENSIONS)
        return cfg
    except Exception:
        return {"dimensions": {"active": list(_DEFAULT_DIMENSIONS), "pending": {}}, "tags": {}}


_SORRY_RE = re.compile(r'\bsorry\b')
_ONLY_SORRY_RE = re.compile(r':=\s*sorry\s*$|by\s*\n?\s*sorry\s*$', re.MULTILINE)


def _merged_chunk_ids() -> set:
    """Chunk ids with a 'UNITY: merge chunk <id>' commit on the project repo (cached 10s).
    ROOT_DIR is .unity, so the repo is its parent."""
    now = time.time()
    if now - _merged_cache["ts"] < 10:
        return _merged_cache["ids"]
    ids: set = set()
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT_DIR.parent), "log", "--grep=UNITY: merge chunk", "--pretty=%s"],
            capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            m = re.search(r"UNITY: merge chunk (\S+)", line)
            if m:
                ids.add(m.group(1))
    except Exception:
        pass
    _merged_cache.update(ts=now, ids=ids)
    return ids


_merged_cache: dict = {"ts": 0.0, "ids": set()}


def _chunk_status(chunk: dict, merged: set | None = None) -> str:
    """Auto-color a DAG node from every signal available, in fidelity order:
    Lean source inspection > merge commits / dag status > typed forum acts > recency."""
    cid = str(chunk.get("id", ""))
    # 1) highest fidelity: inspect the Lean declaration itself (v1-style fields, if present)
    lean_file = chunk.get("lean_file")
    lines_range = chunk.get("lean_decl_lines")
    if lean_file and lines_range:
        path = ROOT_DIR / lean_file
        if path.exists():
            try:
                lines = path.read_text().splitlines()
                start = max(0, lines_range[0] - 1)
                end = min(len(lines), lines_range[1])
                block = "\n".join(lines[start:end])
                if block.strip():
                    if not _SORRY_RE.search(block):
                        return "green"
                    if _ONLY_SORRY_RE.search(block):
                        return "red"
                    return "blue"
            except Exception:
                pass
    # 2) authoritative completion: merge commits or the chunk's own status field
    st = str(chunk.get("status") or "").lower()
    if (merged and cid in merged) or st in ("merged", "done", "complete", "completed"):
        return "green"
    if st in ("blocked", "failed"):
        return "red"
    if st in ("in_progress", "in-progress", "claimed", "active"):
        return "yellow"
    # 3) typed forum acts on the chunk's thread
    for tid in dict.fromkeys([cid, f"chunk-{cid}"]):
        thread_path = FORUM_DIR / f"{tid}.json"
        if not thread_path.exists():
            continue
        try:
            posts = json.loads(thread_path.read_text()).get("posts", [])
        except Exception:
            continue
        acts = [(p.get("act"), p.get("fields") or {}, p.get("timestamp", 0)) for p in posts]
        if any(a == "result" and f.get("build_ok") for a, f, _ in acts):
            return "green"
        if any(a == "obstacle" and f.get("status") == "open" for a, f, _ in acts):
            return "red"
        if any(a == "result" and f.get("status") == "partial" for a, f, _ in acts):
            return "blue"
        if any(a == "claim" and f.get("status") == "open" for a, f, _ in acts):
            return "yellow"
        if any(time.time() - ts < 300 for _, _, ts in acts):
            return "yellow"
    return "grey"


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/threads")
def list_threads():
    """Returns threads, active dimensions, pending proposals, tags, and leaderboard."""
    threads = []
    for path in sorted(FORUM_DIR.glob("*.json")):
        if path.name in ("balances.json", "config.json"):
            continue
        try:
            data = json.loads(path.read_text())
            last = max((p["timestamp"] for p in data["posts"]), default=data["created_at"])
            threads.append({
                "thread_id": data["thread_id"],
                "title": data["title"],
                "description": data.get("description", ""),
                "post_count": len(data["posts"]),
                "last_activity": last,
                "pinned": data["thread_id"] == "_dimensions",
            })
        except Exception:
            continue
    # Pinned first, then by last_activity
    threads.sort(key=lambda t: (not t["pinned"], -t["last_activity"]))
    config = _load_config()
    balances_path = FORUM_DIR / "balances.json"
    leaderboard = []
    if balances_path.exists():
        try:
            balances = json.loads(balances_path.read_text())
            leaderboard = sorted(
                [{"author": a, "balance": r["balance"]} for a, r in balances.items()],
                key=lambda x: x["balance"], reverse=True,
            )
        except Exception:
            pass
    return JSONResponse({
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
    })


@app.get("/api/threads/{thread_id}")
def get_thread(thread_id: str, sort: str = "hot"):
    data = _load_thread(thread_id)
    if data is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    posts = data["posts"]
    for p in posts:
        p.setdefault("upvotes", 0)
        p.setdefault("downvotes", 0)
        p.setdefault("votes_by_dimension", {})
        p.setdefault("tags", [])
        if not isinstance(p.get("reply_to"), list):
            p["reply_to"] = [p["reply_to"]] if p.get("reply_to") else []
    config = _load_config()
    return JSONResponse({
        "thread_id": data["thread_id"],
        "title": data["title"],
        "description": data.get("description", ""),
        "post_count": len(posts),
        "active_dimensions": config["dimensions"]["active"],
        "posts": _sorted_posts(posts, sort),
    })


@app.get("/api/graph")
def get_graph():
    """All posts as nodes + reply edges. Used by the graph view."""
    thread_colors: dict[str, str] = {}
    nodes = []
    edges = []
    for path in sorted(FORUM_DIR.glob("*.json")):
        if path.name in ("balances.json", "config.json"):
            continue
        try:
            data = json.loads(path.read_text())
            tid = data["thread_id"]
            if tid not in thread_colors:
                thread_colors[tid] = _GRAPH_PALETTE[len(thread_colors) % len(_GRAPH_PALETTE)]
            color = thread_colors[tid]
            for post in data["posts"]:
                reply_to = post.get("reply_to") or []
                if not isinstance(reply_to, list):
                    reply_to = [reply_to] if reply_to else []
                nodes.append({
                    "id": post["post_id"],
                    "author": post.get("author", "?"),
                    "content_preview": post.get("content", "")[:200],
                    "thread_id": tid,
                    "thread_title": data["title"],
                    "color": color,
                    "upvotes": post.get("upvotes", 0),
                    "downvotes": post.get("downvotes", 0),
                    "votes_by_dimension": post.get("votes_by_dimension", {}),
                    "tags": post.get("tags", []),
                    "timestamp": post["timestamp"],
                    "redacted": post.get("redacted", False),
                })
                for parent_id in reply_to:
                    edges.append({"id": post["post_id"] + "__" + parent_id,
                                  "source": post["post_id"], "target": parent_id})
        except Exception:
            continue
    return JSONResponse({"nodes": nodes, "edges": edges, "thread_colors": thread_colors})


@app.get("/api/tags/{name}")
def get_tag(name: str):
    config = _load_config()
    tags = config.get("tags", {})
    if name not in tags:
        return JSONResponse({"error": "tag not found"}, status_code=404)
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
    return JSONResponse({
        "tag": name,
        "description": tag["description"],
        "created_by": tag["created_by"],
        "posts": sorted(posts, key=_hot, reverse=True),
    })


@app.get("/api/dag")
def get_dag():
    dag_file = ROOT_DIR / "dag.json"
    if not dag_file.exists():
        return JSONResponse({"error": "dag.json not found"}, status_code=404)
    try:
        dag = json.loads(dag_file.read_text())
    except Exception:
        return JSONResponse({"error": "failed to parse dag.json"}, status_code=500)
    merged = _merged_chunk_ids()
    chunks = [{**c, "status": _chunk_status(c, merged)} for c in dag.get("chunks", [])]
    return JSONResponse({"chunks": chunks})


@app.get("/api/events")
async def events():
    async def generate():
        yield "data: connected\n\n"
        last_mtime = 0.0
        while True:
            await asyncio.sleep(1)
            try:
                mtime = max(
                    (p.stat().st_mtime for p in FORUM_DIR.glob("*.json")),
                    default=0.0,
                )
                dag_file = ROOT_DIR / "dag.json"
                if dag_file.exists():
                    mtime = max(mtime, dag_file.stat().st_mtime)
                if mtime > last_mtime:
                    last_mtime = mtime
                    yield "data: update\n\n"
            except Exception:
                pass
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Forum HTML ────────────────────────────────────────────────────────────────


@app.get("/api/workspace")
def get_workspace():
    """Typed-workspace state: decisions, handoffs, per-chunk consensus, obstacles,
    questions, ledger, and act telemetry (Forum 2.0 view)."""
    decisions: dict = {}
    handoffs: list = []
    chunks: dict = {}
    questions: list = []
    ledger: list = []
    by_act: dict = {}
    for tp in sorted(FORUM_DIR.glob("*.json")):
        if tp.name.startswith("_") or tp.name in ("config.json", "balances.json"):
            continue
        try:
            data = json.loads(tp.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        tid = data.get("thread_id", tp.stem)
        for post in data.get("posts", []):
            act = post.get("act")
            by_act[act or "note"] = by_act.get(act or "note", 0) + 1
            f = post.get("fields") or {}
            if act == "decision":
                decisions[f.get("topic", "?")] = {"topic": f.get("topic"), "choice": f.get("choice"),
                                                  "rationale": f.get("rationale", ""), "author": post["author"],
                                                  "ts": post["timestamp"]}
            elif act == "handoff":
                handoffs.append({"phase": f.get("phase"), "content": post["content"],
                                 "author": post["author"], "ts": post["timestamp"]})
            elif act in ("claim", "result", "obstacle") and tid.startswith("chunk-"):
                c = chunks.setdefault(tid, {"chunk": tid, "claims": [], "results": [], "obstacles": []})
                if act == "claim" and f.get("status") == "open":
                    c["claims"].append({"author": post["author"], "strategy": f.get("strategy", "")})
                elif act == "result":
                    open_obj = [o for o in f.get("objections", []) if o.get("status") == "open"]
                    c["results"].append({"id": post["post_id"], "author": post["author"],
                                         "status": f.get("status"), "build_ok": f.get("build_ok"),
                                         "endorsements": f.get("endorsements", []),
                                         "open_objections": open_obj,
                                         "mergeable": bool(f.get("endorsements")) and not open_obj})
                elif act == "obstacle" and f.get("status") == "open":
                    c["obstacles"].append({"author": post["author"], "content": post["content"]})
            elif act == "question" and f.get("status") == "open":
                questions.append({"id": post["post_id"], "author": post["author"], "to": f.get("to", ""),
                                  "chunk": f.get("chunk", ""), "content": post["content"]})
            elif act == "ledger":
                ledger.append({"author": post["author"], "kind": f.get("kind"), "title": f.get("title"),
                               "goal_shape": f.get("goal_shape", ""), "content": post["content"],
                               "ts": post["timestamp"]})
    return {"decisions": sorted(decisions.values(), key=lambda d: -d["ts"]),
            "handoffs": handoffs[-3:][::-1],
            "chunks": sorted(chunks.values(), key=lambda c: c["chunk"]),
            "questions": questions, "ledger": ledger[::-1][:20], "by_act": by_act,
            "agents": _agent_statuses(chunks)}


def _agent_statuses(chunks: dict) -> list:
    """Roster + live status for the overview: working (recent tool call), reviewing
    (primary during critic), else idle. Activity from the agent's open claim."""
    import yaml as _yaml
    try:
        doc = _yaml.safe_load((ROOT_DIR / "agents.yaml").read_text()) or {}
        groups = doc.get("agents") or []
    except Exception:
        return []
    running = _current_run()["running"]
    phase = None
    try:
        phase = json.loads((ROOT_DIR / "state.json").read_text()).get("phase")
    except Exception:
        pass
    last_tool: dict = {}
    tf = ROOT_DIR / "logs" / "tools.jsonl"
    if tf.exists():
        for line in tf.read_text(errors="replace").splitlines()[-400:]:
            try:
                e = json.loads(line)
                last_tool[e.get("agent", "")] = e.get("ts", "")
            except json.JSONDecodeError:
                continue
    claims = {}
    for c in chunks.values():
        for cl in c.get("claims", []):
            claims[cl["author"]] = {"chunk": c["chunk"], "strategy": cl.get("strategy", "")}
    out = []
    primary_seen = any(g.get("primary") for g in groups)
    for gi, g in enumerate(groups):
        names = g.get("names") or ([g.get("name")] if g.get("name") else [])
        for ni, nm in enumerate(names):
            recent = False
            ts = last_tool.get(nm)
            if ts and running:
                try:
                    recent = time.time() - time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S")) < 240
                except ValueError:
                    pass
            is_primary = (g.get("primary") and ni == 0) or (not primary_seen and gi == 0 and ni == 0)
            status = ("reviewing" if recent and is_primary and phase == "critic"
                      else "working" if recent else "idle")
            cl = claims.get(nm)
            out.append({"name": nm, "model": g.get("model", ""), "primary": bool(is_primary),
                        "status": status,
                        "activity": (cl or {}).get("strategy", ""), "chunk": (cl or {}).get("chunk", "")})
    return out


FORUM_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<title>unity — forum</title>
<meta charset="utf-8">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
:root { --bg:#f7f7f8; --card:#ffffff; --ink:#26242b; --mut:#8e8c94; --line:#e8e7ea; --acc:#7c5cbf; --acc-soft:#f1ebfa; --acc-ink:#6b46a8; --sans:-apple-system,'Inter','Segoe UI',Roboto,sans-serif; --mono:ui-monospace,'Cascadia Code','Fira Code','Menlo',monospace; }
body { font-family: var(--sans); font-size: 13.5px; background: var(--bg); color: var(--ink); }
.mono { font-family: var(--mono); }
header { display: flex; align-items: center; justify-content: space-between; padding: 10px 22px; border-bottom: 1px solid var(--line); background: var(--bg); }
header h1 { font-family: var(--mono); font-size: 15px; font-weight: 700; }
header nav a { font-size: 12px; color: var(--mut); text-decoration: none; margin-left: 14px; }
header nav a:hover { color: var(--ink); }
.pane { padding: 24px 28px 40px; max-width: 1380px; margin: 0 auto; }
.pagehead { display: flex; align-items: baseline; gap: 14px; margin-bottom: 18px; }
.pagehead h1 { font-size: 24px; font-weight: 700; }
.pagehead .ctx { margin-left: auto; font-family: var(--mono); font-size: 12.5px; color: var(--mut); }
.layout { display: grid; grid-template-columns: 300px 1fr; gap: 18px; align-items: start; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 16px 14px; box-shadow: 0 1px 2px rgba(20,18,26,0.04); }
.side h2 { font-size: 11px; letter-spacing: 0.13em; text-transform: uppercase; color: #a3a1a9; margin: 2px 8px 10px; font-weight: 700; }
.th { display: flex; align-items: center; gap: 8px; padding: 7px 10px; border-radius: 9px; cursor: pointer; font-family: var(--mono); font-size: 12.5px; color: #55535b; }
.th:hover { background: #f4f3f6; }
.th.on { background: var(--acc-soft); color: var(--acc-ink); font-weight: 600; }
.th .nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.th .n { margin-left: auto; color: #a3a1a9; font-size: 12px; }
.main .title { font-family: var(--mono); font-size: 17px; font-weight: 700; margin: 4px 4px 16px; }
.post { border: 1px solid var(--line); border-radius: 12px; padding: 13px 16px; margin-bottom: 12px; background: var(--card); }
.post.reply { margin-left: 34px; border-left: 3px solid var(--acc); }
.post .meta { display: flex; align-items: baseline; gap: 9px; flex-wrap: wrap; margin-bottom: 7px; }
.post .who { font-family: var(--mono); font-size: 12px; color: #a3a1a9; }
.post .author { font-weight: 700; font-size: 13.5px; }
.post .body { line-height: 1.55; overflow-wrap: anywhere; font-size: 13.5px; }
.act-badge { font-family: var(--mono); font-size: 10px; font-weight: 700; letter-spacing: 0.08em; border-radius: 6px; padding: 2px 8px; }
.a-claim { background: var(--acc-soft); color: var(--acc-ink); }
.a-result { background: #e6f4ea; color: #2e7d32; }
.a-obstacle { background: #fdeaea; color: #b91c1c; }
.a-question { background: #fdf3dd; color: #b45309; }
.a-answer { background: #e3f0fb; color: #1a5fa0; }
.a-decision { background: #ede6f9; color: #5b3a9e; }
.a-handoff { background: #eceff1; color: #546e7a; }
.a-ledger { background: #e0f2f1; color: #00695c; }
.a-note { background: #f0eff2; color: #7c7a83; }
.tags { margin-top: 7px; }
.tag { font-family: var(--mono); font-size: 10.5px; background: #f4f3f6; color: #7c7a83; border-radius: 6px; padding: 2px 7px; margin-right: 5px; }
.empty { color: #c4c2ca; padding: 14px; }
@media (max-width: 900px) { .layout { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>unity</h1>
  <nav><a href="/">← app</a></nav>
</header>
<div class="pane">
  <div class="pagehead"><h1>Forum</h1><span class="ctx">agent consensus threads · <span id="live">connecting…</span></span></div>
  <div class="layout">
    <div class="card side"><h2>threads</h2><div id="threads"></div></div>
    <div class="card main"><div class="title" id="tt"></div><div id="posts"><div class="empty">select a thread</div></div></div>
  </div>
</div>
<script>
const esc = t => (t || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const $ = id => document.getElementById(id);
let CUR = null;
function reltime(ts) {
  if (!ts) return '';
  const s = Date.now() / 1000 - ts;
  if (s < 90) return 'just now';
  if (s < 5400) return Math.round(s / 60) + 'm ago';
  if (s < 129600) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}
function net(vbd, dim) { const c = (vbd || {})[dim] || {}; return (c.up || 0) - (c.down || 0); }
async function loadThreads() {
  const d = await (await fetch('/api/threads')).json();
  const ths = d.threads.filter(t => t.post_count || !t.thread_id.startsWith('_'));
  $('threads').innerHTML = ths.map(t =>
    '<div class="th' + (t.thread_id === CUR ? ' on' : '') + '" data-id="' + esc(t.thread_id) + '">' +
    '<span class="nm">' + esc(t.thread_id) + '</span><span class="n">' + t.post_count + '</span></div>').join('') ||
    '<div class="empty">no threads yet</div>';
  document.querySelectorAll('.th').forEach(el => el.onclick = () => { CUR = el.dataset.id; loadThreads(); loadPosts(); });
  if (!CUR && ths.length) { CUR = ths[0].thread_id; loadThreads(); loadPosts(); }
}
async function loadPosts() {
  if (!CUR) return;
  const d = await (await fetch('/api/threads/' + encodeURIComponent(CUR) + '?sort=none')).json();
  (d.posts || []).sort((a, b) => a.timestamp - b.timestamp);
  $('tt').textContent = d.thread_id;
  const ids = new Set((d.posts || []).map(p => p.post_id));
  $('posts').innerHTML = (d.posts || []).map(p => {
    const act = (p.act || (p.tags || []).includes('decision') && 'decision' || 'note').toLowerCase();
    const isReply = (p.reply_to || []).some(r => ids.has(r));
    const dims = (d.active_dimensions || []).map(dim => esc(dim) + ' ' + (net(p.votes_by_dimension, dim) >= 0 ? '+' : '') + net(p.votes_by_dimension, dim)).join(' · ');
    return '<div class="post' + (isReply ? ' reply' : '') + '">' +
      '<div class="meta"><span class="act-badge a-' + esc(act) + '">' + esc(act.toUpperCase()) + '</span>' +
      '<span class="author">' + esc(p.author) + '</span>' +
      '<span class="who">' + dims + (dims ? ' · ' : '') + reltime(p.timestamp) + ' · #' + esc(p.post_id) + '</span></div>' +
      '<div class="body">' + esc(p.content) + '</div>' +
      ((p.tags || []).length ? '<div class="tags">' + p.tags.map(t => '<span class="tag">' + esc(t) + '</span>').join('') + '</div>' : '') +
      '</div>';
  }).join('') || '<div class="empty">no posts in this thread</div>';
}
function connect() {
  const es = new EventSource('/api/events');
  es.onopen = () => { $('live').textContent = 'live'; };
  es.onmessage = () => { loadThreads(); loadPosts(); };
  es.onerror = () => { $('live').textContent = 'reconnecting…'; es.close(); setTimeout(connect, 3000); };
}
loadThreads(); connect();
</script>
</body>
</html>
"""


# ── Graph HTML ────────────────────────────────────────────────────────────────

GRAPH_HTML = """\
<!DOCTYPE html>
<html>
<head>
<title>unity — graph</title>
<meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: ui-monospace, 'Cascadia Code', 'Fira Code', 'Menlo', monospace; font-size: 13px; background: #fafafa; color: #111; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
header { display: flex; align-items: center; justify-content: space-between; padding: 10px 20px; border-bottom: 1px solid #e4e4e4; flex-shrink: 0; background: #fafafa; }
header h1 { font-size: 14px; font-weight: 600; letter-spacing: 0.14em; }
nav { display: flex; gap: 16px; }
nav a { font-size: 12px; color: #888; text-decoration: none; transition: color 0.12s; }
nav a:hover { color: #111; }
.controls { display: flex; align-items: center; gap: 14px; }
#status { font-size: 11px; color: #bbb; }
.color-tabs { display: flex; border: 1px solid #e0e0e0; border-radius: 5px; overflow: hidden; }
.color-tabs button { background: none; border: none; border-left: 1px solid #e0e0e0; cursor: pointer; font: inherit; font-size: 12px; padding: 3px 12px; color: #666; transition: all 0.12s; }
.color-tabs button:first-child { border-left: none; }
.color-tabs button.active { background: #111; color: #fff; }
main { display: flex; flex: 1; overflow: hidden; position: relative; }
#cy { flex: 1; }
#info-panel { position: absolute; right: 0; top: 0; bottom: 0; width: 300px; border-left: 1px solid #e4e4e4; overflow-y: auto; padding: 18px; background: #fff; display: none; z-index: 10; }
#info-panel.visible { display: block; }
#info-close { float: right; cursor: pointer; font-size: 15px; line-height: 1; color: #bbb; transition: color 0.1s; }
#info-close:hover { color: #111; }
.info-field { margin-bottom: 12px; }
.info-label { font-size: 10px; color: #bbb; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 3px; }
.info-value { line-height: 1.5; color: #111; }
.info-value.md { font-size: 12px; }
.info-value.md p { margin-bottom: 0.4em; }
.info-value.md code { background: #f0f0f2; padding: 1px 4px; font-size: 11px; border-radius: 3px; }
.info-value.md pre { background: #f4f4f6; padding: 7px 10px; overflow-x: auto; font-size: 11px; margin: 0.3em 0; border-radius: 4px; }
.info-value.md pre code { background: none; padding: 0; }
.info-link { color: #4070e8; text-decoration: none; }
.info-link:hover { text-decoration: underline; }
.tag-chips { display: flex; flex-wrap: wrap; gap: 4px; }
.tag-chip { font-size: 10px; background: #fff8e8; color: #a06000; border: 1px solid #f0dfa0; padding: 2px 7px; border-radius: 10px; }
.dim-row { display: flex; gap: 8px; font-size: 11px; margin-bottom: 2px; }
.dim-name { color: #888; min-width: 110px; }
.dim-up { color: #16a34a; }
.dim-down { color: #dc2626; }
#legend { position: absolute; bottom: 14px; left: 14px; background: rgba(255,255,255,0.97); border: 1px solid #e4e4e4; padding: 9px 14px; font-size: 11px; max-width: 190px; pointer-events: none; border-radius: 6px; }
#legend-title { font-weight: 600; margin-bottom: 5px; color: #999; font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; }
.legend-row { display: flex; align-items: center; gap: 6px; margin-bottom: 3px; overflow: hidden; }
.legend-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.legend-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #555; }
</style>
</head>
<body>
<header>
  <h1>unity</h1>
  <div class="controls">
    <nav>
      <a href="/">← app</a>
      <a href="/forum">forum →</a>
      <a href="/workspace">workspace →</a>
      <a href="/dag">dag →</a>
    </nav>
    <span id="status">loading...</span>
    <div class="color-tabs">
      <button class="active" data-mode="thread">by thread</button>
      <button data-mode="tag">by tag</button>
    </div>
  </div>
</header>
<main>
  <div id="cy"></div>
  <div id="info-panel">
    <span id="info-close" onclick="closePanel()">&#x2715;</span>
    <div id="info-content"></div>
  </div>
  <div id="legend"><div id="legend-title">threads</div><div id="legend-rows"></div></div>
</main>
<script>
marked.use({ breaks: true, gfm: true });
function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function reltime(ts) {
  const d = Math.floor(Date.now()/1000 - ts);
  if (d < 60) return d+'s'; if (d < 3600) return Math.floor(d/60)+'m';
  if (d < 86400) return Math.floor(d/3600)+'h'; return Math.floor(d/86400)+'d';
}

const TAG_PALETTE = ['#e07b39','#7b61c4','#2d9e6b','#c4416a','#3d7fc4','#9e882d','#4ab0b0','#c46e2d'];
let cy = null, colorMode = 'thread', graphData = null, tagColors = {};
let nodePositions = {};  // id -> {x,y}, persisted across reloads
let lastDataSig = '';    // "nodeCount,edgeCount" — skip rebuild when unchanged

document.querySelectorAll('.color-tabs button').forEach(btn => {
  btn.addEventListener('click', () => {
    colorMode = btn.dataset.mode;
    document.querySelectorAll('.color-tabs button').forEach(b => b.classList.toggle('active', b === btn));
    if (graphData) recolor();
  });
});

function buildTagColors(nodes) {
  tagColors = {};
  nodes.forEach(n => (n.tags||[]).forEach(t => {
    if (!(t in tagColors)) tagColors[t] = TAG_PALETTE[Object.keys(tagColors).length % TAG_PALETTE.length];
  }));
}

function nodeColor(n) {
  if (colorMode === 'tag') {
    const firstTag = (n.tags||[])[0];
    return firstTag ? (tagColors[firstTag]||'#cccccc') : '#dddddd';
  }
  return n.color || '#cccccc';
}

function buildLegend() {
  const title = document.getElementById('legend-title');
  const rows = document.getElementById('legend-rows');
  rows.innerHTML = '';
  if (colorMode === 'thread') {
    title.textContent = 'threads';
    Object.entries(graphData.thread_colors).forEach(([tid, col]) => {
      rows.innerHTML += '<div class="legend-row"><span class="legend-dot" style="background:'+col+'"></span><span class="legend-label">'+esc(tid)+'</span></div>';
    });
  } else {
    title.textContent = 'tags';
    if (!Object.keys(tagColors).length) {
      rows.innerHTML = '<div style="color:#aaa">no tags yet</div>';
    } else {
      Object.entries(tagColors).forEach(([tag, col]) => {
        rows.innerHTML += '<div class="legend-row"><span class="legend-dot" style="background:'+col+'"></span><span class="legend-label">#'+esc(tag)+'</span></div>';
      });
      rows.innerHTML += '<div class="legend-row"><span class="legend-dot" style="background:#dddddd"></span><span class="legend-label">untagged</span></div>';
    }
  }
}

function recolor() {
  if (!cy) return;
  const nodeMap = {};
  graphData.nodes.forEach(n => { nodeMap[n.id] = n; });
  cy.nodes().forEach(node => {
    const n = nodeMap[node.id()];
    if (!n) return;
    const col = nodeColor(n);
    node.style('background-color', col);
  });
  buildLegend();
}

// Spring simulation on the thread-level graph.
// Inter-thread edge count -> attraction; ideal distance ∝ 1/√count so heavily
// connected threads end up closer. Unconnected threads only repel each other.
// Returns { threadId: {x, y} } for every thread present in data.
function computeThreadLayout(data) {
  const allThreadIds = [...new Set(data.nodes.map(n => n.thread_id))];
  if (allThreadIds.length === 0) return {};

  // Count cross-thread edges
  const nodeThread = {};
  data.nodes.forEach(n => { nodeThread[n.id] = n.thread_id; });
  const interCount = {};
  data.edges.forEach(e => {
    const t1 = nodeThread[e.source], t2 = nodeThread[e.target];
    if (t1 && t2 && t1 !== t2) {
      const key = [t1, t2].sort().join('\\x00');
      interCount[key] = (interCount[key] || 0) + 1;
    }
  });

  // Initialise on a circle
  const pos = {};
  allThreadIds.forEach((tid, i) => {
    const a = (2 * Math.PI * i) / allThreadIds.length;
    pos[tid] = { x: Math.cos(a) * 400 + 400, y: Math.sin(a) * 400 + 300 };
  });

  // 300 relaxation steps
  const BASE_DIST = 450;
  for (let iter = 0; iter < 300; iter++) {
    const f = {};
    allThreadIds.forEach(t => { f[t] = { x: 0, y: 0 }; });

    // Attraction: inter-thread edges pull threads together
    Object.entries(interCount).forEach(([key, cnt]) => {
      const sep = key.split('\\x00');
      const t1 = sep[0], t2 = sep[1];
      const dx = pos[t2].x - pos[t1].x, dy = pos[t2].y - pos[t1].y;
      const dist = Math.sqrt(dx*dx + dy*dy) || 1;
      const ideal = BASE_DIST / Math.sqrt(cnt);   // more edges → shorter ideal
      const k = 0.02 * (dist - ideal) / dist;
      f[t1].x += k*dx; f[t1].y += k*dy;
      f[t2].x -= k*dx; f[t2].y -= k*dy;
    });

    // Repulsion: all thread pairs push apart
    for (let i = 0; i < allThreadIds.length; i++) {
      for (let j = i + 1; j < allThreadIds.length; j++) {
        const t1 = allThreadIds[i], t2 = allThreadIds[j];
        const dx = pos[t2].x - pos[t1].x, dy = pos[t2].y - pos[t1].y;
        const d2 = dx*dx + dy*dy || 1, d = Math.sqrt(d2);
        const rep = 25000 / (d2 * d);
        f[t1].x -= rep*dx; f[t1].y -= rep*dy;
        f[t2].x += rep*dx; f[t2].y += rep*dy;
      }
    }

    allThreadIds.forEach(t => { pos[t].x += f[t].x; pos[t].y += f[t].y; });
  }

  return pos;
}

function buildGraph(data) {
  graphData = data;
  buildTagColors(data.nodes);
  const nodeIds = new Set(data.nodes.map(n => n.id));
  const elements = [];

  // Centroid of already-placed nodes per thread (for existing threads)
  const threadCentroids = {};
  data.nodes.forEach(n => {
    if (nodePositions[n.id]) {
      const c = threadCentroids[n.thread_id] || (threadCentroids[n.thread_id] = { x: 0, y: 0, count: 0 });
      c.x += nodePositions[n.id].x; c.y += nodePositions[n.id].y; c.count++;
    }
  });
  Object.values(threadCentroids).forEach(c => { c.x /= c.count; c.y /= c.count; });

  // For brand-new threads (no saved nodes), use the inter-thread spring layout
  // so heavily cross-linked threads start near each other
  const allThreadIds = [...new Set(data.nodes.map(n => n.thread_id))];
  const hasNewThreads = allThreadIds.some(t => !threadCentroids[t]);
  if (hasNewThreads) {
    const springPos = computeThreadLayout(data);
    allThreadIds.forEach(tid => {
      if (!threadCentroids[tid] && springPos[tid]) threadCentroids[tid] = springPos[tid];
    });
  }

  data.nodes.forEach(n => {
    const elem = { data: {
      id: n.id,
      label: (n.redacted ? '[REDACTED]' : n.content_preview.substring(0,20)).replace(/\\n/g,' '),
      author: n.author,
      thread_id: n.thread_id,
      bgColor: nodeColor(n),
    }};
    if (nodePositions[n.id]) {
      // Existing node: restore exact position so it doesn't move
      elem.position = nodePositions[n.id];
    } else {
      // New node: start near its thread centroid so cose keeps it in the cluster
      const c = threadCentroids[n.thread_id];
      elem.position = { x: c.x + (Math.random() - 0.5) * 80, y: c.y + (Math.random() - 0.5) * 80 };
    }
    elements.push(elem);
  });

  data.edges.forEach(e => {
    if (nodeIds.has(e.source) && nodeIds.has(e.target)) {
      elements.push({ data: { id: e.id, source: e.source, target: e.target }});
    }
  });

  if (cy) cy.destroy();
  cy = cytoscape({
    container: document.getElementById('cy'),
    elements,
    style: [
      { selector: 'node', style: {
        'background-color': 'data(bgColor)',
        'border-color': 'rgba(0,0,0,0.13)',
        'border-width': 1,
        'label': 'data(label)',
        'font-family': 'ui-monospace, "Cascadia Code", "Fira Code", "Menlo", monospace',
        'font-size': 10,
        'text-valign': 'center',
        'text-halign': 'center',
        'text-wrap': 'ellipsis',
        'text-max-width': 74,
        'width': 88,
        'height': 30,
        'shape': 'roundrectangle',
        'color': '#111',
        'min-zoomed-font-size': 7,
      }},
      { selector: 'node:selected', style: { 'border-width': 2, 'border-color': 'rgba(51,51,51,0.5)' }},
      { selector: 'node:active', style: { 'overlay-opacity': 0.07 }},
      { selector: 'node[?redacted]', style: { 'opacity': 0.35 }},
      { selector: 'edge', style: {
        'curve-style': 'bezier',
        'target-arrow-shape': 'triangle',
        'target-arrow-color': '#ccc',
        'line-color': '#ccc',
        'arrow-scale': 0.65,
        'width': 1,
      }},
    ],
    // randomize: false + pre-set positions = existing nodes barely move, new ones land near their thread
    layout: { name: 'cose', animate: false, nodeRepulsion: 4096, idealEdgeLength: 60, gravity: 1, padding: 40, randomize: false },
  });

  // Persist final positions so next rebuild preserves them
  cy.nodes().forEach(node => {
    nodePositions[node.id()] = { x: node.position('x'), y: node.position('y') };
  });

  cy.on('tap', 'node', e => { e.stopPropagation(); showPanel(e.target.id()); });
  cy.on('tap', e => { if (e.target === cy) closePanel(); });
  cy.on('mouseover', 'node', e => { e.target.style('border-width', 2); });
  cy.on('mouseout', 'node', e => { e.target.style('border-width', e.target.selected() ? 2.5 : 1); });
  buildLegend();
  document.getElementById('status').textContent = data.nodes.length+' posts';
}

const nodeDataMap = {};
function showPanel(id) {
  if (!graphData) return;
  const n = graphData.nodes.find(x => x.id === id);
  if (!n) return;
  nodeDataMap[id] = n;
  const score = n.upvotes - n.downvotes;
  const dimRows = Object.entries(n.votes_by_dimension||{}).map(([dim, c]) => {
    const net = (c.up||0)-(c.down||0);
    return '<div class="dim-row"><span class="dim-name">'+esc(dim)+'</span>'
      +'<span class="dim-up">&#8593;'+(c.up||0)+'</span> '
      +'<span class="dim-down">&#8595;'+(c.down||0)+'</span> '
      +'('+(net>=0?'+':'')+net+')</div>';
  }).join('');
  const tags = (n.tags||[]).map(t=>'<span class="tag-chip">#'+esc(t)+'</span>').join('');
  const contentHtml = n.redacted ? '<em style="color:#bbb">[redacted]</em>' : marked.parse(n.content_preview+(n.content_preview.length>=200?'…':''));
  document.getElementById('info-content').innerHTML =
    '<div class="info-field"><div class="info-label">author</div><div class="info-value">'+esc(n.author)+'</div></div>'
    +'<div class="info-field"><div class="info-label">thread</div><div class="info-value">'+esc(n.thread_title)+' <span style="color:#aaa">('+esc(n.thread_id)+')</span></div></div>'
    +'<div class="info-field"><div class="info-label">'+reltime(n.timestamp)+' ago &middot; &#8593;'+n.upvotes+' &#8595;'+n.downvotes+' ('+(score>=0?'+':'')+score+')</div></div>'
    +(dimRows?'<div class="info-field"><div class="info-label">by dimension</div>'+dimRows+'</div>':'')
    +(tags?'<div class="info-field"><div class="info-label">tags</div><div class="tag-chips">'+tags+'</div></div>':'')
    +'<div class="info-field"><div class="info-label">content</div><div class="info-value md">'+contentHtml+'</div></div>'
    +'<div class="info-field"><a class="info-link" href="/#post-'+esc(n.id)+'" target="_blank">view in forum &rarr;</a></div>';
  document.getElementById('info-panel').classList.add('visible');
}

function closePanel() {
  document.getElementById('info-panel').classList.remove('visible');
  if (cy) cy.$(':selected').unselect();
}

async function load() {
  const res = await fetch('/api/graph');
  if (!res.ok) { document.getElementById('status').textContent = 'error'; return; }
  const data = await res.json();
  const sig = data.nodes.length + ',' + data.edges.length;
  if (sig === lastDataSig) return;  // nothing changed, don't rebuild
  lastDataSig = sig;
  buildGraph(data);
}

load();
setInterval(load, 2000);
</script>
</body>
</html>
"""


# ── DAG HTML ──────────────────────────────────────────────────────────────────

DAG_HTML = """\
<!DOCTYPE html>
<html>
<head>
<title>unity — chunks</title>
<meta charset="utf-8">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system,'Inter','Segoe UI',Roboto,sans-serif; font-size: 13px; background: #f7f7f8; color: #26242b; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
header { display: flex; align-items: center; justify-content: space-between; padding: 10px 22px; border-bottom: 1px solid #e8e7ea; flex-shrink: 0; background: #f7f7f8; }
header h1 { font-size: 14px; font-weight: 600; letter-spacing: 0.14em; }
nav { display: flex; gap: 16px; }
nav a { font-size: 12px; color: #888; text-decoration: none; transition: color 0.12s; }
nav a:hover { color: #111; }
.controls { display: flex; align-items: center; gap: 16px; }
#status { font-size: 11px; color: #bbb; }
main { display: flex; flex: 1; overflow: hidden; position: relative; }
#cy { flex: 1; }
#info-panel { position: absolute; right: 0; top: 0; bottom: 0; width: 280px; border-left: 1px solid #e4e4e4; overflow-y: auto; padding: 18px; background: #fff; display: none; z-index: 10; }
#info-panel.visible { display: block; }
#info-close { float: right; cursor: pointer; font-size: 15px; line-height: 1; color: #bbb; transition: color 0.1s; }
#info-close:hover { color: #111; }
.info-field { margin-bottom: 12px; }
.info-label { font-size: 10px; color: #bbb; text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 3px; }
.info-value { white-space: pre-wrap; line-height: 1.5; color: #111; }
.info-link { color: #4070e8; text-decoration: none; }
.info-link:hover { text-decoration: underline; }
.status-dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
#legend { position: absolute; bottom: 14px; left: 14px; background: rgba(255,255,255,0.97); border: 1px solid #e4e4e4; padding: 9px 14px; font-size: 11px; line-height: 2; pointer-events: none; border-radius: 6px; }
.legend-row { display: flex; align-items: center; gap: 6px; color: #555; }
#waiting { position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%); color: #bbb; font-size: 13px; }
</style>
</head>
<body>
<header>
  <h1 style="font-family:ui-monospace,Menlo,monospace">unity</h1>
  <div class="controls">
    <nav>
      <a href="/">← app</a>
      <a href="/forum">forum →</a>
      <a href="/workspace">workspace →</a>
      <a href="/graph">graph →</a>
    </nav>
    <span id="status">connecting...</span>
  </div>
</header>
<div style="display:flex;align-items:baseline;gap:14px;padding:18px 26px 4px">
  <span style="font-size:24px;font-weight:700">Chunks</span>
  <span id="hlegend" style="display:flex;gap:16px;font-size:12.5px;color:#6e6c75;align-items:center"></span>
  <span style="margin-left:auto;font-family:ui-monospace,Menlo,monospace;font-size:12.5px;color:#8e8c94">proof DAG · edges show dependencies</span>
</div>
<main style="padding:0 26px 20px">
  <div id="cy" style="background:#fff;border:1px solid #e8e7ea;border-radius:14px"></div>
  <div id="info-panel">
    <span id="info-close" onclick="closePanel()">&#x2715;</span>
    <div id="info-content"></div>
  </div>
  <div id="legend" style="display:none">
    <div class="legend-row"><span class="status-dot" style="background:#e8e8e8;border:1px solid #999"></span>pending</div>
    <div class="legend-row"><span class="status-dot" style="background:#fff3a0;border:1px solid #c8a000"></span>claimed / active</div>
    <div class="legend-row"><span class="status-dot" style="background:#c8f0c8;border:1px solid #2d7a2d"></span>merged / builds sorry-free</div>
    <div class="legend-row"><span class="status-dot" style="background:#c8e0f8;border:1px solid #1a5fa0"></span>partial result</div>
    <div class="legend-row"><span class="status-dot" style="background:#f8c8c8;border:1px solid #c02020"></span>blocked / open obstacle</div>
  </div>
  <div id="waiting" style="display:none">waiting for dag.json...</div>
</main>
<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dagre/0.8.5/dagre.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/cytoscape-dagre@2.5.0/cytoscape-dagre.js"></script>
<script>
cytoscape.use(cytoscapeDagre);

const STATUS_COLOR = {
  grey:   { bg: '#ffffff', border: '#d97706' },   // pending
  yellow: { bg: '#ffffff', border: '#7c5cbf' },   // active / claimed
  green:  { bg: '#ffffff', border: '#2e7d32' },   // merged
  blue:   { bg: '#ffffff', border: '#7c5cbf' },   // partial -> active
  red:    { bg: '#ffffff', border: '#c62828' },   // blocked
};
const LEGEND = [['green','Merged','#2e7d32'], ['yellow','Active','#7c5cbf'], ['grey','Pending','#d97706'], ['red','Blocked','#c62828']];
function updateHeaderLegend(data) {
  const counts = {green:0, yellow:0, grey:0, red:0};
  (data.chunks||[]).forEach(c => { const k = c.status === 'blue' ? 'yellow' : c.status; if (k in counts) counts[k]++; });
  const el = document.getElementById('hlegend');
  if (el) el.innerHTML = LEGEND.map(([k, label, col]) =>
    '<span><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:'+col+';margin-right:6px"></span><b>'+label+'</b> '+counts[k]+'</span>').join('');
}

let cy = null, chunks = {};
let nodePositions = {};  // id -> {x,y}, persisted across rebuilds
let lastSig = '';        // sorted chunk-ids, skip rebuild when unchanged

function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function buildGraph(data) {
  if (cy) {
    cy.nodes().forEach(n => { nodePositions[n.id()] = { x: n.position('x'), y: n.position('y') }; });
    cy.destroy();
  }
  chunks = {};
  data.chunks.forEach(c => { chunks[c.id] = c; });
  const elements = [];
  const nodeIds = new Set(data.chunks.map(c => c.id));
  data.chunks.forEach(c => {
    const col = STATUS_COLOR[c.status] || STATUS_COLOR.grey;
    const elem = { data: {
      id: c.id,
      label: c.id + (c.title && c.title !== c.id ? '\\n' + c.title.substring(0,24) : ''),
      status: c.status,
      bgColor: col.bg,
      borderColor: col.border,
    }};
    if (nodePositions[c.id]) elem.position = nodePositions[c.id];
    elements.push(elem);
    (Array.isArray(c.dependencies) ? c.dependencies : (c.dependencies?.local || [])).forEach(dep => {
      if (nodeIds.has(dep)) {
        elements.push({ data: { id: dep+'->'+c.id, source: dep, target: c.id }});
      }
    });
  });
  const allPositionsKnown = data.chunks.length > 0 && data.chunks.every(c => nodePositions[c.id]);
  cy = cytoscape({
    container: document.getElementById('cy'),
    elements,
    style: [
      { selector: 'node', style: {
        'background-color': 'data(bgColor)', 'border-color': 'data(borderColor)', 'border-width': 2,
        'label': 'data(label)', 'font-family': 'ui-monospace, "Cascadia Code", "Fira Code", "Menlo", monospace', 'font-size': '11px',
        'text-valign': 'center', 'text-halign': 'center', 'text-wrap': 'wrap',
        'shape': 'roundrectangle', 'color': '#111', 'padding': '10px 14px',
        'min-zoomed-font-size': 7,
      }},
      { selector: 'node:selected', style: { 'border-width': 2.5, 'border-color': 'rgba(51,51,51,0.6)' }},
      { selector: 'edge', style: {
        'curve-style': 'bezier', 'target-arrow-shape': 'triangle',
        'target-arrow-color': '#ccc', 'line-color': '#ccc', 'arrow-scale': 0.75, 'width': 1,
      }},
    ],
    layout: allPositionsKnown
      ? { name: 'preset' }
      : { name: 'dagre', rankDir: 'TB', nodeSep: 40, rankSep: 70, padding: 30 },
  });
  cy.nodes().forEach(n => { nodePositions[n.id()] = { x: n.position('x'), y: n.position('y') }; });
  cy.on('tap', 'node', e => { e.stopPropagation(); showPanel(e.target.id()); });
  cy.on('tap', e => { if (e.target === cy) closePanel(); });
  cy.on('mouseover', 'node', e => { e.target.style('border-width', 2.5); });
  cy.on('mouseout', 'node', e => { e.target.style('border-width', e.target.selected() ? 2.5 : 1.5); });
}

function updateColors(data) {
  if (!cy) return;
  data.chunks.forEach(c => {
    const node = cy.getElementById(c.id); if (!node.length) return;
    const col = STATUS_COLOR[c.status] || STATUS_COLOR.grey;
    node.style({ 'background-color': col.bg, 'border-color': col.border });
    node.data({ status: c.status, bgColor: col.bg, borderColor: col.border });
    chunks[c.id] = c;
  });
  if (openId) showPanel(openId);
}

let openId = null;
function showPanel(id) {
  openId = id;
  const c = chunks[id]; if (!c) return;
  const col = STATUS_COLOR[c.status] || STATUS_COLOR.grey;
  const statusLabel = { grey:'not started', yellow:'in progress', green:'fully formalized', blue:'partially formalized', red:'by sorry' }[c.status] || c.status;
  document.getElementById('info-content').innerHTML =
    '<div class="info-field"><div class="info-label">chunk</div><div class="info-value">'+esc(c.id)+'</div></div>'
    +'<div class="info-field"><div class="info-label">title</div><div class="info-value">'+esc(c.title||'—')+'</div></div>'
    +'<div class="info-field"><div class="info-label">type</div><div class="info-value">'+esc(c.type||'—')+'</div></div>'
    +'<div class="info-field"><div class="info-label">declarations</div><div class="info-value">'+esc((c.declarations||[]).join(', ')||'—')+'</div></div>'
    +'<div class="info-field"><div class="info-label">summary</div><div class="info-value">'+esc(c.summary||'—')+'</div></div>'
    +'<div class="info-field"><div class="info-label">status</div><div class="info-value"><span class="status-dot" style="background:'+col.bg+';border:1px solid '+col.border+'"></span>'+esc(statusLabel)+'</div></div>'
    +'<div class="info-field"><div class="info-label">lean file</div><div class="info-value">'+esc(c.lean_file||'—')+(c.lean_decl_lines?' : '+c.lean_decl_lines[0]+'–'+c.lean_decl_lines[1]:'')+'</div></div>'
    +'<div class="info-field"><div class="info-label">dependencies</div><div class="info-value">'+esc(((Array.isArray(c.dependencies)?c.dependencies:(c.dependencies?.local||[]))).join(', ')||'none')+'</div></div>'
    +'<div class="info-field"><div class="info-label">forum</div><div class="info-value"><a class="info-link" href="/#'+esc(c.id)+'" target="_blank">'+esc(c.id)+' &rarr;</a></div></div>';
  document.getElementById('info-panel').classList.add('visible');
}

function closePanel() {
  openId = null;
  document.getElementById('info-panel').classList.remove('visible');
  if (cy) cy.$(':selected').unselect();
}

async function loadDag(forceRebuild) {
  const res = await fetch('/api/dag');
  const waiting = document.getElementById('waiting');
  if (!res.ok) { waiting.style.display='block'; return; }
  waiting.style.display = 'none';
  const data = await res.json();
  const sig = (data.chunks||[]).map(c=>c.id).sort().join(',');
  if (forceRebuild || sig !== lastSig) { buildGraph(data); lastSig = sig; }
  else updateColors(data);
  updateHeaderLegend(data);
}

function connect() {
  const es = new EventSource('/api/events');
  es.onopen = () => { document.getElementById('status').textContent = 'live'; };
  es.onmessage = () => { loadDag(false); };
  es.onerror = () => { document.getElementById('status').textContent = 'reconnecting...'; es.close(); setTimeout(connect, 3000); };
}

loadDag(true); connect();
</script>
</body>
</html>
"""


# ── Routes ────────────────────────────────────────────────────────────────────



@app.get("/graph", response_class=HTMLResponse)
def graph():
    return _embeddable(GRAPH_HTML)


@app.get("/dag", response_class=HTMLResponse)
def dag():
    return _embeddable(DAG_HTML)


WORKSPACE_HTML = """\
<!DOCTYPE html>
<html>
<head>
<title>unity — workspace</title>
<meta charset="utf-8">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: ui-monospace, 'Cascadia Code', 'Fira Code', 'Menlo', monospace; font-size: 13px; background: #fafafa; color: #111; }
header { display: flex; align-items: center; justify-content: space-between; padding: 10px 20px; border-bottom: 1px solid #e4e4e4; background: #fafafa; position: sticky; top: 0; }
header h1 { font-size: 14px; font-weight: 600; letter-spacing: 0.14em; }
nav { display: flex; gap: 16px; }
nav a { font-size: 12px; color: #888; text-decoration: none; }
nav a:hover { color: #111; }
#status { font-size: 11px; color: #bbb; }
main { padding: 18px 20px; display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; align-items: start; }
section { background: #fff; border: 1px solid #e4e4e4; border-radius: 6px; padding: 12px 14px; min-width: 0; overflow-wrap: anywhere; }
section h2 { font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase; color: #bbb; margin-bottom: 8px; }
.item { padding: 6px 0; border-top: 1px solid #f2f2f2; line-height: 1.45; overflow-wrap: anywhere; word-break: break-word; }
.item:first-of-type { border-top: none; }
.who { color: #999; font-size: 11px; }
.chunk-card { margin-bottom: 10px; }
.chunk-name { font-weight: 600; font-size: 12px; }
.badge { display: inline-block; font-size: 10px; border-radius: 4px; padding: 1px 6px; margin-left: 6px; }
.ok { background: #e8f5e9; color: #1b5e20; }
.blocked { background: #ffebee; color: #b71c1c; }
.pending { background: #f5f5f5; color: #888; }
.kind { font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em; color: #888; margin-right: 6px; }
.stats { display: flex; flex-wrap: wrap; gap: 8px; }
.stat { border: 1px solid #eee; border-radius: 4px; padding: 2px 8px; font-size: 11px; color: #666; }
.empty { color: #ccc; padding: 8px 0; }
</style>
</head>
<body>
<header>
  <h1>unity — workspace</h1>
  <div style="display:flex;gap:16px;align-items:center">
    <nav>
      <a href="/">← app</a>
      <a href="/forum">forum →</a>
      <a href="/graph">graph →</a>
      <a href="/dag">dag →</a>
    </nav>
    <span id="status">connecting...</span>
  </div>
</header>
<main id="main"></main>
<script>
const esc = t => (t || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function badge(r) {
  if (r.mergeable) return '<span class="badge ok">mergeable</span>';
  if (r.open_objections.length) return '<span class="badge blocked">' + r.open_objections.length + ' objection(s)</span>';
  return '<span class="badge pending">needs endorsement</span>';
}
async function refresh() {
  try {
    const d = await (await fetch('/api/workspace')).json();
    document.getElementById('status').textContent = 'live';
    let h = '';
    h += '<section><h2>Binding decisions</h2>' + (d.decisions.length ? d.decisions.map(x =>
      '<div class="item"><b>' + esc(x.topic) + '</b>: ' + esc(x.choice) +
      (x.rationale ? ' — <span class="who">' + esc(x.rationale) + '</span>' : '') +
      ' <span class="who">(' + esc(x.author) + ')</span></div>').join('') : '<div class="empty">none yet</div>') + '</section>';
    h += '<section><h2>Chunk consensus</h2>' + (d.chunks.length ? d.chunks.map(c =>
      '<div class="chunk-card"><span class="chunk-name">' + esc(c.chunk) + '</span>' +
      c.results.map(r => '<div class="item">' + esc(r.author) + ': ' + esc(r.status) +
        (r.build_ok ? ' ✓build' : '') + badge(r) +
        (r.endorsements.length ? ' <span class="who">endorsed by ' + esc(r.endorsements.join(', ')) + '</span>' : '') +
        r.open_objections.map(o => '<div class="who">⛔ ' + esc(o.by) + ': ' + esc(o.reason) + '</div>').join('') +
        '</div>').join('') +
      (c.claims.length ? '<div class="item who">open claims: ' + c.claims.map(cl => esc(cl.author) +
        (cl.strategy ? ' (' + esc(cl.strategy) + ')' : '')).join(', ') + '</div>' : '') +
      '</div>').join('') : '<div class="empty">no typed chunk activity yet</div>') + '</section>';
    h += '<section><h2>Open obstacles</h2>' + (d.chunks.some(c => c.obstacles.length) ?
      d.chunks.flatMap(c => c.obstacles.map(o => '<div class="item">' + esc(o.content) +
      ' <span class="who">(' + esc(o.author) + ')</span></div>')).join('') : '<div class="empty">none open</div>') + '</section>';
    h += '<section><h2>Open questions</h2>' + (d.questions.length ? d.questions.map(q =>
      '<div class="item">' + esc(q.content) + ' <span class="who">(' + esc(q.author) + ')</span></div>').join('')
      : '<div class="empty">none open</div>') + '</section>';
    h += '<section><h2>Ledger</h2>' + (d.ledger.length ? d.ledger.map(l =>
      '<div class="item"><span class="kind">' + esc(l.kind) + '</span>' + esc(l.content) +
      (l.goal_shape ? ' <span class="who">[' + esc(l.goal_shape) + ']</span>' : '') + '</div>').join('')
      : '<div class="empty">no verified knowledge yet</div>') + '</section>';
    h += '<section><h2>Latest handoffs</h2>' + (d.handoffs.length ? d.handoffs.map(x =>
      '<div class="item">' + esc(x.content) + '</div>').join('') : '<div class="empty">none yet</div>') + '</section>';
    h += '<section><h2>Telemetry (posts by act)</h2><div class="stats">' +
      Object.entries(d.by_act).map(([k, v]) => '<span class="stat">' + esc(k) + ': ' + v + '</span>').join('') +
      '</div></section>';
    document.getElementById('main').innerHTML = h;
  } catch (e) {
    document.getElementById('status').textContent = 'disconnected';
  }
}
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


@app.get("/workspace", response_class=HTMLResponse)
def workspace():
    return _embeddable(WORKSPACE_HTML)





# ── App APIs (project control center) ─────────────────────────────────────────

import base64
import os
import sys

_run_state: dict = {"proc": None}

_COMMANDS: dict = {
    # command -> which extra inputs it takes
    "autoformalize": {"targets": False, "metric": False, "version": False},
    "formalize":     {"targets": True, "metric": False, "version": False},
    "prove":         {"targets": True, "metric": False, "version": False},
    "solve":         {"targets": False, "metric": False, "version": False},
    "create":        {"targets": False, "metric": False, "version": False},
    "verify":        {"targets": True, "metric": False, "version": False},
    "bump":          {"targets": False, "metric": False, "version": True},
    "optimize":      {"targets": True, "metric": True, "version": False},
}


from fastapi import Request
from fastapi.responses import JSONResponse as _JR


@app.exception_handler(ValueError)
async def _value_error_handler(request: Request, exc: ValueError):
    return _JR({"error": str(exc)}, status_code=400)


def _project_root() -> Path:
    return ROOT_DIR.parent


def _safe_unity_path(rel: str) -> Path:
    q = (ROOT_DIR / rel).resolve()
    if not str(q).startswith(str(ROOT_DIR.resolve())):
        raise ValueError("path escapes .unity")
    return q


def _forum_nonempty() -> bool:
    for tp in FORUM_DIR.glob("*.json"):
        if tp.name.startswith("_") or tp.name in ("config.json", "balances.json"):
            continue
        try:
            if json.loads(tp.read_text()).get("posts"):
                return True
        except Exception:
            continue
    return False


def _active_metric() -> str:
    f = ROOT_DIR / "metrics" / ".active"
    return f.read_text().strip() if f.exists() else ""


# Run state is persisted to .unity/logs/webrun.json so the status pill and the
# stop button keep working across `unity serve` restarts (pid-based, process-group kill).

def _run_state_path() -> Path:
    return ROOT_DIR / "logs" / "webrun.json"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def _current_run() -> dict:
    """The last-launched run, reconstructed from disk: works even if serve restarted."""
    try:
        st = json.loads(_run_state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {"running": False, "pid": None, "command": None, "started": 0.0,
                "log": None, "exit_code": None}
    proc = _run_state["proc"]
    if proc is not None and proc.pid == st.get("pid"):
        code = proc.poll()
        st.update(running=code is None, exit_code=code)
    else:  # serve was restarted: we only know liveness, not the exit code
        st.update(running=_pid_alive(st.get("pid")), exit_code=None)
    return st


@app.get("/api/project")
def api_project():
    dag = ROOT_DIR / "dag.json"
    chunk_ids = []
    if dag.exists():
        try:
            chunk_ids = [c.get("id") for c in json.loads(dag.read_text()).get("chunks", [])]
        except Exception:
            pass
    return {"name": _project_root().name,
            "continue": _forum_nonempty(),
            "has_dag": dag.exists(), "chunks": chunk_ids,
            "active_metric": _active_metric(),
            "commands": _COMMANDS,
            "metrics": sorted(p.name for p in (ROOT_DIR / "metrics").glob("*.md")) if (ROOT_DIR / "metrics").exists() else []}


@app.get("/api/unityfile")
def api_file_get(name: str):
    if name not in ("UNITY.md", "agents.yaml"):
        return JSONResponse({"error": "file not editable"}, status_code=400)
    q = _safe_unity_path(name)
    return {"name": name, "content": q.read_text() if q.exists() else ""}


from fastapi import Body


@app.put("/api/unityfile")
def api_file_put(payload: dict = Body(...)):
    name = payload.get("name", "")
    if name not in ("UNITY.md", "agents.yaml"):
        return JSONResponse({"error": "file not editable"}, status_code=400)
    q = _safe_unity_path(name)
    q.write_text(payload.get("content", ""))
    return {"ok": True}


@app.get("/api/env")
def api_env_get():
    q = ROOT_DIR / ".env"
    return {"content": q.read_text() if q.exists() else ""}


@app.put("/api/env")
def api_env_put(payload: dict = Body(...)):
    (ROOT_DIR / ".env").write_text(payload.get("content", ""))
    return {"ok": True}


@app.get("/api/agents")
def api_agents_get():
    q = ROOT_DIR / "agents.yaml"
    raw = q.read_text() if q.exists() else ""
    groups = []
    try:
        import yaml
        doc = yaml.safe_load(raw) or {}
        groups = doc.get("agents") or []
    except Exception:
        pass
    return {"raw": raw, "groups": groups}


def _serialize_groups(groups: list) -> str:
    import yaml
    clean = []
    for g in groups:
        entry = {}
        for k in ("name", "names", "model", "backend", "provider", "primary", "strength",
                  "budget", "base_url", "api_key", "auth_token"):
            v = g.get(k)
            if v in (None, "", [], False):
                continue
            entry[k] = v
        clean.append(entry)
    header = ("# First agent is the primary (preparation, critic, retrospective).\n"
              "# strength: static capability tier; budget: USD per instance.\n\n")
    return header + yaml.safe_dump({"agents": clean}, sort_keys=False)


@app.put("/api/agents")
def api_agents_put(payload: dict = Body(...)):
    q = ROOT_DIR / "agents.yaml"
    if "raw" in payload:
        q.write_text(payload["raw"])
        return {"ok": True}
    q.write_text(_serialize_groups(payload.get("groups") or []))
    return {"ok": True}


@app.post("/api/agents/convert")
def api_agents_convert(payload: dict = Body(...)):
    """Stateless converter for the two-way editor sync: groups -> raw, or raw -> groups.
    Writes nothing."""
    if "groups" in payload:
        return {"raw": _serialize_groups(payload.get("groups") or [])}
    import yaml
    try:
        doc = yaml.safe_load(payload.get("raw", "")) or {}
        groups = doc.get("agents") or []
        if not isinstance(groups, list):
            raise ValueError("'agents' must be a list")
        return {"groups": groups}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/sources")
def api_sources():
    d = ROOT_DIR / "source"
    d.mkdir(exist_ok=True)
    out = []
    for p in sorted(d.rglob("*")):
        if p.is_file():
            out.append({"name": str(p.relative_to(d)), "size": p.stat().st_size})
    return {"files": out}


@app.get("/api/sources/file")
def api_source_get(name: str):
    q = _safe_unity_path(f"source/{name}")
    if not q.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    data = q.read_bytes()
    try:
        return {"name": name, "text": data.decode("utf-8"), "binary": False}
    except UnicodeDecodeError:
        return {"name": name, "text": f"(binary file, {len(data)} bytes)", "binary": True}


@app.post("/api/sources/file")
def api_source_add(payload: dict = Body(...)):
    name = Path(payload.get("name", "")).name
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    q = _safe_unity_path(f"source/{name}")
    q.parent.mkdir(parents=True, exist_ok=True)
    if "content_b64" in payload:
        q.write_bytes(base64.b64decode(payload["content_b64"]))
    else:
        q.write_text(payload.get("content", ""))
    return {"ok": True, "name": name}


@app.put("/api/sources/file")
def api_source_put(payload: dict = Body(...)):
    name = payload.get("name", "")
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    q = _safe_unity_path(f"source/{name}")
    if not q.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    q.write_text(payload.get("content", ""))
    return {"ok": True}


@app.delete("/api/sources/file")
def api_source_del(name: str):
    q = _safe_unity_path(f"source/{name}")
    if q.exists():
        q.unlink()
    return {"ok": True}


@app.get("/api/metrics")
def api_metrics():
    d = ROOT_DIR / "metrics"
    d.mkdir(exist_ok=True)
    return {"files": sorted(p.name for p in d.iterdir() if p.is_file() and not p.name.startswith(".")),
            "active": _active_metric()}


@app.get("/api/metrics/file")
def api_metric_get(name: str):
    q = _safe_unity_path(f"metrics/{Path(name).name}")
    if not q.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"name": Path(name).name, "content": q.read_text(errors="replace")}


@app.put("/api/metrics/file")
def api_metric_put(payload: dict = Body(...)):
    name = Path(payload.get("name", "")).name
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    q = _safe_unity_path(f"metrics/{name}")
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text(payload.get("content", ""))
    return {"ok": True}


@app.delete("/api/metrics/file")
def api_metric_del(name: str):
    q = _safe_unity_path(f"metrics/{Path(name).name}")
    if q.exists():
        q.unlink()
    if _active_metric() == Path(name).stem:
        (ROOT_DIR / "metrics" / ".active").unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/metrics/active")
def api_metric_active(payload: dict = Body(...)):
    d = ROOT_DIR / "metrics"
    d.mkdir(exist_ok=True)
    name = Path(payload.get("name") or "").stem
    if not name or payload.get("clear"):
        (d / ".active").unlink(missing_ok=True)
        return {"ok": True, "active": ""}
    (d / ".active").write_text(name)
    return {"ok": True, "active": name}


_METRIC_TEMPLATE = """# {name}

## Prompt
<what to optimize for, and how to judge an improvement>

## Examples
(none)

## Score function
(none)

## Metric function
(none)
"""


@app.post("/api/metrics/new")
def api_metric_new(payload: dict = Body(...)):
    name = Path(payload.get("name", "")).stem.lower()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    q = _safe_unity_path(f"metrics/{name}.md")
    if q.exists():
        return JSONResponse({"error": "metric exists"}, status_code=400)
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text(_METRIC_TEMPLATE.format(name=name))
    return {"ok": True, "name": f"{name}.md"}


import re as _re

_DECL_RE = _re.compile(
    r"^(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+|noncomputable\s+|partial\s+|unsafe\s+|scoped\s+)*"
    r"(theorem|lemma|def|abbrev|structure|inductive|class|instance|opaque|axiom)\s+([A-Za-z0-9_.'₀-₉]+)",
    _re.M)


_IDENT_RE = _re.compile(r"[A-Za-z_][A-Za-z0-9_.'₀-₉]*")


def _blueprint_trees() -> list[str]:
    """Selectable sources for the blueprint: the main checkout + live agent worktrees."""
    trees = ["main"]
    wd = _project_root() / ".worktrees"
    if wd.is_dir():
        trees += sorted(p.name for p in wd.iterdir() if p.is_dir())
    return trees


def _blueprint_base(tree: str) -> Path:
    if tree in ("", "main"):
        return _project_root()
    base = (_project_root() / ".worktrees" / tree).resolve()
    if base.parent != (_project_root() / ".worktrees").resolve() or not base.is_dir():
        raise ValueError(f"unknown tree '{tree}'")
    return base


def _scan_blueprint(base: Path) -> list[tuple[str, list[dict], dict]]:
    """Parse `base`'s .lean files into (relpath, decls, bodies-by-name) triples."""
    parsed = []
    for p in sorted(base.rglob("*.lean")):
        if any(part in (".lake", ".unity", ".worktrees", "lake-packages", "build") for part in p.parts):
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        matches = list(_DECL_RE.finditer(text))
        decls, bodies = [], {}
        for i, m in enumerate(matches):
            body = text[m.start():matches[i + 1].start() if i + 1 < len(matches) else len(text)]
            kind, name = m.group(1), m.group(2)
            status = ("axiom" if kind == "axiom"
                      else "sorry" if _re.search(r"\bsorry\b|\badmit\b", body) else "complete")
            decls.append({"name": name, "kind": kind, "status": status,
                          "line": text.count("\n", 0, m.start()) + 1})
            bodies[name] = body
        if decls:
            parsed.append((str(p.relative_to(base)), decls, bodies))
    # naive in-project deps: identifiers in a decl's body that name another project decl
    all_names = {d["name"] for _, decls, _ in parsed for d in decls}
    used_by: dict = {}
    for _, decls, bodies in parsed:
        for d in decls:
            deps = sorted((set(_IDENT_RE.findall(bodies[d["name"]])) & all_names) - {d["name"]})
            d["deps"] = deps[:12]
            for n in deps:
                used_by.setdefault(n, []).append(d["name"])
    for _, decls, _ in parsed:
        for d in decls:
            d["used_by"] = len(used_by.get(d["name"], []))
    return parsed


# ── Kernel-exact blueprint (LeanArchitect's mechanism without its annotations) ────
# unity/blueprint_extract.lean loads the project's compiled modules and dumps every
# project decl with kernel-derived kind / sorry usage / used-constant deps. Slow-ish
# and needs a built project, so it runs in a background thread, is cached against the
# newest .lean mtime, and the regex scan serves whenever it's stale or unavailable.

import threading

_KERNEL: dict = {"running": False}
_KERNEL_LOCK = threading.Lock()


def _kernel_cache_path() -> Path:
    return ROOT_DIR / "blueprint-kernel.json"


def _src_stamp(base: Path) -> float:
    stamp = 0.0
    extra = [base / n for n in ("lakefile.toml", "lakefile.lean", "lean-toolchain", "lake-manifest.json")]
    for p in list(base.rglob("*.lean")) + extra:
        if any(part in (".lake", ".unity", ".worktrees", "lake-packages", "build") for part in p.parts):
            continue
        try:
            stamp = max(stamp, p.stat().st_mtime)
        except OSError:
            pass
    return stamp


def _kernel_extract(base: Path) -> dict | None:
    """Run the bundled extractor; None when the project isn't built, the toolchain is
    too old for the script, or anything else fails (caller keeps the regex scan)."""
    mods = []
    for p in base.rglob("*.lean"):
        if any(part in (".lake", ".unity", ".worktrees", "lake-packages", "build") for part in p.parts):
            continue
        mods.append(".".join(p.relative_to(base).with_suffix("").parts))
    if not mods:
        return None
    script = Path(__file__).parent.parent / "blueprint_extract.lean"
    env = dict(os.environ)
    env["PATH"] = env.get("PATH", "") + os.pathsep + str(Path.home() / ".elan" / "bin")
    try:
        r = subprocess.run(["lake", "env", "lean", "--run", str(script)] + sorted(mods),
                           cwd=base, capture_output=True, text=True, timeout=300, env=env)
        if r.returncode != 0:
            return None
        rows = json.loads(r.stdout.strip().splitlines()[-1])
        return {row["name"]: row for row in rows}
    except Exception:
        return None


def _kernel_refresh(stamp: float) -> None:
    data = _kernel_extract(_project_root())
    with _KERNEL_LOCK:
        _KERNEL.update(running=False, stamp=stamp, data=data, attempt_ts=time.time())
    try:
        _kernel_cache_path().write_text(json.dumps({"stamp": stamp, "decls": data,
                                                    "attempt_ts": time.time()}))
    except OSError:
        pass


def _kernel_data() -> tuple[dict | None, bool]:
    """(fresh kernel data for the main tree | None, refresh-in-flight?). A stale cache
    kicks off a background re-extract; a recorded failed attempt is not retried until
    the sources change again."""
    stamp = _src_stamp(_project_root())
    with _KERNEL_LOCK:
        if "stamp" not in _KERNEL:  # first call after serve start: load persisted cache
            try:
                saved = json.loads(_kernel_cache_path().read_text())
                _KERNEL.update(stamp=saved.get("stamp", 0.0), data=saved.get("decls"),
                               attempt_ts=saved.get("attempt_ts", 0.0))
            except (OSError, json.JSONDecodeError):
                _KERNEL.update(stamp=0.0, data=None, attempt_ts=0.0)
        if _KERNEL["stamp"] == stamp:
            # a failed extraction (e.g. project mid-build) retries after a cooldown
            if _KERNEL["data"] is not None or _KERNEL["running"] or \
                    time.time() - _KERNEL.get("attempt_ts", 0.0) < 120:
                return _KERNEL["data"], _KERNEL["running"]
        if not _KERNEL["running"]:
            _KERNEL["running"] = True
            threading.Thread(target=_kernel_refresh, args=(stamp,), daemon=True).start()
        return None, True


def _apply_kernel(files: list, kernel: dict) -> None:
    """Overlay kernel-exact deps + status onto the regex scan. Kernel names are fully
    qualified; regex names are as written — match exactly or by unique '.name' suffix.
    Adds the 'tainted' status: proof complete but transitively resting on a sorry/axiom."""
    regex_names = {d["name"] for f in files for d in f["decls"]}
    to_regex: dict = {}
    for kname in kernel:
        if kname in regex_names:
            to_regex[kname] = kname
        else:
            tails = [r for r in regex_names if kname.endswith("." + r)]
            if len(tails) == 1:
                to_regex[kname] = tails[0]
    to_kernel = {v: k for k, v in to_regex.items()}

    def direct_bad(k: str) -> bool:
        return kernel[k]["sorried"] or kernel[k]["kind"] == "axiom"

    memo: dict = {}

    def tainted(k: str, seen: tuple = ()) -> bool:
        if k in memo:
            return memo[k]
        if k in seen:
            return False
        bad = any(direct_bad(d) or tainted(d, seen + (k,))
                  for d in kernel[k]["deps"] if d in kernel)
        memo[k] = bad
        return bad

    for f in files:
        for d in f["decls"]:
            k = to_kernel.get(d["name"])
            if not k:
                continue
            row = kernel[k]
            d["deps"] = sorted({to_regex[x] for x in row["deps"] if x in to_regex})
            if row["sorried"]:
                d["status"] = "sorry"
            elif row["kind"] == "axiom":
                d["status"] = "axiom"
            elif tainted(k):
                d["status"] = "tainted"
            else:
                d["status"] = "complete"
    counts: dict = {}
    for f in files:
        for d in f["decls"]:
            for x in d["deps"]:
                counts[x] = counts.get(x, 0) + 1
    for f in files:
        for d in f["decls"]:
            d["used_by"] = counts.get(d["name"], 0)


def _blueprint_files(tree: str):
    """(files, bodies-by-(path,name), source, refreshing) — regex scan with the kernel
    overlay applied when fresh exact data exists (main tree only)."""
    parsed = _scan_blueprint(_blueprint_base(tree))
    files = [{"path": path, "decls": decls} for path, decls, _ in parsed]
    bodies = {(path, d["name"]): bs[d["name"]] for path, decls, bs in parsed for d in decls}
    source, refreshing = "regex", False
    if tree in ("", "main"):
        kernel, refreshing = _kernel_data()
        if kernel:
            _apply_kernel(files, kernel)
            source = "kernel"
    return files, bodies, source, refreshing


@app.get("/api/blueprint")
def api_blueprint(tree: str = "main"):
    """The Lean blueprint: every declaration in the selected tree's .lean files, with
    proof status (complete / tainted / sorry / axiom) and in-project dependency links —
    kernel-exact when the project builds, regex-approximate otherwise. This is the
    project's actual Lean structure, distinct from the run's chunk DAG."""
    files, _, source, refreshing = _blueprint_files(tree)
    total = sum(len(f["decls"]) for f in files)
    counts = {s: sum(1 for f in files for d in f["decls"] if d["status"] == s)
              for s in ("sorry", "axiom", "tainted")}
    return {"files": files, "total": total, "sorries": counts["sorry"],
            "axioms": counts["axiom"], "tainted": counts["tainted"],
            "source": source, "refreshing": refreshing,
            "tree": tree or "main", "trees": _blueprint_trees()}


def _chunk_for_decl(name: str) -> dict | None:
    """Find the chunk in dag.json that covers a declaration, if any."""
    try:
        chunks = json.loads((ROOT_DIR / "dag.json").read_text()).get("chunks", [])
    except (OSError, json.JSONDecodeError):
        return None
    short = name.split(".")[-1]
    for c in chunks:
        decls = c.get("declarations") or []
        if (name in decls or short in decls or c.get("lean_decl") in (name, short)
                or c.get("title") in (name, short)):
            return {"id": c.get("id"), "title": c.get("title", ""), "status": c.get("status", "")}
    for c in chunks:  # weaker match: the decl name inside the chunk id or statement
        if short and (short.lower() in str(c.get("id", "")).lower()
                      or _re.search(r"\b" + _re.escape(short) + r"\b", str(c.get("statement", "")))):
            return {"id": c.get("id"), "title": c.get("title", ""), "status": c.get("status", "")}
    return None


@app.get("/api/blueprint/decl")
def api_blueprint_decl(name: str, file: str, tree: str = "main"):
    """Detail for one declaration: signature, full source, deps both ways (kernel-exact
    when available), chunk link."""
    files, bodies, source, _ = _blueprint_files(tree)
    for f in files:
        if f["path"] != file:
            continue
        for d in f["decls"]:
            if d["name"] != name:
                continue
            body = bodies[(file, name)]
            # signature: decl head up to the proof/definition body
            sig_end = _re.search(r":=|\bby\b|\bwhere\b", body)
            signature = body[:sig_end.start()].rstrip() if sig_end else body.splitlines()[0]
            used_by = sorted({x["name"] for f2 in files for x in f2["decls"]
                              if name in x["deps"]})
            return {**{k: d[k] for k in ("name", "kind", "status", "line", "deps")},
                    "file": file, "signature": signature[:2000], "source_kind": source,
                    "source": body.rstrip()[:6000], "truncated": len(body) > 6000,
                    "used_by": used_by[:20], "chunk": _chunk_for_decl(name)}
    return JSONResponse({"error": f"declaration '{name}' not found in {file}"}, status_code=404)


@app.get("/api/tools")
def api_tools():
    """Aggregated tool-call telemetry from .unity/logs/tools.jsonl: agent × tool counts."""
    f = ROOT_DIR / "logs" / "tools.jsonl"
    counts: dict = {}
    last_ts = None
    if f.exists():
        for line in f.read_text(errors="replace").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (e.get("agent", "?"), e.get("tool", "?"))
            counts[key] = counts.get(key, 0) + 1
            last_ts = e.get("ts", last_ts)
    rows = [{"agent": a, "tool": t, "count": c} for (a, t), c in counts.items()]
    rows.sort(key=lambda r: -r["count"])
    return {"rows": rows, "last_ts": last_ts}


@app.get("/api/logs")
def api_logs():
    d = ROOT_DIR / "logs"
    if not d.exists():
        return {"files": []}
    files = [{"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime}
             for p in d.iterdir() if p.is_file() and p.suffix in (".log", ".jsonl")]
    return {"files": sorted(files, key=lambda f: -f["mtime"])}


_ANSI_RE = None


def _strip_ansi(text: str) -> str:
    global _ANSI_RE
    if _ANSI_RE is None:
        import re
        _ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
    return _ANSI_RE.sub("", text)


@app.get("/api/logs/file")
def api_log_get(name: str):
    q = _safe_unity_path(f"logs/{Path(name).name}")
    if not q.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    data = q.read_bytes()
    clipped = len(data) > 200_000
    return {"name": Path(name).name, "clipped": clipped,
            "text": _strip_ansi(data[-200_000:].decode("utf-8", errors="replace"))}


@app.get("/api/run")
def api_run_status():
    st = _current_run()
    tail = ""
    if st.get("log") and Path(st["log"]).exists():
        data = Path(st["log"]).read_bytes()
        tail = _strip_ansi(data[-4000:].decode("utf-8", errors="replace"))
    phase = None
    if st["running"]:
        try:
            phase = json.loads((ROOT_DIR / "state.json").read_text()).get("phase")
        except (OSError, json.JSONDecodeError):
            pass
    return {"running": st["running"], "command": st.get("command"),
            "started": st.get("started", 0.0), "exit_code": st.get("exit_code"),
            "phase": phase, "stopping": st["running"] and (ROOT_DIR / "stop-requested").exists(),
            "log": Path(st["log"]).name if st.get("log") else None,
            "log_tail": tail}


@app.post("/api/run")
def api_run_start(payload: dict = Body(...)):
    command = payload.get("command", "")
    if command not in _COMMANDS:
        return JSONResponse({"error": f"unknown command '{command}'"}, status_code=400)
    if _current_run()["running"]:
        return JSONResponse({"error": "a run is already active"}, status_code=409)

    argv = [command]
    if command == "optimize":
        metric = payload.get("metric") or _active_metric()
        if not metric:
            return JSONResponse({"error": "optimize needs a metric (set one active or pass it)"},
                                status_code=400)
        argv.append(Path(metric).stem)
    if _COMMANDS[command].get("version"):
        ver = (payload.get("version") or "").strip()
        if ver:
            argv.append(ver)
    targets = (payload.get("targets") or "").strip()
    if targets and _COMMANDS[command]["targets"]:
        argv += ["--targets", ", ".join(t.strip() for t in targets.splitlines() if t.strip())]
    cont = payload.get("continue")
    if cont is None:
        cont = _forum_nonempty()
    if cont:
        argv.append("--continue")

    if payload.get("dry"):
        return {"ok": True, "dry": True, "argv": argv}

    logs = ROOT_DIR / "logs"
    logs.mkdir(exist_ok=True)
    log_path = logs / f"webrun-{int(time.time())}-{command}.log"
    code = ("import sys, json, os; sys.argv = ['unity'] + json.loads(os.environ['UNITY_WEB_ARGS']); "
            "from unity.cli import main; main()")
    env = {**os.environ, "UNITY_WEB_ARGS": json.dumps(argv)}
    (ROOT_DIR / "stop-requested").unlink(missing_ok=True)  # clear any stale safe-stop flag
    lf = open(log_path, "ab")
    # own process group so stop can kill the whole agent tree, not just the wrapper
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=str(_project_root()),
                            stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            env=env, start_new_session=True)
    _run_state["proc"] = proc
    _run_state_path().write_text(json.dumps(
        {"pid": proc.pid, "command": command, "started": time.time(), "log": str(log_path)}))
    return {"ok": True, "argv": argv, "log": str(log_path)}


@app.post("/api/run/stop")
def api_run_stop(payload: dict = Body(default={})):
    import signal
    st = _current_run()
    if not st["running"]:
        return {"ok": True, "stopped": False}
    if payload.get("mode", "safe") == "safe":
        # Safe stop: agents end at their next stream item, remaining phases are skipped,
        # worktrees get cleaned up. Force mode (second click) kills the process group.
        (ROOT_DIR / "stop-requested").write_text(str(time.time()))
        return {"ok": True, "stopping": True}
    pid = st["pid"]
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
        for _ in range(30):  # up to 3s of grace, then hard-kill the group
            time.sleep(0.1)
            if not _pid_alive(pid):
                break
        else:
            os.killpg(pgid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return {"ok": True, "stopped": True}




APP_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<title>unity</title>
<meta charset="utf-8">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
:root { --bg:#f7f7f8; --card:#ffffff; --ink:#26242b; --mut:#8e8c94; --line:#e8e7ea; --acc:#7c5cbf; --acc-soft:#f1ebfa; --acc-ink:#6b46a8; --ok:#2e7d32; --ok-soft:#e6f4ea; --warn:#b45309; --warn-soft:#fdf3dd; --bad:#b91c1c; --bad-soft:#fdeaea; --sans:-apple-system,'Inter','Segoe UI',Roboto,sans-serif; --mono:ui-monospace,'Cascadia Code','Fira Code','Menlo',monospace; }
body { font-family: var(--sans); font-size: 13.5px; background: var(--bg); color: var(--ink); }
.mono { font-family: var(--mono); }
header { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; padding: 9px 22px; border-bottom: 1px solid var(--line); background: var(--bg); position: sticky; top: 0; z-index: 5; }
.brand { display: flex; align-items: center; gap: 10px; white-space: nowrap; }
.brand b { font-family: var(--mono); font-size: 15px; font-weight: 700; }
.brand .sep { width: 1px; height: 18px; background: #d8d7dc; }
.brand .proj { font-family: var(--mono); font-size: 13px; color: var(--mut); }
.tabs { display: flex; gap: 2px; overflow-x: auto; max-width: 100%; scrollbar-width: none; margin: 0 auto; }
.tabs::-webkit-scrollbar { display: none; }
.tabs button { background: none; border: 1px solid transparent; cursor: pointer; font: inherit; font-size: 13px; padding: 5px 13px; color: #6e6c75; border-radius: 999px; white-space: nowrap; }
.tabs button:hover { color: var(--ink); }
.tabs button.active { background: var(--card); color: var(--ink); border-color: var(--line); box-shadow: 0 1px 3px rgba(0,0,0,0.07); font-weight: 600; }
nav { display: flex; gap: 10px; align-items: center; }
#status { font-family: var(--mono); font-size: 12px; color: var(--mut); white-space: nowrap; }
#runbtn { background: var(--acc); color: #fff; border: none; border-radius: 999px; font: inherit; font-size: 13px; font-weight: 600; padding: 6px 18px; cursor: pointer; }
#runbtn:hover { filter: brightness(1.07); }
#runbtn.running { background: var(--bad); }
#runbtn.stopping { background: var(--warn); }
#gearbtn { background: none; border: 1px solid var(--line); border-radius: 999px; font-size: 13px; padding: 5px 10px; cursor: pointer; color: #6e6c75; background: var(--card); }
#gearbtn:hover { color: var(--ink); }
.runwrap { position: relative; }
.runmenu { display: none; position: absolute; right: 0; top: 112%; background: var(--card); border: 1px solid var(--line); border-radius: 10px; min-width: 170px; box-shadow: 0 10px 30px rgba(0,0,0,0.12); z-index: 20; overflow: hidden; padding: 4px; }
.runwrap:hover .runmenu { display: block; }
.runwrap.running .runmenu { display: none; }
.runmenu div { padding: 7px 13px; cursor: pointer; font-size: 13px; color: #55535b; border-radius: 7px; font-family: var(--mono); }
.runmenu div:hover { background: var(--acc-soft); color: var(--acc-ink); }
main { padding: 0; }
.pane { padding: 26px 28px 40px; max-width: 1380px; margin: 0 auto; }
.pagehead { display: flex; align-items: baseline; gap: 14px; margin-bottom: 18px; }
.pagehead h1 { font-size: 24px; font-weight: 700; letter-spacing: -0.01em; }
.pagehead .ctx { margin-left: auto; font-family: var(--mono); font-size: 12.5px; color: var(--mut); }
.framewrap iframe { display: block; width: 100%; height: calc(100vh - 54px); border: none; background: var(--card); }
.toolbar { display: flex; gap: 8px; align-items: center; padding: 7px 22px; border-bottom: 1px solid var(--line); background: var(--bg); }
.framewrap .toolbar + iframe { height: calc(100vh - 96px); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; align-items: start; }
section, .card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 18px 20px; min-width: 0; overflow-wrap: anywhere; box-shadow: 0 1px 2px rgba(20,18,26,0.04); }
section h2, .card h2 { font-size: 11px; letter-spacing: 0.13em; text-transform: uppercase; color: #a3a1a9; margin-bottom: 12px; font-weight: 700; }
.item { padding: 8px 0; border-top: 1px solid #f1f0f3; line-height: 1.5; overflow-wrap: anywhere; }
.item:first-of-type { border-top: none; }
.who { color: #a3a1a9; font-size: 11.5px; }
.badge { display: inline-block; font-size: 10.5px; border-radius: 6px; padding: 2px 8px; margin-left: 6px; font-weight: 600; letter-spacing: 0.04em; }
.ok { background: var(--ok-soft); color: var(--ok); } .blocked { background: var(--bad-soft); color: var(--bad); } .pending { background: #f0eff2; color: #7c7a83; } .lav { background: var(--acc-soft); color: var(--acc-ink); } .amber { background: var(--warn-soft); color: var(--warn); }
.kind { font-size: 10px; text-transform: uppercase; color: #98968f; margin-right: 6px; font-weight: 700; }
textarea { width: 100%; min-height: 320px; font-family: var(--mono); font-size: 12.5px; border: 1px solid var(--line); border-radius: 10px; padding: 13px; background: var(--card); resize: vertical; line-height: 1.6; }
textarea:focus, input:focus, select:focus { outline: none; border-color: var(--acc); box-shadow: 0 0 0 3px rgba(124,92,191,0.14); }
input, select { font: inherit; font-size: 13px; border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px; background: var(--card); }
button.act { font: inherit; font-size: 13px; border: 1px solid #dcdbe0; background: var(--card); border-radius: 999px; padding: 5px 15px; cursor: pointer; color: #4c4a52; }
button.act:hover { border-color: #b8b6bf; color: var(--ink); }
button.primary { background: var(--acc); color: #fff; border-color: var(--acc); font-weight: 600; }
button.primary:hover { filter: brightness(1.07); }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 12px 0 2px; }
.seg { display: inline-flex; background: #efeef1; border-radius: 999px; padding: 3px; }
.seg button { background: none; border: none; font: inherit; font-size: 12.5px; padding: 4px 14px; border-radius: 999px; cursor: pointer; color: #6e6c75; }
.seg button.on { background: var(--card); color: var(--ink); font-weight: 600; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
/* overview */
.ov-top { display: grid; grid-template-columns: 2fr 1fr; gap: 16px; align-items: stretch; }
.stat-big { font-size: 44px; font-weight: 700; font-family: var(--mono); line-height: 1.05; }
.stat-sub { font-family: var(--mono); color: var(--mut); font-size: 13px; margin-top: 4px; }
.pbar { display: flex; height: 9px; border-radius: 6px; overflow: hidden; background: #efeef1; margin: 18px 0 10px; }
.pbar div { height: 100%; }
.leg { display: flex; gap: 18px; font-size: 12.5px; color: #6e6c75; flex-wrap: wrap; }
.leg b { color: var(--ink); }
.dotc { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: 1px; }
.sechead { display: flex; align-items: center; gap: 12px; margin: 26px 0 12px; font-size: 11px; letter-spacing: 0.13em; text-transform: uppercase; color: #a3a1a9; font-weight: 700; }
.sechead::after { content: ""; flex: 1; height: 1px; background: var(--line); }
.sechead .r { font-family: var(--mono); text-transform: none; letter-spacing: 0; font-weight: 400; color: var(--mut); }
.agrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.acard { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 14px 16px; box-shadow: 0 1px 2px rgba(20,18,26,0.04); }
.acard .top { display: flex; align-items: center; gap: 11px; padding-bottom: 11px; border-bottom: 1px solid #f1f0f3; margin-bottom: 10px; }
.avatar { width: 36px; height: 36px; border-radius: 9px; background: var(--acc-soft); color: var(--acc-ink); display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 15px; flex: none; }
.acard .nm { font-weight: 700; font-size: 14px; }
.acard .mdl { font-family: var(--mono); font-size: 11.5px; color: var(--mut); }
.astat { margin-left: auto; font-size: 12px; white-space: nowrap; }
.astat.working { color: var(--acc-ink); } .astat.reviewing { color: var(--warn); } .astat.idle { color: #9c9aa2; }
.chunklink { font-family: var(--mono); font-size: 12px; color: var(--acc-ink); }
/* blueprint */
.bp-decl { display: flex; gap: 9px; align-items: baseline; padding: 6px 0; border-top: 1px solid #f2f1f4; font-size: 13px; font-family: var(--mono); }
.bp-decl:first-of-type { border-top: none; }
.bp-decl:hover { background: #fafafb; cursor: pointer; }
.dot { width: 8px; height: 8px; border-radius: 50%; flex: none; align-self: center; }
.dot.complete { background: #34a853; } .dot.sorry { background: #e53935; } .dot.axiom { background: #fb8c00; } .dot.tainted { background: #f4b400; }
.bp-kind { color: #b0aeb6; font-size: 10px; text-transform: uppercase; width: 70px; flex: none; font-weight: 700; }
.bp-deps { color: #c2c0c8; font-size: 11px; margin-left: auto; text-align: right; }
.srcchip { font-family: var(--mono); font-size: 10.5px; letter-spacing: 0.08em; border: 1px solid var(--line); border-radius: 7px; padding: 3px 9px; color: #7c7a83; background: #fafafb; text-transform: uppercase; }
/* agents tab */
.agent-card { border: 1px solid var(--line); border-radius: 14px; padding: 15px 17px; margin-bottom: 13px; background: var(--card); position: relative; box-shadow: 0 1px 2px rgba(20,18,26,0.04); }
.agent-card.is-primary { border-color: var(--acc); box-shadow: 0 0 0 1px var(--acc) inset; }
.agent-card .fields { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 11px; }
.agent-card label { font-size: 10px; text-transform: uppercase; color: #aaa8b0; display: block; margin-bottom: 4px; font-weight: 700; letter-spacing: 0.07em; }
.pbadge { position: absolute; top: -9px; left: 14px; background: var(--acc); color: #fff; font-size: 9px; font-weight: 700; letter-spacing: 0.1em; padding: 2px 9px; border-radius: 6px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.chip { font-size: 12px; border: 1px solid #dcdbe0; border-radius: 999px; padding: 2px 11px; cursor: pointer; color: #63616a; background: var(--card); font-family: var(--mono); }
.chip.on { background: var(--acc); color: #fff; border-color: var(--acc); }
.modal { position: fixed; inset: 0; background: rgba(24,22,30,0.38); display: none; align-items: center; justify-content: center; z-index: 40; backdrop-filter: blur(2px); }
.modal.open { display: flex; }
.modal .box { background: var(--card); border-radius: 16px; padding: 22px 24px; width: min(720px, 92vw); max-height: 86vh; overflow-y: auto; box-shadow: 0 20px 60px rgba(0,0,0,0.22); }
.modal h3 { font-size: 15px; margin-bottom: 12px; font-weight: 700; }
pre.log { background: #1b1a20; color: #d8d6de; font-size: 11.5px; padding: 13px; border-radius: 10px; max-height: 50vh; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-family: var(--mono); }
.filelist { width: 100%; border-collapse: collapse; font-family: var(--mono); }
.filelist td { padding: 7px 12px 7px 0; border-top: 1px solid #f1f0f3; font-size: 12.5px; }
.filelist tr:first-child td { border-top: none; }
.empty { color: #c4c2ca; padding: 8px 0; }
pre.tail { background: #1b1a20; color: #d8d6de; font-size: 11.5px; padding: 13px; border-radius: 10px; height: 58vh; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-family: var(--mono); }
.envfield { margin-bottom: 13px; }
.envfield label { display: block; font-size: 11px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; color: #8e8c94; margin-bottom: 4px; }
.envfield input, .envfield select { width: 100%; font-family: var(--mono); }
.envfield .hint { font-size: 11.5px; color: #a3a1a9; margin-top: 3px; }
#toast { position: fixed; bottom: 24px; right: 24px; background: #26242b; color: #fff; font-size: 13px; padding: 10px 18px; border-radius: 10px; box-shadow: 0 8px 24px rgba(0,0,0,0.28); opacity: 0; transform: translateY(8px); transition: all .18s ease; pointer-events: none; z-index: 60; }
#toast.show { opacity: 1; transform: translateY(0); }
@media (max-width: 1000px) { .ov-top { grid-template-columns: 1fr; } header { padding: 8px 12px; gap: 10px; } .pane { padding: 18px 14px 30px; } }
</style>
</head>
<body>
<header>
  <div class="brand"><b>unity</b><span class="sep"></span><span class="proj" id="title"></span></div>
  <div class="tabs" id="tabs">
    <button data-tab="overview" class="active">overview</button>
    <button data-tab="blueprint">blueprint</button>
    <button data-tab="forum">forum</button>
    <button data-tab="chunks">chunks</button>
    <button data-tab="agents">agents</button>
    <button data-tab="prompt">prompt</button>
    <button data-tab="sources">sources</button>
    <button data-tab="metrics">metrics</button>
    <button data-tab="logs">logs</button>
  </div>
  <nav>
    <span id="status">idle</span>
    <div class="runwrap" id="runwrap">
      <button id="runbtn">run ▾</button>
      <div class="runmenu" id="runmenu"></div>
    </div>
    <button id="gearbtn" title="settings (.unity/.env)">⚙</button>
  </nav>
</header>
<main>
  <div id="tab-overview" class="pane"></div>
  <div id="tab-blueprint" class="pane" style="display:none"></div>
  <div id="tab-forum" class="framewrap" style="display:none">
    <div class="toolbar"><button class="act" id="forum-view-toggle">graph view</button></div>
    <iframe id="forum-frame" data-src="/forum"></iframe>
  </div>
  <div id="tab-chunks" class="framewrap" style="display:none"><iframe data-src="/dag"></iframe></div>
  <div id="tab-agents" class="pane" style="display:none"></div>
  <div id="tab-prompt" class="pane" style="display:none"></div>
  <div id="tab-sources" class="pane" style="display:none"></div>
  <div id="tab-metrics" class="pane" style="display:none"></div>
  <div id="tab-logs" class="pane" style="display:none"></div>
</main>

<div class="modal" id="runmodal"><div class="box">
  <h3 id="rm-title">run</h3>
  <div class="row"><label><input type="checkbox" id="rm-continue"> --continue</label>
    <span class="who" id="rm-continue-note"></span></div>
  <div id="rm-metric-row" class="row" style="display:none">metric:
    <select id="rm-metric"></select></div>
  <div id="rm-version-row" class="row" style="display:none">version:
    <input id="rm-version" placeholder="e.g. v4.16.0 (blank = from UNITY.md)" style="flex:1"></div>
  <div id="rm-targets-row" style="display:none">
    <div class="who">targets — one per line (empty = all)</div>
    <textarea id="rm-targets" style="min-height:90px"></textarea>
    <div class="chips" id="rm-chips"></div>
  </div>
  <div class="row"><button class="act primary" id="rm-start">start</button>
    <button class="act" onclick="document.getElementById('runmodal').classList.remove('open')">cancel</button>
    <span class="who" id="rm-err"></span></div>
</div></div>

<div class="modal" id="viewmodal"><div class="box">
  <h3 id="view-name"></h3>
  <pre class="log" id="view-body" style="background:#fafafb;color:var(--ink);border:1px solid var(--line)"></pre>
  <div class="row"><button class="act" onclick="document.getElementById('viewmodal').classList.remove('open')">close</button></div>
</div></div>

<div class="modal" id="envmodal"><div class="box">
  <h3>settings</h3>
  <div class="who" style="margin-bottom:14px">stored in <span class="mono">.unity/.env</span></div>
  <div class="envfield"><label>max attempts</label><input id="env-MAX_ATTEMPTS" type="number" min="1">
    <div class="hint">critic-loop cap per run (default 5)</div></div>
  <div class="envfield"><label>lean lsp port</label><input id="env-LEAN_LSP_PORT" type="number" min="1">
    <div class="hint">port for the lean-lsp server (default 8888)</div></div>
  <div class="envfield"><label>axle api key</label><input id="env-AXLE_API_KEY" type="password" autocomplete="off">
    <div class="hint">unlocks Axle Lean verification tools for all agents</div></div>
  <div class="envfield"><label>aristotle api key</label><input id="env-ARISTOTLE_API_KEY" type="password" autocomplete="off">
    <div class="hint">unlocks the Aristotle prover offload tools</div></div>
  <div class="row"><button class="act primary" id="env-save">save</button>
    <button class="act" onclick="document.getElementById('envmodal').classList.remove('open')">close</button></div>
</div></div>

<div class="modal" id="declmodal"><div class="box">
  <h3 id="dm-title" class="mono"></h3>
  <div class="who" id="dm-meta" style="margin-bottom:10px; line-height:1.6"></div>
  <pre class="log" id="dm-source" style="background:#fafafb;color:var(--ink);border:1px solid var(--line); max-height:46vh"></pre>
  <div class="row"><button class="act" onclick="document.getElementById('declmodal').classList.remove('open')">close</button></div>
</div></div>

<div id="toast"></div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dagre/0.8.5/dagre.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/cytoscape-dagre@2.5.0/cytoscape-dagre.js"></script>
<script>
const esc = t => (t || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const $ = id => document.getElementById(id);
let PROJECT = {commands:{}, chunks:[], metrics:[]};
const J = (url, opts) => fetch(url, opts).then(r => r.json());
const put = (url, body) => J(url, {method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
const post = (url, body) => J(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});

const TABS = ['overview','blueprint','forum','chunks','agents','prompt','sources','metrics','logs'];
document.querySelectorAll('#tabs button').forEach(b => b.onclick = () => {
  document.querySelectorAll('#tabs button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  TABS.forEach(t => $('tab-'+t).style.display = 'none');
  const t = b.dataset.tab; $('tab-'+t).style.display = 'block';
  const fr = document.querySelector('#tab-' + t + ' iframe');
  if (fr && !fr.getAttribute('src')) fr.src = fr.dataset.src;
  if (loaders[t]) loaders[t]();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.querySelectorAll('.modal.open').forEach(m => m.classList.remove('open'));
});
let toastTimer = null;
function toast(msg) {
  const el = $('toast'); el.textContent = msg; el.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove('show'), 1800);
}
function reltime(ts) {
  if (!ts) return '';
  const s = Date.now() / 1000 - ts;
  if (s < 90) return 'just now';
  if (s < 5400) return Math.round(s / 60) + 'm ago';
  if (s < 129600) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}
function pagehead(title, ctx) {
  return '<div class="pagehead"><h1>' + title + '</h1><span class="ctx">' + (ctx || '') + '</span></div>';
}

// ── settings (.env form) ──────────────────────────────────────────────────────
const ENV_KEYS = ['MAX_ATTEMPTS', 'LEAN_LSP_PORT', 'AXLE_API_KEY', 'ARISTOTLE_API_KEY'];
let envExtra = [];
$('gearbtn').onclick = async () => {
  const d = await J('/api/env');
  const vals = {}; envExtra = [];
  (d.content || '').split('\n').forEach(line => {
    const m = line.match(/^([A-Z_]+)=(.*)$/);
    if (m && ENV_KEYS.includes(m[1])) vals[m[1]] = m[2];
    else if (line.trim() && !line.trim().startsWith('#')) envExtra.push(line);
  });
  $('env-MAX_ATTEMPTS').value = vals.MAX_ATTEMPTS || '5';
  $('env-LEAN_LSP_PORT').value = vals.LEAN_LSP_PORT || '8888';
  $('env-AXLE_API_KEY').value = vals.AXLE_API_KEY || '';
  $('env-ARISTOTLE_API_KEY').value = vals.ARISTOTLE_API_KEY || '';
  $('envmodal').classList.add('open');
};
$('env-save').onclick = async () => {
  const lines = ENV_KEYS.map(k => k + '=' + $('env-' + k).value.trim()).concat(envExtra);
  await put('/api/env', {content: lines.join('\n') + '\n'});
  toast('settings saved');
};
$('forum-view-toggle').onclick = () => {
  const fr = $('forum-frame'), toGraph = !fr.src.includes('/graph');
  fr.src = toGraph ? '/graph' : '/forum';
  $('forum-view-toggle').textContent = toGraph ? 'forum view' : 'graph view';
};

// ── overview ──────────────────────────────────────────────────────────────────
async function loadOverview() {
  try {
    const [w, r, bp] = await Promise.all([J('/api/workspace'), J('/api/run'), J('/api/blueprint')]);
    const mins = r.running ? Math.floor(Date.now() / 1000 - r.started) / 60 | 0 : 0;
    const ctx = r.running ? ('running · unity ' + esc(r.command) + ' · ' + mins + 'm')
      : (r.command ? 'last run · ' + esc(r.command) : 'no runs yet');
    let h = pagehead('Overview', ctx);
    // top row: run status + obstacles
    const verified = bp.total - bp.sorries - bp.axioms - (bp.tainted || 0);
    const warn = (bp.tainted || 0) + bp.axioms, bad = bp.sorries;
    const pct = bp.total ? Math.round(100 * verified / bp.total) : 0;
    const big = r.running ? esc(r.phase || 'running') : 'idle';
    const sub = r.running ? ('unity ' + esc(r.command) + ' · ' + mins + 'm' + (r.stopping ? ' · stopping…' : ''))
      : (r.command ? 'last: unity ' + esc(r.command) + (r.exit_code === null ? '' : ' (exit ' + r.exit_code + ')') : 'press run to start');
    h += '<div class="ov-top"><div class="card"><h2>run status</h2>' +
      '<div class="stat-big">' + big + '</div><div class="stat-sub">' + sub + '</div>' +
      (bp.total ? '<div class="leg" style="margin-top:16px"><span><span class="dotc" style="background:#34a853"></span><b>' + verified + '</b> verified</span>' +
        (warn ? '<span><span class="dotc" style="background:#f4b400"></span><b>' + warn + '</b> pending</span>' : '') +
        (bad ? '<span><span class="dotc" style="background:#e53935"></span><b>' + bad + '</b> sorry</span>' : '') +
        '<span class="who">' + pct + '% of ' + bp.total + ' declarations</span></div>' : '') + '</div>';
    const attn = w.chunks.flatMap(c => c.obstacles.map(o => ({...o, chunk: c.chunk, color: '#e53935'})))
      .concat(w.questions.map(q => ({...q, chunk: q.chunk || '', color: '#f4b400'})));
    h += '<div class="card"><h2>open obstacles & questions</h2>' + (attn.length ? attn.slice(0, 6).map(o =>
      '<div class="item"><span class="dotc" style="background:' + o.color + '"></span>' + (o.chunk ? '<b class="mono">' + esc(o.chunk) + '</b>' : '') +
      '<div class="who" style="margin-top:2px">' + esc(o.content).slice(0, 160) + '</div></div>').join('')
      : '<div class="empty">none open</div>') + '</div></div>';
    // agents
    const ags = w.agents || [];
    const nWork = ags.filter(a => a.status === 'working').length, nRev = ags.filter(a => a.status === 'reviewing').length;
    h += '<div class="sechead">agents<span class="r">' + (ags.length ? (nWork + ' working · ' + nRev + ' reviewing · ' + (ags.length - nWork - nRev) + ' idle') : '') + '</span></div>';
    h += ags.length ? '<div class="agrid">' + ags.map(a =>
      '<div class="acard"><div class="top"><div class="avatar">' + esc((a.name || '?')[0].toUpperCase()) + '</div>' +
      '<div><span class="nm">' + esc(a.name) + '</span>' + (a.primary ? '<span class="badge lav">PRIMARY</span>' : '') +
      '<div class="mdl">' + esc(a.model) + '</div></div>' +
      '<span class="astat ' + a.status + '">● ' + a.status + '</span></div>' +
      (a.activity ? '<div style="font-size:12.5px">' + esc(a.activity).slice(0, 110) + '</div>' : '<div class="who">' + (a.status === 'idle' ? 'idle · awaiting assignment' : 'working') + '</div>') +
      (a.chunk ? '<div class="chunklink">→ ' + esc(a.chunk) + '</div>' : '') + '</div>').join('') + '</div>'
      : '<div class="card"><div class="empty">no agents yet — set up your roster in the agents tab</div></div>';
    // bottom row
    h += '<div class="grid" style="margin-top:26px">';
    h += '<section><h2>recent decisions</h2>' + (w.decisions.length ? w.decisions.slice(0, 5).map(x =>
      '<div class="item"><b class="mono">' + esc(x.topic) + '</b><div style="font-size:12.5px;margin-top:2px">' + esc(x.choice) + '</div>' +
      '<div class="who">' + esc(x.author) + ' · ' + reltime(x.ts) + '</div></div>').join('') : '<div class="empty">none yet</div>') + '</section>';
    const tl = await J('/api/tools');
    h += '<section><h2>tool usage</h2>' + (tl.rows.length ?
      '<table class="filelist">' + tl.rows.slice(0, 12).map(x =>
        '<tr><td>' + esc(x.agent) + '</td><td>' + esc(x.tool) + '</td><td class="who">' + x.count + '×</td></tr>').join('') + '</table>'
      : '<div class="empty">no tool calls recorded yet</div>') + '</section>';
    h += '<section><h2>latest handoffs</h2>' + (w.handoffs.length ? w.handoffs.map(x =>
      '<div class="item">' + esc(x.content).slice(0, 200) + '<div class="who">' + reltime(x.ts) + '</div></div>').join('') : '<div class="empty">none yet</div>') + '</section>';
    h += '</div>';
    $('tab-overview').innerHTML = h;
  } catch (e) { $('tab-overview').innerHTML = pagehead('Overview', '') + '<div class="card"><div class="empty">overview unavailable</div></div>'; }
}

// ── blueprint ─────────────────────────────────────────────────────────────────
let BP = {view: 'list', filter: 'all', cy: null, dagreWired: false};
function bpMatch(x) {
  if (BP.filter === 'all') return true;
  if (BP.filter === 'verified') return x.status === 'complete';
  return x.status !== 'complete';  // sorry / axiom / tainted
}
async function loadBlueprint() {
  const d = await J('/api/blueprint');
  if (d.error) return;
  const src = d.source === 'kernel' ? '<span class="srcchip" title="statuses and dependencies read from the compiled Lean environment" style="border-color:#bfe3c6;color:#2e7d32;background:#f0f9f1">kernel-verified</span>'
    : '<span class="srcchip" title="textual approximation — kernel data appears once the project builds">regex approx.</span>';
  const refr = d.refreshing ? ' <span class="who">kernel extraction running…</span>' : '';
  if (d.refreshing) setTimeout(() => { if ($('tab-blueprint').style.display !== 'none') loadBlueprint(); }, 8000);
  let h = pagehead('Blueprint', 'main · ' + (d.source === 'kernel' ? 'kernel' : 'regex'));
  h += '<div class="card" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">' +
    '<span class="mono">' + d.total + ' declarations</span>' +
    (d.sorries ? '<span class="mono" style="color:#e53935">· ' + d.sorries + ' sorry</span>' : '') +
    (d.tainted ? '<span class="mono" style="color:#b8860b">· ' + d.tainted + ' tainted</span>' : '') +
    (d.axioms ? '<span class="mono" style="color:#fb8c00">· ' + d.axioms + ' axiom</span>' : '') +
    src + refr + '<span style="flex:1"></span>' +
    (BP.view === 'list' ? '<span class="seg" id="bp-filter">' +
      ['all', 'verified', 'sorry'].map(f => '<button data-f="' + f + '"' + (BP.filter === f ? ' class="on"' : '') + '>' + f[0].toUpperCase() + f.slice(1) + '</button>').join('') +
      '</span>' : '') +
    '<button class="act" id="bp-toggle">' + (BP.view === 'list' ? 'graph view' : 'list view') + '</button></div>';
  if (!d.files.length) {
    h += '<div class="card" style="margin-top:14px"><div class="empty">no Lean declarations found</div></div>';
    $('tab-blueprint').innerHTML = h;
  } else if (BP.view === 'list') {
    h += d.files.map(f => {
      const decls = f.decls.filter(bpMatch);
      if (!decls.length) return '';
      return '<section style="margin-top:14px"><h2 class="mono" style="text-transform:none;letter-spacing:0">' + esc(f.path) + '</h2>' +
        decls.map(x =>
          '<div class="bp-decl" data-file="' + esc(f.path) + '" data-name="' + esc(x.name) + '">' +
          '<span class="dot ' + x.status + '"></span>' +
          '<span class="bp-kind">' + esc(x.kind) + '</span>' +
          '<span title="line ' + x.line + (x.deps.length ? ' — uses: ' + esc(x.deps.join(', ')) : '') + '">' + esc(x.name) + '</span>' +
          '<span class="bp-deps">' + (x.deps.length ? '→ ' + x.deps.length : '') + (x.used_by ? ' · used by ' + x.used_by : '') + '</span></div>'
        ).join('') + '</section>';
    }).join('');
    $('tab-blueprint').innerHTML = h;
    document.querySelectorAll('#tab-blueprint .bp-decl').forEach(el =>
      el.onclick = () => showDecl(el.dataset.file, el.dataset.name));
  } else {
    h += '<div class="card" style="margin-top:14px; padding:0"><div id="bp-cy" style="height:72vh"></div></div>';
    $('tab-blueprint').innerHTML = h;
    buildBpGraph(d);
  }
  document.querySelectorAll('#bp-filter button').forEach(b => b.onclick = () => { BP.filter = b.dataset.f; loadBlueprint(); });
  $('bp-toggle').onclick = () => { BP.view = BP.view === 'list' ? 'graph' : 'list'; loadBlueprint(); };
}
function buildBpGraph(d) {
  if (typeof cytoscape === 'undefined' || typeof cytoscapeDagre === 'undefined') {
    $('bp-cy').innerHTML = '<div class="empty" style="padding:20px">graph libraries unavailable (offline?) — use list view</div>';
    return;
  }
  if (!BP.dagreWired) { cytoscape.use(cytoscapeDagre); BP.dagreWired = true; }
  const COLORS = {complete: '#34a853', sorry: '#e53935', axiom: '#fb8c00', tainted: '#f4b400'};
  const names = new Set(); d.files.forEach(f => f.decls.forEach(x => names.add(x.name)));
  const elements = [];
  d.files.forEach(f => f.decls.forEach(x => {
    elements.push({data: {id: x.name, label: x.name.split('.').pop(), file: f.path,
                          borderColor: COLORS[x.status] || COLORS.complete}});
    x.deps.forEach(dep => { if (names.has(dep)) elements.push({data: {id: dep + '->' + x.name, source: dep, target: x.name}}); });
  }));
  BP.cy = cytoscape({
    container: $('bp-cy'), elements,
    style: [
      {selector: 'node', style: {'background-color': '#ffffff', 'border-color': 'data(borderColor)',
        'border-width': 2, 'label': 'data(label)', 'font-size': '11px',
        'font-family': 'ui-monospace, Menlo, monospace',
        'text-valign': 'center', 'text-halign': 'center', 'shape': 'roundrectangle',
        'color': '#26242b', 'padding': '9px 13px', 'min-zoomed-font-size': 7}},
      {selector: 'edge', style: {'curve-style': 'bezier', 'target-arrow-shape': 'triangle',
        'target-arrow-color': '#d0cfd4', 'line-color': '#d0cfd4', 'arrow-scale': 0.75, 'width': 1}},
    ],
    layout: {name: 'dagre', rankDir: 'TB', nodeSep: 30, rankSep: 60, padding: 24},
  });
  BP.cy.on('tap', 'node', e => showDecl(e.target.data('file'), e.target.id()));
}
async function showDecl(file, name) {
  const d = await J('/api/blueprint/decl?file=' + encodeURIComponent(file) + '&name=' + encodeURIComponent(name));
  if (d.error) return;
  $('dm-title').textContent = d.kind + ' ' + d.name;
  const chunk = d.chunk ? 'chunk: <b>' + esc(d.chunk.id) + '</b>' + (d.chunk.status ? ' (' + esc(d.chunk.status) + ')' : '') : 'no matching chunk';
  $('dm-meta').innerHTML = [
    'status: <b>' + esc(d.status) + '</b>', esc(d.file) + ':' + d.line, chunk,
    d.deps.length ? 'uses: ' + esc(d.deps.join(', ')) : 'uses: nothing in-project',
    d.used_by.length ? 'used by: ' + esc(d.used_by.join(', ')) : 'used by: nothing in-project',
  ].join('<br>');
  $('dm-source').textContent = d.source + (d.truncated ? '\n… (truncated)' : '');
  $('declmodal').classList.add('open');
}

// ── agents ────────────────────────────────────────────────────────────────────
const AG_F = ['model', 'backend', 'provider', 'budget', 'base_url', 'api_key', 'auth_token'];
const API_LABEL = {claude_code: 'anthropic', anthropic: 'anthropic', codex: 'openai', openai: 'openai'};
const AG_PRESETS = {
  'Claude (subscription)': {name: 'Ada', model: 'claude-opus-4-6', backend: 'anthropic', budget: 10},
  'Claude (API key)': {name: 'Grace', model: 'claude-sonnet-5', backend: 'anthropic', api_key: '${ANTHROPIC_API_KEY}', budget: 5},
  'Codex (subscription)': {name: 'Kurt', model: 'gpt-5.5-codex', backend: 'openai'},
  'Codex (OpenAI API)': {name: 'Karl', model: 'gpt-5.5-codex', backend: 'openai', api_key: '${OPENAI_API_KEY}'},
  'OpenRouter — Claude': {name: 'Emmy', model: 'anthropic/claude-sonnet-5', backend: 'anthropic', base_url: 'https://openrouter.ai/api', auth_token: '${OPENROUTER_API_KEY}'},
  'OpenRouter — non-Claude model': {name: 'Alan', model: 'qwen/qwen3-coder:free', backend: 'openai', base_url: 'https://openrouter.ai/api/v1', api_key: '${OPENROUTER_API_KEY}'},
  'FreeInference': {name: 'Sophie', model: 'glm-5.1', backend: 'openai', base_url: 'https://freeinference.org/v1', api_key: '${FREEINFERENCE_API_KEY}'},
  'Local vLLM': {name: 'Henri', model: 'my-model', backend: 'openai', base_url: 'http://localhost:8000/v1', api_key: 'unity'},
};
let agTimer = null, agLastEdited = 'cards';
function agCollect() {
  return [...document.querySelectorAll('.agent-card')].map(c => {
    const g = {};
    const get = f => { const el = c.querySelector('[data-f=' + f + ']'); return el ? el.value.trim() : ''; };
    const nm = get('name');
    if (nm.includes(',')) g.names = nm.split(',').map(x => x.trim()).filter(Boolean); else g.name = nm;
    AG_F.forEach(f => { const v = get(f); if (v) g[f] = (f === 'budget' ? parseFloat(v) : v); });
    if (c.dataset.primary === '1') g.primary = true;
    if (c.dataset.full === '1') g._full = true;
    return g;
  });
}
function agentCard(g) {
  const full = !!g._full;  // "+ new" seeds every field once; unused ones drop on save
  const inp = (f, v, type, label) => '<div><label>' + (label || f.replace('_', ' ')) + '</label><input data-f="' + f + '" value="' + esc(String(v ?? '')) + '"' + (type ? ' type="' + type + '"' : '') + ' style="width:100%"></div>';
  const prim = !!g.primary;
  const api = API_LABEL[g.backend] || 'anthropic';
  let h = '<div class="agent-card' + (prim ? ' is-primary' : '') + '" data-primary="' + (prim ? '1' : '0') + '" data-full="' + (full ? '1' : '0') + '">';
  if (prim) h += '<span class="pbadge">PRIMARY</span>';
  h += '<div class="fields">';
  h += inp('name', g.name || (g.names || []).join(', '));
  h += inp('model', g.model);
  h += '<div><label>api</label><select data-f="backend" style="width:100%">' +
    ['anthropic', 'openai'].map(o => '<option' + (o === api ? ' selected' : '') + '>' + o + '</option>').join('') + '</select></div>';
  h += inp('budget', g.budget, '', 'budget (usd)');
  const show = f => full || g[f] !== undefined;
  if (show('provider')) h += inp('provider', g.provider);
  if (show('base_url')) h += inp('base_url', g.base_url);
  if (show('api_key')) h += inp('api_key', g.api_key, 'password', 'api key');
  if (show('auth_token')) h += inp('auth_token', g.auth_token, 'password', full ? 'auth token' : 'api key');
  h += '</div><div class="row">' + (prim ? '' : '<button class="act ag-primary">set as primary</button>') +
       '<button class="act ag-del">remove</button></div></div>';
  return h;
}
function agRenderCards(groups) {
  groups = groups || [];
  if (groups.length && !groups.some(g => g.primary)) groups[0].primary = true;
  $('agent-cards').innerHTML = groups.map(g => agentCard(g)).join('');
  const cardIdx = b => [...document.querySelectorAll('.agent-card')].indexOf(b.closest('.agent-card'));
  document.querySelectorAll('.ag-del').forEach(b => b.onclick = () => { b.closest('.agent-card').remove(); agSyncRaw(); });
  document.querySelectorAll('.ag-primary').forEach(b => b.onclick = () => {
    const gs = agCollect(); gs.forEach(g => delete g.primary); gs[cardIdx(b)].primary = true;
    agRenderCards(gs); agSyncRaw();
  });
}
function agClean(gs) { return gs.map(g => { const x = {...g}; delete x._full; return x; }); }
async function agSyncRaw() {
  agLastEdited = 'cards';
  const r = await post('/api/agents/convert', {groups: agClean(agCollect())});
  if (document.activeElement !== $('ag-raw-text')) { $('ag-raw-text').value = r.raw; $('ag-err').textContent = ''; }
}
async function agSyncCards() {
  agLastEdited = 'raw';
  const r = await post('/api/agents/convert', {raw: $('ag-raw-text').value});
  if (r.error) { $('ag-err').textContent = 'yaml: ' + r.error; return; }
  $('ag-err').textContent = '';
  agRenderCards(r.groups);
}
async function loadAgents() {
  const d = await J('/api/agents');
  $('tab-agents').innerHTML = pagehead('Agents', (d.groups || []).length + ' configured') +
    '<div id="agent-cards"></div>' +
    '<div class="row"><select id="ag-preset"><option value="">add from preset…</option>' +
    Object.keys(AG_PRESETS).map(k => '<option>' + esc(k) + '</option>').join('') + '</select>' +
    '<button class="act" id="ag-add">+ new</button>' +
    '<button class="act primary" id="ag-save">save</button>' +
    '<button class="act" id="ag-raw-toggle">raw</button>' +
    '<span class="who" id="ag-err" style="color:#b91c1c"></span></div>' +
    '<div id="ag-raw" style="display:none;margin-top:10px"><textarea id="ag-raw-text"></textarea></div>';
  agRenderCards(d.groups);
  $('ag-raw-text').value = d.raw;
  $('agent-cards').addEventListener('input', () => { clearTimeout(agTimer); agTimer = setTimeout(agSyncRaw, 350); });
  $('ag-raw-text').addEventListener('input', () => { clearTimeout(agTimer); agTimer = setTimeout(agSyncCards, 500); });
  $('ag-add').onclick = () => { agRenderCards([...agCollect(), {name: 'agent', model: '', provider: '', base_url: '', api_key: '', auth_token: '', _full: true}]); agSyncRaw(); };
  $('ag-preset').onchange = e => {
    const g = AG_PRESETS[e.target.value]; e.target.value = '';
    if (!g) return;
    agRenderCards([...agCollect(), JSON.parse(JSON.stringify(g))]); agSyncRaw();
    toast('preset added — fill in the key (or set the env var)');
  };
  $('ag-raw-toggle').onclick = () => { const r = $('ag-raw'); r.style.display = r.style.display === 'none' ? 'block' : 'none'; };
  $('ag-save').onclick = async () => {
    clearTimeout(agTimer);
    if (agLastEdited === 'raw') await put('/api/agents', {raw: $('ag-raw-text').value});
    else await put('/api/agents', {groups: agClean(agCollect())});
    toast('agents saved');
    const wasOpen = $('ag-raw').style.display !== 'none';
    await loadAgents();
    if (wasOpen) $('ag-raw').style.display = 'block';
  };
}

// ── prompt ────────────────────────────────────────────────────────────────────
async function loadPrompt() {
  const d = await J('/api/unityfile?name=UNITY.md');
  $('tab-prompt').innerHTML = pagehead('Prompt', 'UNITY.md') +
    '<section><textarea id="um-text"></textarea><div class="row">' +
    '<button class="act primary" id="um-save">save</button></div></section>';
  $('um-text').value = d.content;
  $('um-save').onclick = async () => { await put('/api/unityfile', {name: 'UNITY.md', content: $('um-text').value});
    toast('prompt saved'); };
}

// ── sources ───────────────────────────────────────────────────────────────────
async function loadSources() {
  const d = await J('/api/sources');
  let h = pagehead('Sources', d.files.length + ' file(s)') + '<section><table class="filelist">';
  h += (d.files.length ? d.files.map(f => '<tr><td>' + esc(f.name) + '</td><td class="who">' + f.size + ' B</td>' +
    '<td><button class="act" onclick="editSource(\'' + esc(f.name) + '\')">edit</button></td>' +
    '<td><button class="act" onclick="delSource(\'' + esc(f.name) + '\')">remove</button></td></tr>').join('')
    : '<tr><td class="empty">no sources yet</td></tr>') + '</table>';
  h += '<div class="row" style="margin-top:12px"><input type="file" id="src-upload" multiple>' +
       '<span class="who">files are copied into .unity/source/</span></div>' +
       '<div id="src-editor" style="display:none;margin-top:10px"><div class="who mono" id="src-edit-name"></div>' +
       '<textarea id="src-text"></textarea><div class="row"><button class="act primary" id="src-save">save</button></div></div></section>';
  $('tab-sources').innerHTML = h;
  $('src-upload').onchange = async (e) => {
    for (const file of e.target.files) {
      const b64 = await new Promise(res => { const r = new FileReader(); r.onload = () => res(r.result.split(',')[1]); r.readAsDataURL(file); });
      await post('/api/sources/file', {name: file.name, content_b64: b64});
    }
    toast(e.target.files.length + ' file(s) uploaded');
    loadSources();
  };
  $('src-save').onclick = async () => {
    await put('/api/sources/file', {name: $('src-edit-name').textContent, content: $('src-text').value});
    toast('source saved');
  };
}
async function editSource(name) {
  const d = await J('/api/sources/file?name=' + encodeURIComponent(name));
  if (d.binary) { $('view-name').textContent = name; $('view-body').textContent = d.text; $('viewmodal').classList.add('open'); return; }
  $('src-editor').style.display = 'block'; $('src-edit-name').textContent = name; $('src-text').value = d.text;
}
async function delSource(name) {
  await fetch('/api/sources/file?name=' + encodeURIComponent(name), {method: 'DELETE'});
  toast('source removed'); loadSources();
}

// ── metrics ───────────────────────────────────────────────────────────────────
async function loadMetrics() {
  const d = await J('/api/metrics');
  let h = pagehead('Metrics', d.active ? 'active: ' + esc(d.active) : 'none active') + '<section><table class="filelist">';
  h += (d.files.length ? d.files.map(f => { const on = f.replace(/[.]md$/, '') === d.active; return '<tr><td>' + esc(f) + (on ? ' <span class="badge lav">ACTIVE</span>' : '') + '</td>' +
    '<td><button class="act" onclick="editMetric(\'' + esc(f) + '\')">edit</button></td>' +
    '<td><button class="act" onclick="' + (on ? 'unsetActive()' : 'setActive(\'' + esc(f) + '\')') + '">' + (on ? 'unset' : 'set active') + '</button></td>' +
    '<td><button class="act" onclick="delMetric(\'' + esc(f) + '\')">remove</button></td></tr>'; }).join('')
    : '<tr><td class="empty">no metrics</td></tr>') + '</table>';
  h += '<div class="row" style="margin-top:12px"><input id="mt-new-name" placeholder="new metric name">' +
       '<button class="act" id="mt-new">create</button></div>' +
       '<div id="mt-editor" style="display:none;margin-top:10px"><div class="who mono" id="mt-edit-name"></div>' +
       '<textarea id="mt-text"></textarea><div class="row"><button class="act primary" id="mt-save">save</button></div></div></section>';
  $('tab-metrics').innerHTML = h;
  $('mt-new').onclick = async () => { const n = $('mt-new-name').value.trim(); if (!n) return;
    await post('/api/metrics/new', {name: n}); toast('metric created'); loadMetrics(); };
  $('mt-save').onclick = async () => {
    await put('/api/metrics/file', {name: $('mt-edit-name').textContent, content: $('mt-text').value});
    toast('metric saved');
  };
}
async function editMetric(name) {
  const d = await J('/api/metrics/file?name=' + encodeURIComponent(name));
  $('mt-editor').style.display = 'block'; $('mt-edit-name').textContent = name; $('mt-text').value = d.content;
}
async function setActive(name) { await post('/api/metrics/active', {name}); toast('active metric: ' + name.replace(/[.]md$/, '')); loadMetrics(); }
async function unsetActive() { await post('/api/metrics/active', {clear: true}); toast('active metric cleared'); loadMetrics(); }
async function delMetric(name) { await fetch('/api/metrics/file?name=' + encodeURIComponent(name), {method: 'DELETE'}); toast('metric removed'); loadMetrics(); }

// ── logs ──────────────────────────────────────────────────────────────────────
let LOG_OPEN = null;
async function loadLogs() {
  const d = await J('/api/logs');
  const fmt = t => new Date(t * 1000).toLocaleString();
  $('tab-logs').innerHTML = pagehead('Logs', '.unity/logs/') + '<section><table class="filelist">' +
    (d.files.length ? d.files.map(f => '<tr><td>' + esc(f.name) + (f.name === RUN.log && RUN.running ? ' <span class="badge ok">live</span>' : '') + '</td>' +
      '<td class="who">' + f.size + ' B</td>' +
      '<td class="who">' + fmt(f.mtime) + ' (' + reltime(f.mtime) + ')</td>' +
      '<td><button class="act" onclick="openLog(\'' + esc(f.name) + '\')">view</button></td></tr>').join('')
      : '<tr><td class="empty">no logs yet</td></tr>') + '</table>' +
    '<div id="log-viewer" style="display:none;margin-top:12px"><div class="who mono" id="log-viewer-name"></div>' +
    '<pre class="tail" id="log-viewer-body"></pre></div></section>';
  if (LOG_OPEN) openLog(LOG_OPEN);
}
async function openLog(name) {
  LOG_OPEN = name;
  const d = await J('/api/logs/file?name=' + encodeURIComponent(name));
  const v = $('log-viewer'); if (!v) return;
  v.style.display = 'block';
  $('log-viewer-name').textContent = name + (d.clipped ? ' (last 200 KB)' : '') +
    (name === RUN.log && RUN.running ? ' — live, auto-refreshing' : '');
  const el = $('log-viewer-body'), stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 12 || !el.textContent;
  el.textContent = d.text || '(empty)';
  if (stick) el.scrollTop = el.scrollHeight;
}

// ── run ───────────────────────────────────────────────────────────────────────
function buildRunMenu() {
  $('runmenu').innerHTML = Object.keys(PROJECT.commands).map(c => '<div data-c="' + c + '">' + c + '</div>').join('');
  document.querySelectorAll('#runmenu div').forEach(el => el.onclick = () => openRunModal(el.dataset.c));
}
let RUN_CMD = null;
function openRunModal(cmd) {
  RUN_CMD = cmd;
  $('rm-title').textContent = 'run: unity ' + cmd;
  $('rm-continue').checked = PROJECT['continue'];
  $('rm-continue-note').textContent = PROJECT['continue'] ? '(auto-detected: forum has prior state)' : '(fresh run: forum empty)';
  const spec = PROJECT.commands[cmd];
  $('rm-metric-row').style.display = spec.metric ? 'flex' : 'none';
  if (spec.metric) {
    $('rm-metric').innerHTML = PROJECT.metrics.map(m => '<option' + (m.replace(/[.]md$/, '') === PROJECT.active_metric ? ' selected' : '') + '>' + esc(m) + '</option>').join('');
  }
  $('rm-version-row').style.display = spec.version ? 'flex' : 'none';
  $('rm-version').value = '';
  $('rm-targets-row').style.display = spec.targets ? 'block' : 'none';
  $('rm-targets').value = '';
  $('rm-chips').innerHTML = (PROJECT.chunks || []).map(c => '<span class="chip" data-c="' + esc(c) + '">' + esc(c) + '</span>').join('');
  document.querySelectorAll('#rm-chips .chip').forEach(ch => ch.onclick = () => {
    ch.classList.toggle('on');
    const t = $('rm-targets'); const lines = new Set(t.value.split('\n').filter(Boolean));
    if (ch.classList.contains('on')) lines.add(ch.dataset.c); else lines.delete(ch.dataset.c);
    t.value = [...lines].join('\n');
  });
  $('rm-err').textContent = '';
  $('runmodal').classList.add('open');
}
$('rm-start').onclick = async () => {
  const body = {command: RUN_CMD, 'continue': $('rm-continue').checked, targets: $('rm-targets').value};
  if (PROJECT.commands[RUN_CMD].metric) body.metric = $('rm-metric').value;
  if (PROJECT.commands[RUN_CMD].version) body.version = $('rm-version').value;
  const r = await post('/api/run', body);
  if (r.error) { $('rm-err').textContent = r.error; return; }
  $('runmodal').classList.remove('open');
  toast('run started: unity ' + r.argv.join(' '));
  pollRun();
};
let RUN = {running: false, stopping: false, log: null};
$('runbtn').onclick = async () => {
  if (!RUN.running) return;
  if (!RUN.stopping) {
    await post('/api/run/stop', {mode: 'safe'});
    toast('safe stop requested — agents finish their current turn');
  } else if (confirm('Force stop? This kills the whole run immediately.')) {
    await post('/api/run/stop', {mode: 'force'});
    toast('run force-stopped');
  }
  pollRun();
};
async function pollRun() {
  const d = await J('/api/run');
  RUN = {running: d.running, stopping: d.stopping, log: d.log};
  if (d.running) {
    $('runwrap').classList.add('running');
    const mins = Math.floor((Date.now() / 1000 - d.started) / 60);
    $('runbtn').classList.toggle('stopping', d.stopping);
    $('runbtn').classList.toggle('running', !d.stopping);
    $('runbtn').textContent = d.stopping ? 'force stop' : 'stop';
    $('status').textContent = d.command + (d.phase ? ' · ' + d.phase : '') + ' · ' + mins + 'm' +
      (d.stopping ? ' · stopping…' : '');
  } else {
    $('runwrap').classList.remove('running');
    $('runbtn').classList.remove('running', 'stopping'); $('runbtn').textContent = 'run ▾';
    $('status').textContent = d.command ? ('last: ' + d.command + (d.exit_code === null ? '' : ' (exit ' + d.exit_code + ')')) : 'idle';
  }
}

const loaders = {overview: loadOverview, blueprint: loadBlueprint, agents: loadAgents,
                 prompt: loadPrompt, sources: loadSources, metrics: loadMetrics, logs: loadLogs};
async function boot() {
  PROJECT = await J('/api/project');
  $('title').textContent = PROJECT.name;
  buildRunMenu();
  loadOverview();
  pollRun();
  setInterval(() => {
    pollRun();
    if ($('tab-overview').style.display !== 'none') loadOverview();
    if ($('tab-logs').style.display !== 'none' && LOG_OPEN && LOG_OPEN === RUN.log && RUN.running) openLog(LOG_OPEN);
  }, 5000);
}
boot();
</script>
</body>
</html>
"""


# When a page is embedded as an app-shell tab, drop its own header so navbars don't
# nest (and can't recurse via the "← app" link).
_EMBED_SNIPPET = ("<script>if(self!==top){var _h=document.querySelector('header');"
                  "if(_h)_h.remove();}</script>")


def _embeddable(html: str) -> str:
    return html.replace("</body>", _EMBED_SNIPPET + "\n</body>")


@app.get("/", response_class=HTMLResponse)
def app_shell():
    return APP_HTML


@app.get("/forum", response_class=HTMLResponse)
def forum_page():
    return _embeddable(FORUM_HTML)


# ── Entry point ───────────────────────────────────────────────────────────────

async def run(forum_dir: Path, root_dir: Path = Path("."), port: int = 8080) -> None:
    """Serve the dashboard against `forum_dir`/`root_dir` (runs in the caller's event loop)."""
    global FORUM_DIR, ROOT_DIR
    FORUM_DIR = Path(forum_dir)
    ROOT_DIR = Path(root_dir)
    FORUM_DIR.mkdir(parents=True, exist_ok=True)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="error")
    await uvicorn.Server(config).serve()


def main() -> None:
    import asyncio
    parser = argparse.ArgumentParser(description="Unity Forum Web UI")
    parser.add_argument("--forum-dir", default="forum")
    parser.add_argument("--root-dir", default=".", help="Root directory where dag.json and Lean files live")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    asyncio.run(run(args.forum_dir, args.root_dir, args.port))


if __name__ == "__main__":
    main()
