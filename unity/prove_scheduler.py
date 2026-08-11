"""Event-driven scheduler for the prove-only runtime."""

from __future__ import annotations

import asyncio
import math
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import asyncclick as click
from rich.console import Console

from . import library, worktree
from .config import Paths
from .orchestrator import build_mcp
from .prove_runtime import RuntimeStore, TASK_TERMINAL
from .roster import Agent, Roster
from .spawn import clear_task_context, get_completed_stats, set_task_context, spawn


_console = Console()


@dataclass
class RunningWorker:
    agent: Agent
    task_id: str
    worktree_name: str
    worktree_path: Path
    future: asyncio.Task
    started: float


def deterministic_setup(project_root: Path, *, build: bool = True) -> dict:
    """Inspect and validate the Lean environment without consuming a model call."""
    lakefile = next((p for p in (project_root / "lakefile.toml", project_root / "lakefile.lean")
                     if p.exists()), None)
    if lakefile is None:
        raise click.ClickException("prove needs a Lean project with lakefile.toml or lakefile.lean")
    toolchain = project_root / "lean-toolchain"
    info = {
        "lakefile": lakefile.name,
        "toolchain": toolchain.read_text().strip() if toolchain.exists() else "",
        "manifest": (project_root / "lake-manifest.json").exists(),
        "git_head": "",
        "build_ok": False,
    }
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=project_root,
                          text=True, capture_output=True)
    if head.returncode:
        raise click.ClickException("prove needs a Git commit so candidates can have immutable identities")
    info["git_head"] = head.stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                           cwd=project_root, text=True, capture_output=True).stdout.splitlines()
    relevant = []
    for line in dirty:
        changed = line[3:].replace("\\", "/")
        parts = changed.split("/")
        if any(token in parts for token in (".unity", ".worktrees")) or changed == ".gitignore":
            continue
        relevant.append(line)
    if relevant:
        preview = "\n".join(relevant[:12])
        raise click.ClickException(
            "prove requires committed source state for immutable verification; commit or stash these changes:\n"
            + preview)
    if build and os.getenv("UNITY_PROVE_SKIP_SETUP_BUILD", "").lower() not in {"1", "true", "yes"}:
        proc = subprocess.run(["lake", "build"], cwd=project_root, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        info["build_ok"] = proc.returncode == 0
        info["build_tail"] = "\n".join(proc.stdout.splitlines()[-20:])
        if proc.returncode:
            raise click.ClickException("deterministic setup build failed:\n" + info["build_tail"])
        post = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                              cwd=project_root, text=True, capture_output=True).stdout.splitlines()
        post_relevant = []
        for line in post:
            changed = line[3:].replace("\\", "/")
            parts = changed.split("/")
            if any(token in parts for token in (".unity", ".worktrees")) or changed == ".gitignore":
                continue
            post_relevant.append((line, changed))
        if post_relevant and all(path == "lake-manifest.json" for _, path in post_relevant):
            subprocess.run(["git", "add", "lake-manifest.json"], cwd=project_root, check=True)
            subprocess.run([
                "git", "-c", "user.name=Unity", "-c", "user.email=unity@local.invalid",
                "commit", "-m", "UNITY: deterministic Lake manifest setup", "--", "lake-manifest.json",
            ], cwd=project_root, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            info["setup_commit"] = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=project_root, text=True,
                capture_output=True, check=True).stdout.strip()
            info["git_head"] = info["setup_commit"]
        elif post_relevant:
            raise click.ClickException(
                "lake build unexpectedly changed project source/config:\n" +
                "\n".join(line for line, _ in post_relevant[:12]))
    else:
        info["build_ok"] = True
    return info


def _max_task_attempts() -> int:
    explicit = os.getenv("UNITY_PROVE_TASK_ATTEMPTS")
    if explicit:
        return max(1, int(explicit))
    raw = os.getenv("MAX_ATTEMPTS", "").strip()
    if raw:
        try:
            value = float(raw)
            if math.isfinite(value):
                return max(1, int(value))
        except ValueError:
            pass
    return 3


def _agent_preamble(agent: Agent, roster: Roster) -> str:
    capacity = ", ".join(f"{a.name} ({a.model})" for a in roster.agents)
    return (
        f"You are {agent.name}, using {agent.model}. The configured roster ({capacity}) is available "
        "capacity, not a fixed team that must all stay active. You have one assigned proof-search task. "
        "Authoritative state, leases, cancellation, candidates and reviews are controlled through the "
        "Forum tools. A cancelled heartbeat is binding: stop promptly. Do not infer current state from "
        "old conversation or shell transcript.\n\n"
    )


class ProveScheduler:
    def __init__(self, roster: Roster, paths: Paths, store: RuntimeStore, prompt: str):
        self.roster = roster
        self.paths = paths
        self.store = store
        self.prompt = prompt
        self.mcp = build_mcp(paths, store.run_dir / "forum")
        self.running: dict[str, RunningWorker] = {}
        self.max_task_attempts = _max_task_attempts()
        self.poll_seconds = float(os.getenv("UNITY_PROVE_POLL_SECONDS", "0.5"))

    def _available_agents(self) -> list[Agent]:
        busy = set(self.running)
        return [agent for agent in self.roster.agents if agent.name not in busy]

    def _choose_agent(self, task: dict, available: list[Agent], state: dict) -> Agent | None:
        candidates = list(available)
        if task["kind"] == "review" and task.get("parent_candidate"):
            author = state["candidates"].get(task["parent_candidate"], {}).get("author", "")
            candidates = [a for a in candidates if a.name.lower() != author.lower()]
        if not candidates:
            return None
        candidates.sort(key=lambda a: (a.cost, a.strength))
        if task["kind"] in {"review", "counterexample_search"}:
            return max(candidates, key=lambda a: (a.strength, -a.cost))
        failed = sum(1 for t in state["tasks"].values()
                     if t["goal_id"] == task["goal_id"] and t["kind"] == task["kind"]
                     and t["status"] == "failed")
        # Start cheaply; repeated failures route monotonically toward stronger capacity.
        return candidates[min(failed, len(candidates) - 1)]

    async def _worker_turn(self, agent: Agent, task: dict, wt_name: str, wt: Path) -> str | None:
        from .forum import server as forum_server
        forum_server.FORUM_DIR = self.store.run_dir / "forum"
        brief = forum_server.build_brief(agent.name, task["goal_id"])
        contract = (
            f"ASSIGNED TASK (already atomically claimed for you): {task['id']}\n"
            f"Goal: {task['goal_id']}\nKind: {task['kind']}\n"
            f"Description: {task['description']}\nStrategy: {task['strategy_key']}\n"
            f"Parent candidate: {task.get('parent_candidate') or '-'}\n"
            f"Parent objection: {task.get('parent_objection') or '-'}\n\n"
            "Call forum_task_heartbeat immediately and after long tool/build operations. Publish reusable "
            "intermediate knowledge with forum_finding. You may create genuinely independent tasks with "
            "forum_create_task; exact normalized duplicates will be rejected. Exploration, library/API "
            "search, scratch Lean, debugging and proof writing all happen in this one PROVE runtime.\n\n"
            "For a proof/fix: edit only relevant source, run useful local checks, commit the exact bytes, "
            "then immediately call forum_submit_candidate(goal_id, author, commit_sha, task_id). Your own build "
            "claim is never trusted; Unity verifies the immutable commit. A proof found during search is a "
            "final candidate—submit it directly.\n\n"
            "For review: inspect the exact candidate commit, source hash, verification record and patch. "
            "Call forum_endorse_candidate only for an independent correct/faithful review, otherwise call "
            "forum_object_candidate with a concrete evidenced defect; that automatically creates fix work.\n\n"
            "Before ending, complete or release the task through the structured API. Do not post a prove "
            "handoff; live state is the handoff.\n"
        )
        system = _agent_preamble(agent, self.roster) + "Workspace brief:\n" + brief + "\n\n" + self.prompt
        context = library.library_context()
        if context:
            system += "\n\n" + context
        return await spawn(agent, system, contract, wt, self.mcp,
                           subagents=library.library_subagents())

    async def _start(self, agent: Agent, task: dict) -> bool:
        claim = self.store.claim_task(task["id"], agent.name)
        if not claim.get("ok"):
            return False
        wt_name = f"{self.store.load()['run_id']}-{agent.name}-{task['id'][-6:]}"
        state = self.store.load()
        parent_id = claim["task"].get("parent_candidate")
        startpoint = state["candidates"].get(parent_id, {}).get("commit_sha", "HEAD")
        try:
            wt = await asyncio.to_thread(worktree.create_worktree, wt_name, self.paths.project_root,
                                         startpoint)
        except Exception as exc:
            self.store.finish_task(task["id"], agent.name, "failed",
                                   f"worktree setup failed: {exc}")
            self.store.event("worker_start_failed", agent=agent.name, task_id=task["id"],
                             error=repr(exc)[:500])
            return False
        worktree.symlink_lake_cache(wt, self.paths.project_root)
        worktree.link_shared_unity(wt, self.paths.unity)
        set_task_context(agent.name, task["id"], self.store.run_dir)
        future = asyncio.create_task(self._worker_turn(agent, claim["task"], wt_name, wt))
        self.running[agent.name] = RunningWorker(agent, task["id"], wt_name, wt, future, time.monotonic())
        self.store.set_worker(agent.name, task["id"], "active", agent.model)
        self.store.event("worker_started", agent=agent.name, model=agent.model, task_id=task["id"])
        return True

    async def _finish_worker(self, name: str, worker: RunningWorker) -> None:
        cancelled = worker.future.cancelled()
        error = ""
        if not cancelled:
            try:
                worker.future.result()
            except asyncio.CancelledError:
                cancelled = True
            except Exception as exc:
                error = repr(exc)
                _console.print(f"[red]prove worker {name} failed: {exc!r}[/red]")
        elapsed = time.monotonic() - worker.started
        state = self.store.load()
        task = state["tasks"].get(worker.task_id)
        if task and task["status"] == "claimed" and task.get("owner") == name:
            self.store.finish_task(worker.task_id, name, "failed",
                                   error or ("cancelled" if cancelled else "worker ended without structured completion"))
        self.store.record_task_usage(worker.task_id, elapsed, get_completed_stats(name),
                                     model=worker.agent.model, agent=name)
        clear_task_context(name)
        self.store.set_worker(name, None, "idle", worker.agent.model)
        self.store.event("worker_stopped", agent=name, task_id=worker.task_id,
                         cancelled=cancelled, seconds=round(elapsed, 3), error=error[:300])
        await asyncio.to_thread(worktree.cleanup_worktree, worker.worktree_name,
                                worker.worktree_path, self.paths.project_root)
        self.running.pop(name, None)

    async def _collect_finished(self) -> None:
        for name, worker in list(self.running.items()):
            if worker.future.done():
                await self._finish_worker(name, worker)

    async def _cancel_obsolete(self) -> None:
        state = self.store.load()
        for name, worker in list(self.running.items()):
            status = state["tasks"].get(worker.task_id, {}).get("status")
            if status in {"cancelled", "dominated", "superseded", "failed", "stale"} and not worker.future.done():
                worker.future.cancel()
                self.store.event("worker_cancel_signal", agent=name, task_id=worker.task_id, reason=status)
        await asyncio.sleep(0)

    async def _process_candidates(self) -> None:
        state = self.store.load()
        for candidate in list(state["candidates"].values()):
            if candidate["status"] == "submitted":
                _console.print(f"[cyan]verifying {candidate['id']} at {candidate['commit_sha'][:12]}[/cyan]")
                await asyncio.to_thread(self.store.verify_candidate, candidate["id"])
                await self._cancel_obsolete()
        state = self.store.load()
        for candidate in list(state["candidates"].values()):
            if candidate["status"] == "acceptable":
                result = await asyncio.to_thread(self.store.accept_candidate, candidate["id"])
                if result.get("ok"):
                    _console.print(f"[green]accepted {candidate['id']} for {result['goal_id']}[/green]")
                await self._cancel_obsolete()

    def _ensure_followup_work(self) -> None:
        state = self.store.load()
        for goal in state["goals"].values():
            if goal["status"] in {"closed", "review"}:
                continue
            related = [t for t in state["tasks"].values() if t["goal_id"] == goal["id"]]
            active_speculative = any(t["kind"] not in {"review", "verification"}
                                     and t["status"] in {"pending", "claimed", "stale"} for t in related)
            live_candidate = any(c["goal_id"] == goal["id"] and c["status"] not in
                                 {"failed", "rejected", "superseded"} for c in state["candidates"].values())
            attempts = sum(1 for t in related if t["kind"] == "proof_attempt")
            if not active_speculative and not live_candidate and attempts < self.max_task_attempts:
                self.store.create_task(
                    goal["id"], "proof_attempt", f"Prove {goal['declaration']} (escalation {attempts + 1})",
                    "runtime", strategy_key=f"direct-proof-escalation-{attempts + 1}",
                )

    async def _enforce_budgets(self) -> None:
        state = self.store.load()
        now = time.time()
        wall_limit = state["policy"]["task_wall_seconds"]
        token_limit = int(os.getenv("UNITY_PROVE_TASK_TOKEN_LIMIT", "1000000"))
        tool_limit = int(os.getenv("UNITY_PROVE_TASK_TOOL_LIMIT", "1000"))
        for task in state["tasks"].values():
            if task["status"] != "claimed" or not task.get("owner"):
                continue
            wall = now - (task.get("claimed_at") or now)
            stagnant = now - (task.get("last_progress_at") or task.get("claimed_at") or now)
            reason = ""
            if wall > wall_limit:
                reason = f"stagnant: wall budget {wall_limit}s exceeded"
            elif stagnant > wall_limit:
                reason = f"stagnant: no meaningful progress for {wall_limit}s"
            elif task.get("tokens", 0) > token_limit:
                reason = f"stagnant: token budget {token_limit} exceeded"
            elif task.get("tool_calls", 0) > tool_limit:
                reason = f"stagnant: tool-call budget {tool_limit} exceeded"
            if reason:
                self.store.finish_task(task["id"], task["owner"], "failed", reason)
                self.store.event("task_stagnant", task_id=task["id"], reason=reason)
        await self._cancel_obsolete()

    async def _allocate(self) -> None:
        state = self.store.load()
        tasks = [t for t in state["tasks"].values() if t["status"] in {"pending", "stale"}
                 and all(state["tasks"].get(dep, {}).get("status") == "complete"
                         or state["goals"].get(state["tasks"].get(dep, {}).get("goal_id", ""), {}).get("status") == "closed"
                         for dep in t["dependencies"])]
        priority = {"review": 0, "debugging": 1, "dependency_resolution": 2,
                    "verification": 3, "proof_attempt": 4, "library_search": 5,
                    "api_search": 6, "decomposition": 7, "counterexample_search": 8,
                    "formalization": 9}
        tasks.sort(key=lambda t: (priority.get(t["kind"], 20), t["created_at"]))
        available = self._available_agents()
        for task in tasks:
            if not available:
                break
            latest = self.store.load()
            goal = latest["goals"][task["goal_id"]]
            if goal["status"] == "closed":
                self.store.cancel_task(task["id"], "goal already closed")
                continue
            if goal["status"] == "review" and task["kind"] not in {"review", "verification", "debugging"}:
                self.store.cancel_task(task["id"], "verified candidate dominates speculative work", "dominated")
                continue
            agent = self._choose_agent(task, available, latest)
            if agent is None:
                continue
            if await self._start(agent, task):
                available.remove(agent)

    def _exhausted(self, state: dict) -> bool:
        if self.running:
            return False
        if any(c["status"] in {"submitted", "acceptable"} for c in state["candidates"].values()):
            return False
        runnable_tasks = [t for t in state["tasks"].values()
                          if t["status"] in {"pending", "stale", "claimed"}
                          and (t["status"] == "claimed" or all(
                              state["tasks"].get(dep, {}).get("status") == "complete"
                              or state["goals"].get(
                                  state["tasks"].get(dep, {}).get("goal_id", ""), {}).get("status") == "closed"
                              for dep in t["dependencies"]))]
        runnable = bool(runnable_tasks)
        if runnable:
            # A lone reviewer cannot independently review its own result. This is a
            # genuine capacity exhaustion, not a reason to weaken the policy.
            for task in runnable_tasks:
                if task["kind"] != "review":
                    return False
                cand = state["candidates"].get(task.get("parent_candidate") or "", {})
                if any(a.name.lower() != str(cand.get("author", "")).lower() for a in self.roster.agents):
                    return False
            return True
        for goal in state["goals"].values():
            attempts = sum(1 for t in state["tasks"].values()
                           if t["goal_id"] == goal["id"] and t["kind"] == "proof_attempt")
            if goal["status"] == "open" and attempts < self.max_task_attempts:
                return False
        return True

    async def run(self) -> dict:
        terminal_status: str | None = None
        try:
            while True:
                if (self.paths.unity / "stop-requested").exists():
                    terminal_status = "stopped"
                    for worker in self.running.values():
                        worker.future.cancel()
                    await asyncio.gather(*(self._finish_worker(n, w) for n, w in list(self.running.items())))
                    break
                await self._collect_finished()
                await self._process_candidates()
                self.store.expire_stale_claims()
                await self._enforce_budgets()
                self._ensure_followup_work()
                await self._allocate()
                state = self.store.load()
                if all(g["status"] == "closed" for g in state["goals"].values()):
                    terminal_status = "complete"
                    break
                if self._exhausted(state):
                    terminal_status = "exhausted"
                    break
                await asyncio.sleep(self.poll_seconds)
        finally:
            for worker in self.running.values():
                worker.future.cancel()
            for name, worker in list(self.running.items()):
                await self._finish_worker(name, worker)
        if terminal_status is not None:
            # Termination telemetry includes cancellation and worktree cleanup,
            # making verified-to-termination a real post-solution spend measure.
            self.store.mark_status(terminal_status)
        return self.store.load()
