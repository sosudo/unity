"""Compiler-driven Bump controller. No original-meaning preservation contract.

The controller owns configuration, builds and Git integration. Agents own leased
files in private worktrees and communicate through the Bump Forum. This is the
only workflow selected by ``unity bump``; legacy receipts are not reinterpreted.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from threading import Event
import uuid
from copy import deepcopy

CONFIG_FILES = ("lean-toolchain", "lakefile.lean", "lakefile.toml", "lake-manifest.json")
_CAPACITY_COOLDOWN_SECONDS = 60.0
_ASSISTANCE_SECONDS = 300.0

from . import artifacts, bump_jobs, bump_worktree
from .bump_spawn import BumpProviderCapacityError, SpawnCleanupError, spawn
from .config import Paths


def paths_for(root):
    paths = Paths.from_unity_dir(Path(root).resolve() / ".unity")
    return replace(paths, forum=paths.unity / "forum" / "bump")


def git(root, *args, check=True, input=None):
    result = bump_jobs.run(Path(root), ["git", "-c", "core.hooksPath=/dev/null",
                                      "-c", "commit.gpgsign=false", *args], cwd=Path(root), input=input)
    if check and result.returncode:
        raise ValueError(result.stderr.strip() or "Bump Git operation failed")
    return result.stdout.strip() if check else result


def accepted_rename_paths(root, state, task):
    """Find a recorded, integrated Git move for an obsolete command task."""
    if task.get("kind") != "command" or task.get("chunk_ids"):
        return {}
    selected = state["config"].get("scope", {}).get("selected_modules", {})
    missing = {path for path in task.get("files", []) if not (root / path).is_file()}
    if not missing:
        return {}
    aliases = {}
    for candidate in state.get("candidates", {}).values():
        parent = state["tasks"].get(candidate.get("task_id"), {})
        merged = candidate.get("merged_sha")
        if (candidate.get("status") != "merged" or not merged
                or parent.get("kind") not in {"configuration", "dependency", "scope_review"}
                or parent.get("revision") != candidate.get("task_revision")
                or git(root, "merge-base", "--is-ancestor", merged, state["main_sha"], check=False).returncode):
            continue
        mapping = parent.get("requested_config", {}).get("module_renames", {})
        if not isinstance(mapping, dict):
            continue
        diff = git(root, "diff", "--name-status", "--find-renames=50%", "-z",
                   merged + "^", merged, check=False)
        if diff.returncode:
            continue
        entries = iter(diff.stdout.split("\0"))
        moves = {}
        try:
            for status in entries:
                if not status:
                    break
                old = next(entries)
                if status.startswith(("R", "C")):
                    new = next(entries)
                    if status.startswith("R"):
                        moves[old] = new
        except StopIteration:
            continue
        for old, new in mapping.items():
            if not isinstance(old, str) or not isinstance(new, str):
                continue
            old_path = old.replace(".", "/") + ".lean"
            new_path = selected.get(new)
            if (old_path in missing and new_path and new_path in task["files"]
                    and moves.get(old_path) == new_path and (root / new_path).is_file()):
                evidence = {"path": new_path, "candidate_id": candidate["candidate_id"],
                            "merged_sha": merged, "old_module": old, "new_module": new}
                if old_path in aliases and aliases[old_path] != evidence:
                    return {}
                aliases[old_path] = evidence
    if len({item["path"] for item in aliases.values()}) != len(aliases):
        return {}
    return aliases if set(aliases) == missing else {}


def write_json(path, value):
    artifacts._atomic_write(Path(path), (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


@contextmanager
def merge_lock(paths):
    paths.forum.mkdir(parents=True, exist_ok=True)
    with (paths.forum / "integration.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def stopped(paths):
    return (paths.unity / "stop-requested").exists()


def phase(paths, value):
    from . import bump_tasks
    with bump_tasks.transaction(paths.forum) as state:
        state["phase"] = value
    write_json(paths.unity / "state.json", {"command": "bump", "phase": value,
                                          "workflow": "compiler-migration-v2"})


def mcp_servers(paths, profile):
    servers = {
        "unity-forum": {"command": sys.executable, "args": [
            "-m", "unity.forum.bump_workflow_server", "--forum-dir", str(paths.forum),
            "--project-root", str(paths.project_root), "--profile", profile]},
        "lean-lsp": {"command": "uvx", "args": ["lean-lsp-mcp"]},
    }
    if os.environ.get("AXLE_API_KEY"):
        servers["axle"] = {"command": "uvx", "args": ["--from", "axiom-axle-mcp", "axle-mcp-server"],
                           "env": {"AXLE_API_KEY": os.environ["AXLE_API_KEY"]}}
    if os.environ.get("ARISTOTLE_API_KEY"):
        servers["aristotle"] = {"command": sys.executable, "args": ["-m", "unity.aristotle"],
                                "env": {"ARISTOTLE_API_KEY": os.environ["ARISTOTLE_API_KEY"]}}
    return servers


def runtime_env(paths, author, task_id, profile, config, *, session_token=None):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", author)
    scratch = paths.unity / "tmp" / "bump" / safe
    scratch.mkdir(parents=True, exist_ok=True)
    for name in ("uv-tools", "uv-bin", "lake-cache"):
        (scratch / name).mkdir(exist_ok=True)
    setup_path = paths.unity / "bump" / "setup.json"
    setup = json.loads(setup_path.read_text()) if setup_path.exists() else {}
    real_lake = (setup.get("toolchain", {}).get("lake", {}).get("executable")
                 or os.environ.get("UNITY_REAL_LAKE") or shutil.which("lake"))
    bin_dir = paths.unity / "bin" / "bump-workflow"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = f"#!{sys.executable}\nfrom unity.bump_lake_guard import main\nraise SystemExit(main())\n"
    wrapper = bin_dir / "lake"
    if not wrapper.exists() or wrapper.read_text() != script:
        artifacts._atomic_write(wrapper, script.encode())
        wrapper.chmod(0o700)
    env = {
        "TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch),
        "UV_TOOL_DIR": str(scratch / "uv-tools"), "UV_TOOL_BIN_DIR": str(scratch / "uv-bin"),
        "LAKE_CACHE_DIR": str(scratch / "lake-cache"),
        "UNITY_AGENT_NAME": author, "UNITY_BUMP_TASK_ID": task_id,
        "UNITY_BUMP_PROJECT_ROOT": str(paths.project_root), "UNITY_BUMP_PROFILE": profile,
        "UNITY_BUMP_WORKFLOW": "compiler-migration-v2",
        "UNITY_BUMP_PROCESS_RECEIPTS": str(paths.forum / "processes"),
        "UNITY_BUMP_LAKE_ARGS": json.dumps(config.get("lake_args", [])),
        "PATH": os.pathsep.join((str(bin_dir), str(Path(sys.executable).absolute().parent), os.environ.get("PATH", ""))),
        "PIP_REQUIRE_VIRTUALENV": "true", "PIP_DISABLE_PIP_VERSION_CHECK": "true",
    }
    if session_token:
        env["UNITY_BUMP_WORKER_SESSION"] = session_token
    if real_lake:
        env["UNITY_REAL_LAKE"] = str(Path(real_lake).absolute())
    return env


def task_worktree(paths, author, task_id):
    """A new owned attempt never resets or discards another attempt's work."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", author)
    name = safe + "-" + uuid.uuid4().hex[:12]
    tree = bump_worktree.create_worktree(name, paths.project_root)
    bump_worktree.symlink_lake_cache(tree, paths.project_root)
    return tree


async def stop_worker(task, interrupt, *, grace=5.0, hard=35.0):
    """Never release a file lease before its actual worker cleanup has finished."""
    if task.done():
        return
    interrupt.set()
    done, _ = await asyncio.wait({task}, timeout=grace)
    if not done:
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=hard)
    if not done:
        raise SpawnCleanupError("Bump worker cleanup exceeded its bounded shutdown deadline")
    if not task.cancelled():
        exception = task.exception()
        if isinstance(exception, SpawnCleanupError):
            raise exception


class Controller:
    def __init__(self, paths, roster, *, max_attempts=5, agent_locks=None,
                 dependency_runs=None, ancestors=(), resume_checkpoint=False):
        self.paths, self.roster, self.max_attempts = paths, roster, max_attempts
        self.agent_locks = agent_locks if agent_locks is not None else {
            agent.name: asyncio.Lock() for agent in roster.agents}
        self.dependency_runs = dependency_runs if dependency_runs is not None else {}
        self.ancestors = ancestors
        self.resume_checkpoint = resume_checkpoint
        self.workers = {}
        self.helpers = {}
        self.capacity_cooldowns = {}
        self.initial_build = None
        self.build_cancel = Event()
        self.orphan_checks = set()
        self.diagnostics_queue = asyncio.Queue()
        # All agents repair; the configured primary takes the final review turn.
        self.writers = roster.agents

    def state(self):
        from . import bump_tasks
        return bump_tasks.load(self.paths.forum)

    def capacity_cooling(self, author):
        return self.capacity_cooldowns.get(author, 0) > time.monotonic()

    def all_available_writers_cooling(self):
        available = [agent for agent in self.writers if agent.name not in self.workers and agent.name not in self.helpers
                     and not self.agent_locks[agent.name].locked()]
        return bool(available) and all(self.capacity_cooling(agent.name) for agent in available)

    def record_capacity(self, author, task_id):
        from . import bump_tasks
        self.capacity_cooldowns[author] = time.monotonic() + _CAPACITY_COOLDOWN_SECONDS
        with bump_tasks.transaction(self.paths.forum) as state:
            bump_tasks._event(state, "provider_capacity", "controller", task_id=task_id,
                              worker=author, reason="Selected model is at capacity",
                              cooldown_seconds=_CAPACITY_COOLDOWN_SECONDS)

    def inventory(self, state=None):
        from . import bump_tasks, bump_setup
        state = state or self.state()
        scope = state["config"].get("scope", {})
        selected = set(scope.get("selected_modules", {}).values())
        excluded = set(scope.get("excluded_files", {}))
        files = bump_setup.current_source_files(self.paths.project_root, scope.get("build_dir", ".lake/build"))
        inventory = bump_tasks.scan_sources(self.paths.project_root, files)
        for chunk in inventory["chunks"].values():
            if chunk["path"] in excluded and chunk["path"] not in selected:
                chunk.update(status="excluded", scope_excluded=True)
            elif chunk["path"] not in selected:
                chunk["scope_unclassified"] = True
        return inventory

    def discover_scope(self, root, config):
        from . import bump_setup
        return bump_setup.discover_scope(root, modules=config.get("modules", ()),
            project_scope=config.get("project_scope", "build"), lake_args=config.get("lake_args", ()))

    def refresh(self, result, *, authoritative_full_build=False):
        from . import bump_tasks, bump_verification
        state = self.state()
        main_sha = git(self.paths.project_root, "rev-parse", "HEAD")
        identity = result.get("source_identity") or {}
        returncode = result.get("returncode")
        expected_commands = bump_verification.scope_build_commands(
            state["config"].get("scope", {}), state["config"].get("lake_args", ()))
        # A single default receipt remains sufficient when there are no extras.
        # Candidate/module-only builds must never stand in for the whole scope.
        builds = result.get("builds", [result])
        full_scope = (
            isinstance(builds, list) and len(builds) == len(expected_commands)
            and ("builds" not in result or result.get("commands") == expected_commands)
            and all(isinstance(item, dict)
                    and item.get("command") == command
                    and type(item.get("returncode")) is int and item["returncode"] >= 0
                    and item.get("source_stable") is True
                    and item.get("source_identity") == identity
                    for item, command in zip(builds, expected_commands)))
        # Only a finished full-scope build can resolve obsolete task blockers.
        # `completed` means `passed` in build results: declaration failures must
        # not prevent reconciliation. Streams/review aggregates opt out.
        authoritative_current_build = (
            authoritative_full_build
            and type(returncode) is int and returncode >= 0
            and result.get("source_stable") is True
            and result.get("config_revision") == state["config"]["revision"]
            and identity.get("main_sha") == main_sha == state["main_sha"]
            and identity.get("scope") == state["config"].get("scope", {})
            and identity.get("lake_args") == list(state["config"].get("lake_args", ()))
            and full_scope
        )
        inventory = self.inventory(state)
        # HEAD alone cannot detect an uncommitted import/configuration change
        # since the build. This hashes files only; do not probe Lean/Lake here.
        # An empty record opts into guarded declaration completion but supplies
        # no evidence. Do not fall back to checked_files from a rejected build.
        declaration_build = {}
        if (authoritative_current_build and identity.get("files") is not None
                and identity["files"] == bump_verification._files(
                    self.paths.project_root, state["config"].get("scope", {}).get("build_dir", ".lake/build"))):
            declaration_build = result
        bump_tasks.refresh(self.paths.forum, inventory, result.get("diagnostics", []),
                           main_sha=main_sha,
                           config_revision=state["config"]["revision"],
                           checked_files=result.get("checked_files", ()), complete=False,
                           reconcile_environment=authoritative_current_build,
                           declaration_build=declaration_build)
        with bump_tasks.transaction(self.paths.forum) as state:
            # Large compiler output belongs in artifacts, not duplicated task state.
            state["last_build"] = {key: value for key, value in result.items()
                                   if key not in {"output", "stdout", "stderr", "source_identity"}}
            if isinstance(result.get("builds"), list):
                state["last_build"]["builds"] = [
                    {key: value for key, value in item.items()
                     if key not in {"output", "stdout", "stderr", "source_identity"}}
                    for item in result["builds"] if isinstance(item, dict)]
            # Keep only compact, source-bound evidence; it can suppress a
            # redundant failing recheck, never establish successful repair.
            state["orphan_failure_receipt"] = None
            if declaration_build and returncode > 0:
                errors = [row for row in result.get("diagnostics", [])
                          if row.get("severity", "error") == "error"]
                state["orphan_failure_receipt"] = {
                    "source_identity": deepcopy(identity), "config_revision": state["config"]["revision"],
                    "commands": deepcopy(expected_commands), "returncode": returncode,
                    "attempted_files": deepcopy(result.get("attempted_files")),
                    "errors": [{"path": row.get("path"), "id": row.get("id")} for row in errors],
                    "unmapped": deepcopy(result.get("unmapped")), "log_ref": deepcopy(result.get("log_ref")),
                }
            for task in state["tasks"].values():
                if task["kind"] == "configuration" and not task["files"]:
                    task["files"] = [name for name in CONFIG_FILES if (self.paths.project_root / name).exists()]
        return bool(declaration_build)

    async def build(self, *, modules=(), stream=False):
        from . import bump_verification
        state = self.state()
        loop = asyncio.get_running_loop()
        def on_output(channel, text):
            loop.call_soon_threadsafe(self.diagnostics_queue.put_nowait, (channel, text))
        def execute():
            with bump_jobs.cancellation_scope(self.build_cancel):
                build = bump_verification.build if modules else bump_verification.build_scope
                return build(self.paths.project_root, **({"modules": modules} if modules else {}),
                    lake_args=state["config"].get("lake_args", ()), scope=state["config"].get("scope"),
                    on_output=on_output if stream else None)
        result = await self.checked_job(execute)
        return {**result, "config_revision": state["config"]["revision"]}

    async def checked_job(self, function, *args, **kwargs):
        """Stop markers remain observable during captured builds/integration."""
        def execute():
            with bump_jobs.cancellation_scope(self.build_cancel):
                return function(*args, **kwargs)
        job = asyncio.create_task(asyncio.to_thread(execute))
        try:
            while not job.done():
                if stopped(self.paths):
                    self.build_cancel.set()
                await asyncio.wait({job}, timeout=0.1)
            return await job
        except asyncio.CancelledError:
            self.build_cancel.set()
            await asyncio.shield(job)
            raise

    async def worker(self, agent, task_id, tree, profile, interrupt):
        state = self.state()
        prompt_root = Path(__file__).with_name("prompts")
        system = (prompt_root / "bump" / (profile.upper() + ".md")).read_text()
        if self.paths.unity_md.is_file():
            system += "\n\nProject-specific instructions:\n" + self.paths.unity_md.read_text()
        tools_prompt = prompt_root / "BUMP_WORKFLOW_TOOLS.md"
        if tools_prompt.exists():
            system += "\n\n" + tools_prompt.read_text()
        system += ("\nYou are " + agent.name + ". Team: " + ", ".join(a.name for a in self.roster.agents)
                   + ". Primary: " + self.roster.primary.name + ".")
        instruction = (f"Your task is {task_id}. Read bump_task and bump_brief, communicate useful findings, "
                       "and work only on leased files. Submit a repair or yield with a concrete blocker. "
                       "Do not run Unity internals, edit controller state, or invoke another pipeline.")
        previous = state["tasks"].get(task_id, {}).get("previous_worktrees", [])
        if previous:
            instruction += (" Earlier attempts are retained at " + ", ".join(previous) +
                            ". Inspect and recover relevant partial work into your leased files if useful.")
        if profile == "critic":
            instruction = "As primary, review the current snapshot and full migration diff; submit_migration_verdict."
        async with self.agent_locks[agent.name]:
            if profile == "repairing":
                from . import bump_advisory_cache
                base = state.get("tasks", {}).get(task_id, {}).get("base_main_sha")
                if base:
                    await bump_advisory_cache.seed(self.paths, tree, base, state["config"], interrupt)
                    from .forum.bump_workflow_server import _assistance_current
                    assignment = state["tasks"][task_id]
                    binding = {"task_id": task_id, "author": agent.name,
                               "task_revision": assignment.get("revision"), "attempt": assignment.get("attempts"),
                               "base_main_sha": base, "config_revision": assignment.get("config_revision")}
                    current = self.state()
                    if (not _assistance_current(current, binding)
                            or current["tasks"][task_id].get("worktree") != str(Path(tree).resolve())):
                        return None
                if interrupt.is_set() or stopped(self.paths): return None
            env = runtime_env(self.paths, agent.name, task_id, profile, state["config"],
                              session_token=uuid.uuid4().hex)
            options = {"own_process_group": True, "interrupt_event": interrupt, "mcp_profile": "bump",
                       "env_overrides": env, "log_context": {"command": "bump", "run_id": state["run_id"],
                       "phase": profile, "task_id": task_id, "role": profile}}
            result = await spawn(agent, system, instruction, tree, mcp_servers(self.paths, profile), **options)
            if profile == "critic" or stopped(self.paths) or interrupt.is_set():
                return result
            assignment = state["tasks"].get(task_id)
            if not assignment:
                return result
            verified = self._submission_followup_needed(agent.name, task_id, tree, assignment)
            if not verified:
                return result
            # The prior spawn has completed its process cleanup. A single new
            # turn keeps the same leased tree and logical attempt, and asks the
            # worker—not the controller—to decide whether to submit or yield.
            from . import bump_tasks
            with bump_tasks.transaction(self.paths.forum) as current:
                task = current["tasks"].get(task_id, {})
                if (stopped(self.paths) or interrupt.is_set() or task.get("owner") != agent.name
                        or task.get("status") != "running" or task.get("yield_requested")
                        or task.get("revision") != verified.get("revision")
                        or task.get("base_main_sha") != assignment.get("base_main_sha")
                        or task.get("worktree") != str(Path(tree).resolve())
                        or task.get("config_revision") != assignment.get("config_revision")
                        or task.get("config_revision") != current["config"]["revision"]
                        or any(row.get("status") == "pending" for row in current.get("config_requests", {}).values())
                        or any(current["leases"].get(path, {}).get("task_id") != task_id
                               or current["leases"].get(path, {}).get("author") != agent.name
                               or current["leases"].get(path, {}).get("task_revision") != task.get("revision")
                               for path in task.get("files", []))
                        or any(c.get("task_id") == task_id and c.get("task_revision") == task.get("revision")
                               for c in current["candidates"].values())):
                    return result
                bump_tasks._event(current, "submission_followup", "controller", task_id=task_id,
                                  worker=agent.name, worktree=str(tree))
            if stopped(self.paths) or interrupt.is_set():
                return result
            instruction = (f"Your task {task_id} still has leased source edits and no submitted candidate. "
                           "Inspect the retained private worktree. Call submit_repair and report its returned "
                           "candidate_id, or call yield_task with a concrete blocker. Do not claim a submission "
                           "unless the tool returns a candidate_id.")
            followup_options = {**options, "env_overrides": {**env, "UNITY_BUMP_WORKER_SESSION": uuid.uuid4().hex}}
            return await spawn(agent, system, instruction, tree, mcp_servers(self.paths, profile), **followup_options)

    def _submission_followup_needed(self, author, task_id, tree, assignment):
        """Only a still-owned, unchanged assignment with leased source edits qualifies."""
        current = self.state()
        task = current["tasks"].get(task_id, {})
        if (task.get("owner") != author or task.get("status") != "running" or task.get("yield_requested")
                or type(task.get("revision")) is not int or type(assignment.get("revision")) is not int
                or task["revision"] < assignment["revision"]
                or task.get("base_main_sha") != assignment.get("base_main_sha")
                or task.get("config_revision") != assignment.get("config_revision")
                or task.get("config_revision") != current["config"]["revision"]
                or any(row.get("status") == "pending" for row in current.get("config_requests", {}).values())
                or task.get("worktree") != str(Path(tree).resolve())
                or any(current["leases"].get(path, {}).get("task_id") != task_id
                       or current["leases"].get(path, {}).get("author") != author
                       or current["leases"].get(path, {}).get("task_revision") != task["revision"]
                       for path in task.get("files", []))
                or any(c.get("task_id") == task_id and c.get("task_revision") == task.get("revision")
                       for c in current["candidates"].values())):
            return False
        sources = {p for p in task.get("files", []) if p.endswith(".lean") and Path(p).name not in CONFIG_FILES
                   and current["leases"].get(p, {}).get("task_id") == task_id
                   and current["leases"].get(p, {}).get("author") == author}
        if not sources or not task.get("base_main_sha"):
            return False
        changed = set()
        commands = [("diff", "--name-only", "--no-renames", "-z", task["base_main_sha"], "HEAD"),
                    ("diff", "--name-only", "--no-renames", "-z", "HEAD"),
                    ("diff", "--cached", "--name-only", "--no-renames", "-z"),
                    ("ls-files", "--others", "--exclude-standard", "-z")]
        for command in commands:
            result = git(tree, *command, check=False)
            if result.returncode:
                return False
            changed.update(filter(None, result.stdout.split("\0")))
        return task if sources & changed else False

    @staticmethod
    def yield_handoff_from(state, task):
        """Recover an unfulfilled explicit yield, including old checkpoints."""
        if task.get("yield_handoff_from"):
            return task["yield_handoff_from"]
        actor = None
        for event in reversed(state.get("events", [])):
            if event.get("task_id") != task["task_id"]:
                continue
            kind, author = event.get("kind"), event.get("author")
            if kind in {"candidate_submitted", "declaration_resolved", "orphan_revalidated"}:
                break
            if kind in {"claim", "release"}:
                if actor is not None and actor != author:
                    break
                actor = author
                if kind == "release" and event.get("previous_yield"):
                    return author
            elif kind == "yield" and actor == author:
                return author
        return None

    @staticmethod
    def continuation_author(state, task):
        """Prefer the last merged contributor only until another task attempt."""
        if task.get("kind") not in {"repair", "command"}:
            return None
        candidates = [row for row in state.get("candidates", {}).values()
                      if row.get("task_id") == task.get("task_id")]
        if not candidates:
            return None
        latest = max(candidates, key=lambda row: (str(row.get("submitted") or ""),
                                                   str(row.get("candidate_id") or "")))
        if latest.get("status") != "merged" or not latest.get("author"):
            return None
        for event in reversed(state.get("events", [])):
            if event.get("task_id") != task["task_id"]:
                continue
            kind = event.get("kind")
            if kind == "candidate_submitted":
                return latest["author"] if event.get("candidate_id") == latest.get("candidate_id") else None
            if kind in {"claim", "yield"}:
                return None
            if kind == "release" and (event.get("author") != latest["author"]
                                       or event.get("reason") != "Recovered integrated repair"
                                       or event.get("previous_yield")):
                return None
        return None  # No submission event to bind the saved candidate to its attempt.

    @staticmethod
    def handoff_preference(state, task, writers):
        """Choose the least recently assigned eligible writer for this task."""
        last_claim = {event.get("author"): index
                      for index, event in enumerate(state.get("events", []))
                      if event.get("kind") == "claim" and event.get("task_id") == task["task_id"]}
        return min(writers, key=lambda writer: last_claim.get(writer.name, -1)).name

    def writer_eligible(self, state, item, agent, *, free_helper=None):
        """Apply the same dispatch rules when considering helper preemption."""
        from . import bump_tasks
        helpers = set(self.helpers) - ({free_helper} if free_helper else set())
        if (stopped(self.paths) or any(r.get("status") == "pending" for r in state.get("config_requests", {}).values())
                or agent.name in self.workers or agent.name in helpers
                or (agent.name != free_helper and self.agent_locks[agent.name].locked())
                or self.capacity_cooling(agent.name)):
            return False
        configuration = item.get("kind") in {"configuration", "dependency", "scope_review"}
        configuration_pending = any(t["kind"] in {"configuration", "dependency", "scope_review"}
                                    and t["status"] not in {"complete", "grouped", "retired"}
                                    for t in state["tasks"].values())
        if ((configuration and not item.get("files"))
                or (configuration_pending and (not configuration or self.workers))
                or item.get("status") not in {"pending", "needs_repair", "ready"}
                or bump_tasks.orphaned_repair(item, state["chunks"])):
            return False
        if item.get("kind") == "command" and any(
                not (self.paths.project_root / path).is_file() for path in item.get("files", [])):
            return False
        yielded_by = self.yield_handoff_from(state, item)
        if not yielded_by:
            preferred = self.continuation_author(state, item)
            if (preferred and preferred != agent.name and any(w.name == preferred for w in self.writers)
                    and preferred not in self.workers and preferred not in helpers
                    and not self.capacity_cooling(preferred)
                    and (preferred == free_helper or not self.agent_locks[preferred].locked())):
                return False
        else:
            others = [other for other in self.writers if other.name != yielded_by]
            idle = [other for other in others if other.name not in self.workers and other.name not in helpers
                    and (other.name == free_helper or not self.agent_locks[other.name].locked())
                    and not self.capacity_cooling(other.name)]
            if idle and agent.name != self.handoff_preference(state, item, idle): return False
            if agent.name == yielded_by and not idle and any(other.name in self.workers for other in others):
                return False
        return all(state["tasks"].get(key, {}).get("status") in {"complete", "clean", "merged"}
                   for key in item.get("dependencies", []))

    def launch_ready(self):
        from . import bump_tasks
        if stopped(self.paths):
            return
        state = self.state()
        # A retained request must be reconciled before dispatch, including while
        # the initial build streams diagnostics after a restart. Otherwise a
        # writer can spend an attempt only to be stopped for that request.
        if any(row.get("status") == "pending" for row in state.get("config_requests", {}).values()):
            return
        for agent in self.writers:
            if (agent.name in self.workers or agent.name in self.helpers or self.agent_locks[agent.name].locked()
                    or self.capacity_cooling(agent.name)):
                continue
            ready = sorted(state["tasks"].values(), key=lambda row: (
                row.get("kind") not in {"configuration", "dependency", "imports"}, row["task_id"]))
            for item in ready:
                if not self.writer_eligible(state, item, agent):
                    continue
                try:
                    assignment = bump_tasks.claim(self.paths.forum, agent.name, item["task_id"],
                                                  max_attempts=self.max_attempts)
                except ValueError:
                    continue
                if assignment.get("status") in {"conflict", "blocked", "exhausted"}:
                    continue
                tree = task_worktree(self.paths, agent.name, item["task_id"])
                bump_tasks.configure_assignment(self.paths.forum, agent.name, item["task_id"],
                                                str(tree), state["main_sha"])
                profile = "preparing" if item.get("kind") in {"configuration", "dependency", "scope_review"} else "repairing"
                interrupt = asyncio.Event()
                task = asyncio.create_task(self.worker(agent, item["task_id"], tree, profile, interrupt))
                self.workers[agent.name] = {"task": task, "interrupt": interrupt, "task_id": item["task_id"],
                                           "tree": tree, "agent": agent}
                state = self.state()
                break

    async def assistance_worker(self, agent, request, tree, interrupt):
        """A helper has no source lease or candidate authority."""
        state = self.state()
        system = (Path(__file__).with_name("prompts") / "bump" / "ASSISTING.md").read_text()
        if self.paths.unity_md.is_file():
            system += "\n\nProject-specific instructions:\n" + self.paths.unity_md.read_text()
        system += ("\nYou are " + agent.name + ". Team: " + ", ".join(a.name for a in self.roster.agents)
                   + ". Primary: " + self.roster.primary.name + ".")
        instruction = (f"Help with request {request['event_id']} for task {request['task_id']}. "
                       f"Question and supplied context: {request['content']}\n"
                       "Read the task and its context, investigate in your private checkout, and call "
                       "answer_assistance with actionable findings and evidence. Do not claim a repair.")
        async with self.agent_locks[agent.name]:
            from . import bump_advisory_cache
            await bump_advisory_cache.seed(self.paths, tree, request["base_main_sha"], state["config"], interrupt)
            from .forum.bump_workflow_server import _assistance_current
            if interrupt.is_set() or stopped(self.paths) or not _assistance_current(self.state(), request): return None
            env = runtime_env(self.paths, agent.name, request["task_id"], "assisting", state["config"],
                              session_token=uuid.uuid4().hex)
            env["UNITY_BUMP_ASSISTANCE_REQUEST"] = request["event_id"]
            servers = mcp_servers(self.paths, "assisting")
            servers = {name: servers[name] for name in ("unity-forum", "lean-lsp")}
            return await spawn(agent, system, instruction, tree, servers,
                own_process_group=True, interrupt_event=interrupt, mcp_profile="bump", env_overrides=env,
                log_context={"command": "bump", "run_id": state["run_id"], "phase": "assisting",
                             "task_id": request["task_id"], "role": "assisting"})

    def launch_assistance(self):
        from . import bump_tasks
        from .forum.bump_workflow_server import _assistance_current
        if stopped(self.paths): return
        state = self.state()
        if any(t.get("kind") in {"configuration", "dependency", "scope_review"}
               and t.get("status") not in {"complete", "grouped", "retired"} for t in state["tasks"].values()):
            return
        requests = [e for e in state["events"] if e.get("kind") == "assistance_request"]
        started = {e.get("request_id") for e in state["events"] if e.get("kind") == "assistance_started"}
        for request in requests:
            if request["event_id"] in started or not _assistance_current(state, request): continue
            owner = next((a for a in self.roster.agents if a.name == request["author"]), None)
            available = [a for a in self.roster.agents if a.name != request["author"]
                         and a.name not in self.workers and a.name not in self.helpers
                         and not self.agent_locks[a.name].locked() and not self.capacity_cooling(a.name)]
            if not available: continue
            available.sort(key=lambda a: (getattr(a, "model", None) == getattr(owner, "model", None), a.name))
            agent = available[0]
            with bump_tasks.transaction(self.paths.forum) as current:
                if (not _assistance_current(current, request) or any(e.get("kind") == "assistance_started"
                        and e.get("request_id") == request["event_id"] for e in current["events"])):
                    continue
                bump_tasks._event(current, "assistance_started", "controller", task_id=request["task_id"],
                                  request_id=request["event_id"], helper=agent.name,
                                  deadline_at=time.time() + _ASSISTANCE_SECONDS)
            started.add(request["event_id"])
            try:
                base = request["base_main_sha"]
                if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40,64}", base):
                    raise ValueError("invalid assistance base commit")
                if git(self.paths.project_root, "rev-parse", "--verify", base + "^{commit}") != base:
                    raise ValueError("assistance base commit unavailable")
                safe = re.sub(r"[^A-Za-z0-9_-]", "_", agent.name)
                tree = bump_worktree.create_worktree(safe + "-help-" + uuid.uuid4().hex[:12],
                                                   self.paths.project_root, base=base)
                bump_worktree.symlink_lake_cache(tree, self.paths.project_root)
            except (ValueError, OSError) as exc:
                with bump_tasks.transaction(self.paths.forum) as current:
                    bump_tasks._event(current, "assistance_finished", "controller", task_id=request["task_id"],
                                      request_id=request["event_id"], helper=agent.name, reason=str(exc))
                continue
            interrupt = asyncio.Event()
            task = asyncio.create_task(self.assistance_worker(agent, request, tree, interrupt))
            self.helpers[agent.name] = {"task": task, "interrupt": interrupt, "request": request,
                                        "deadline": time.monotonic() + _ASSISTANCE_SECONDS}

    async def reconcile_helpers(self, *, drain=False, preempt=False):
        from . import bump_tasks
        from .forum.bump_workflow_server import _assistance_current
        state = self.state()
        # Check dispatch and claim rules as if this one helper had finished.
        preempt_author = None
        if preempt:
            for agent in self.writers:
                if agent.name not in self.helpers or self.helpers[agent.name]["task"].done(): continue
                for item in state["tasks"].values():
                    try:
                        for path in item.get("files", []): bump_tasks.relative_path(path)
                    except ValueError:
                        continue
                    if (self.writer_eligible(state, item, agent, free_helper=agent.name)
                            and item.get("status") == "pending" and item.get("owner") is None
                            and item.get("attempts", 0) < self.max_attempts
                            and all(state.get("chunk_attempts", {}).get(c, 0) < self.max_attempts
                                    for c in item.get("chunk_ids", []))
                            and not any(p in state["leases"] for p in item.get("files", []))
                            and all(state["tasks"].get(d, {}).get("status") == "complete"
                                    for d in item.get("dependencies", []))):
                        preempt_author = agent.name
                        break
                if preempt_author: break
        for author, record in list(self.helpers.items()):
            task, request = record["task"], record["request"]
            should_preempt = author == preempt_author
            if not task.done() and (drain or stopped(self.paths) or not _assistance_current(state, request)
                                    or time.monotonic() >= record["deadline"] or should_preempt):
                with bump_tasks.transaction(self.paths.forum) as current:
                    bump_tasks._event(current, "assistance_stopping", "controller", task_id=request["task_id"],
                                      request_id=request["event_id"], helper=author)
                await stop_worker(task, record["interrupt"])
            if not task.done(): continue
            error = task.exception() if not task.cancelled() else None
            if isinstance(error, SpawnCleanupError): raise error
            if isinstance(error, BumpProviderCapacityError): self.record_capacity(author, request["task_id"])
            with bump_tasks.transaction(self.paths.forum) as current:
                bump_tasks._event(current, "assistance_finished", "controller", task_id=request["task_id"],
                                  request_id=request["event_id"], helper=author,
                                  reason=type(error).__name__ if error else "finished or stopped")
            self.helpers.pop(author)

    async def recheck_orphaned_repairs(self):
        """Recheck deleted compiler ranges without spending a model attempt."""
        from . import bump_tasks, bump_verification
        state = self.state()
        scope = state["config"].get("scope", {})
        selected = scope.get("selected_modules", {})
        for tid, task in sorted(state["tasks"].items()):
            if task.get("status") not in {"pending", "ready", "needs_repair", "blocked"}:
                continue
            aliases = accepted_rename_paths(self.paths.project_root, state, task)
            renamed = bool(aliases)
            if ((not renamed and not bump_tasks.orphaned_repair(task, state["chunks"])) or task.get("owner")
                    or task.get("status") not in {"pending", "ready", "needs_repair", "blocked"}
                    or any(task.get(key) for key in ("audit_feedback", "critic_feedback", "review_snapshot_id", "grouped_review_feedback"))
                    or not task.get("diagnostics") or any(d.get("kind") not in {None, "declaration", "import"}
                            or d.get("severity", "error") != "error" or d.get("path") not in task["files"]
                            for d in task["diagnostics"])
                    or any(state["tasks"].get(dep, {}).get("status") not in {"complete", "clean", "merged"}
                           for dep in task.get("dependencies", []))
                    or any(lease.get("task_id") == tid or path in task["files"] for path, lease in state["leases"].items())
                    or any(c.get("task_id") == tid and c.get("status") in {"submitted", "merging"}
                           for c in state["candidates"].values())):
                continue
            files = {aliases.get(path, {}).get("path", path) for path in task["files"]}
            modules = sorted(name for name, path in selected.items() if path in files)
            if not files or {selected[name] for name in modules} != files:
                continue
            try:
                scanned = bump_tasks.scan_sources(self.paths.project_root, sorted(files))
            except (OSError, ValueError):
                continue
            if not renamed and any(cid in scanned["chunks"] for cid in task["chunk_ids"]):
                continue  # An incomplete inventory is not evidence of removal.
            source_hashes = {path: row["sha256"] for path, row in scanned["files"].items()}
            receipt = state.get("orphan_failure_receipt") or {}
            evidence = receipt.get("source_identity") or {}
            attempted, errors = receipt.get("attempted_files"), receipt.get("errors")
            if (type(receipt.get("returncode")) is int and receipt["returncode"] > 0
                    and receipt.get("config_revision") == state["config"]["revision"]
                    and receipt.get("commands") == bump_verification.scope_build_commands(
                        scope, state["config"].get("lake_args", ()))
                    and evidence.get("main_sha") == state["main_sha"]
                    and git(self.paths.project_root, "rev-parse", "HEAD") == state["main_sha"]
                    and evidence.get("scope") == scope
                    and evidence.get("lake_args") == list(state["config"].get("lake_args", ()))
                    and evidence.get("files") == bump_verification._files(
                        self.paths.project_root, scope.get("build_dir", ".lake/build"))
                    and all(evidence.get("files", {}).get(path) == digest for path, digest in source_hashes.items())
                    and isinstance(attempted, list) and files <= set(attempted)
                    and isinstance(errors, list) and errors and all(isinstance(row, dict) and row.get("path") for row in errors)
                    and files <= {row["path"] for row in errors}
                    and receipt.get("unmapped") == []):
                continue
            key = (tuple(sorted(files)), state["main_sha"], state["config"]["revision"], tuple(sorted(source_hashes.items())))
            if renamed:
                key = ("renamed_command", tid, bump_tasks.digest(aliases), *key)
            if key in self.orphan_checks:
                continue
            self.orphan_checks.add(key)
            result = await self.build(modules=modules)
            current = self.state()
            identity = result.get("source_identity") or {}
            command = ["lake", *state["config"].get("lake_args", ()), "--rehash", "build", *["+" + m for m in modules]]
            stable = (type(result.get("returncode")) is int and result["returncode"] >= 0
                      and result.get("source_stable") is True and result.get("config_revision") == state["config"]["revision"]
                      and result.get("command") == command and current["main_sha"] == state["main_sha"]
                      and current["config"]["revision"] == state["config"]["revision"]
                      and identity.get("main_sha") == state["main_sha"]
                      and identity.get("scope") == scope and identity.get("lake_args") == list(state["config"].get("lake_args", ()))
                      and identity.get("files") == bump_verification._files(self.paths.project_root, scope.get("build_dir", ".lake/build"))
                      and all(identity.get("files", {}).get(path) == digest for path, digest in source_hashes.items()))
            if not stable:
                return True
            errors = [row for row in result.get("diagnostics", []) if row.get("severity", "error") == "error"]
            if (not result.get("unmapped") and files <= set(result.get("checked_files", []))
                    and not any(row.get("path") in files or not row.get("path") for row in errors)):
                with bump_tasks.transaction(self.paths.forum) as fresh:
                    if fresh["main_sha"] == state["main_sha"] and fresh["config"]["revision"] == state["config"]["revision"]:
                        if renamed:
                            target = fresh["tasks"].get(tid)
                            provenance_unchanged = all(
                                fresh["candidates"].get(item["candidate_id"]) == state["candidates"].get(item["candidate_id"])
                                and fresh["tasks"].get(state["candidates"][item["candidate_id"]]["task_id"])
                                    == state["tasks"].get(state["candidates"][item["candidate_id"]]["task_id"])
                                for item in aliases.values())
                            if (target == task and provenance_unchanged
                                    and not any(lease.get("task_id") == tid or path in task["files"]
                                    for path, lease in fresh["leases"].items())
                                    and not any(c.get("task_id") == tid and c.get("status") in {"submitted", "merging"}
                                                for c in fresh["candidates"].values())):
                                target["status"] = "complete"
                                bump_tasks._event(fresh, "renamed_command_revalidated", "controller", task_id=tid,
                                    main_sha=state["main_sha"], config_revision=state["config"]["revision"],
                                    aliases=deepcopy(aliases), diagnostics=deepcopy(task["diagnostics"]),
                                    modules=modules, source_sha256=source_hashes, log_ref=deepcopy(result.get("log_ref")))
                        for target_id, target in fresh["tasks"].items():
                            if (set(target.get("files", [])) != files or target.get("owner")
                                    or target.get("status") not in {"pending", "ready", "needs_repair", "blocked"}
                                    or not bump_tasks.orphaned_repair(target, fresh["chunks"])
                                    or any(cid in scanned["chunks"] for cid in target["chunk_ids"])
                                    or any(target.get(k) for k in ("audit_feedback", "critic_feedback", "review_snapshot_id", "grouped_review_feedback"))
                                    or not target.get("diagnostics") or any(d.get("kind") not in {None, "declaration", "import"}
                                         or d.get("severity", "error") != "error" or d.get("path") not in files
                                         for d in target["diagnostics"])
                                    or any(lease.get("task_id") == target_id or path in files for path, lease in fresh["leases"].items())
                                    or any(c.get("task_id") == target_id and c.get("status") in {"submitted", "merging"}
                                           for c in fresh["candidates"].values())):
                                continue
                            resolved = [d.get("id") or "diagnostic-" + bump_tasks.digest(d)[:20] for d in target["diagnostics"]]
                            target.update(status="complete", diagnostics=[])
                            bump_tasks._event(fresh, "orphan_revalidated", "controller", task_id=target_id,
                                              main_sha=state["main_sha"], config_revision=state["config"]["revision"],
                                              modules=modules, source_sha256=source_hashes,
                                              resolved_diagnostic_ids=resolved, log_ref=deepcopy(result.get("log_ref")))
                # Another same-file orphan may still be owned; let it receive
                # a fresh check after its lease closes instead of stranding it.
                self.orphan_checks.discard(key)
            else:
                with bump_tasks.transaction(self.paths.forum) as fresh:
                    if fresh["main_sha"] == state["main_sha"] and fresh["config"]["revision"] == state["config"]["revision"]:
                        bump_tasks._event(fresh, "orphan_recheck_incomplete", "controller", task_id=tid,
                                          main_sha=state["main_sha"], config_revision=state["config"]["revision"],
                                          modules=modules, source_sha256=source_hashes,
                                          returncode=result["returncode"], log_ref=deepcopy(result.get("log_ref")))
                if result.get("diagnostics"):
                    # Keep unrelated whole-build diagnostics; add exact targeted
                    # errors so newly exposed declarations become ordinary tasks.
                    rows = list(current.get("diagnostics", []))
                    seen = {bump_tasks.digest(row) for row in rows}
                    rows.extend(row for row in result["diagnostics"] if bump_tasks.digest(row) not in seen)
                    bump_tasks.refresh(self.paths.forum, self.inventory(current), rows,
                        main_sha=current["main_sha"], config_revision=current["config"]["revision"], declaration_build={})
            return True
        return False

    async def reap_workers(self):
        from . import bump_tasks
        for author, record in list(self.workers.items()):
            task = record["task"]
            if not task.done():
                continue
            error = task.exception() if not task.cancelled() else None
            if isinstance(error, SpawnCleanupError):
                raise error
            if isinstance(error, BumpProviderCapacityError):
                self.record_capacity(author, record["task_id"])
            state = self.state()
            pending = any(row["task_id"] == record["task_id"] and row.get("status") in {"submitted", "merging"}
                          for row in state["candidates"].values())
            if not pending:
                bump_tasks.release(self.paths.forum, author, record["task_id"],
                    "Worker failed: " + type(error).__name__ if error else "Worker ended without a submitted repair", closed=True)
            self.workers.pop(author)

    @staticmethod
    def configuration_handoff_deferred(state, request):
        parent = state["tasks"].get(request.get("task_id"))
        if not parent or parent.get("kind") not in {"configuration", "dependency", "scope_review"}:
            return False
        return (bool(parent.get("owner")) or parent.get("status") in {"submitted", "merging"}
                or any(c.get("task_id") == parent["task_id"] and c.get("status") in {"submitted", "merging"}
                       for c in state["candidates"].values()))

    def reconcile_configuration_handoffs(self):
        """Fold queued configuration extensions into their original assignment.

        Also repairs scheduled child tasks from older checkpoints. Leases and
        submitted candidates must finish before an assignment can be changed.
        """
        from . import bump_tasks
        with bump_tasks.transaction(self.paths.forum) as state:
            for request in state.get("config_requests", {}).values():
                if request.get("status") not in {"pending", "scheduled"}:
                    continue
                if request.get("scheduled_task_id") == request.get("task_id"):
                    continue
                # Existing first-generation assignments are independent work;
                # only fold extensions made by a configuration assignment.
                if (request.get("status") == "scheduled"
                        and request.get("task_id") not in state["config_requests"]):
                    continue
                parent = state["tasks"].get(request.get("task_id"))
                child = state["tasks"].get(request["request_id"])
                if (not parent or parent.get("kind") not in {"configuration", "dependency", "scope_review"}
                        or parent.get("owner") or parent.get("status") in {"complete", "grouped", "retired", "submitted", "merging"}
                        or any(c.get("task_id") == parent["task_id"] and c.get("status") in {"submitted", "merging"}
                               for c in state["candidates"].values())
                        or (child and (child.get("owner") or child.get("status") not in {"pending", "ready", "needs_repair"}))):
                    continue
                old_options = parent.get("requested_config", {})
                incoming = request.get("requested_config", {})
                old_renames = old_options.get("module_renames", {})
                new_renames = incoming.get("module_renames", {})
                eligible = set(state["config"].get("modules", [])) & set(
                    state["config"].get("scope", {}).get("selected_modules", {}))
                retained = {old: new for old, new in old_renames.items() if old in eligible}
                invalid = set(old_renames) - eligible
                correction = (bool(invalid) and "module_renames" in incoming
                              and all(new_renames.get(old) == new for old, new in retained.items()))
                base_renames = retained if correction else old_renames
                combined = {**base_renames, **new_renames}
                conflict = (bool(set(new_renames) - eligible)
                            or (bool(invalid) and "module_renames" in incoming and not correction)
                            or any(old in old_renames and old_renames[old] != new
                                for old, new in new_renames.items())
                            or len(set(combined.values())) != len(combined)
                            or bool(set(combined) & set(combined.values())))
                if conflict:
                    request.update(status="rejected", rejection_reason="Conflicting or chained module renames")
                    if child:
                        child.update(status="grouped", grouped_into=parent["task_id"])
                        for other in state["tasks"].values():
                            if child["task_id"] in other.get("dependencies", []):
                                other["dependencies"] = sorted((set(other["dependencies"]) - {child["task_id"]}) |
                                                               ({parent["task_id"]} if other is not parent else set()))
                    bump_tasks._event(state, "configuration_handoff_rejected", "controller",
                                      request_id=request["request_id"], task_id=parent["task_id"],
                                      reason=request["rejection_reason"])
                    continue
                options = {**old_options, **incoming}
                if "modules" in old_options and "modules" in options:
                    options["modules"] = sorted(set(old_options["modules"]) | set(options["modules"]))
                if "module_renames" in old_options or "module_renames" in incoming:
                    options["module_renames"] = combined
                if old_options.get("project_scope") == "all":
                    options["project_scope"] = "all"
                files = sorted(set(parent["files"]) | set(request.get("files", [])) |
                               set(child.get("files", []) if child else []))
                # Only newly covered files, modules, or rename pairs earn a new
                # retry budget. Reversible option changes cannot replenish it.
                extension = (bool(set(files) - set(parent["files"]))
                             or bool(set(options.get("modules", [])) -
                                     (set(old_options.get("modules", [])) | set(state["config"].get("modules", []))))
                             or bool(set(combined.items()) - set(old_renames.items())))
                changed = files != parent["files"] or options != old_options
                parent.update(files=files, requested_config=options,
                              reason=request["reason"], proposed_changes=request["proposed_changes"])
                if changed:
                    parent["revision"] += 1
                if extension:
                    parent["attempts"] = 0
                if correction:
                    bump_tasks._event(state, "configuration_rename_corrected", "controller",
                                      task_id=parent["task_id"], request_id=request["request_id"],
                                      original_renames=deepcopy(old_renames), corrected_renames=deepcopy(combined),
                                      reason=request["reason"], proposed_changes=request["proposed_changes"])
                if child:
                    child.update(status="grouped", grouped_into=parent["task_id"])
                    for other in state["tasks"].values():
                        if child["task_id"] in other.get("dependencies", []):
                            other["dependencies"] = sorted((set(other["dependencies"]) - {child["task_id"]}) |
                                                           ({parent["task_id"]} if other is not parent else set()))
                request["status"] = "scheduled"
                request["scheduled_task_id"] = parent["task_id"]
                bump_tasks._event(state, "configuration_handoff", "controller", request_id=request["request_id"],
                                  task_id=parent["task_id"], changed=changed, extension=extension)

    async def handle_requests(self):
        """Configuration work gets an exclusive writer and a drained environment."""
        from . import bump_tasks
        await self.reconcile_helpers()
        state = self.state()
        requests = [r for r in state.get("config_requests", {}).values() if r["status"] == "pending"]
        for author, record in list(self.workers.items()):
            task = state["tasks"][record["task_id"]]
            if requests or task.get("yield_requested"):
                await stop_worker(record["task"], record["interrupt"])
        await self.reap_workers()
        self.reconcile_configuration_handoffs()
        state = self.state()
        requests = [r for r in state.get("config_requests", {}).values()
                    if r["status"] == "pending" and not self.configuration_handoff_deferred(state, r)]
        if requests:
            from . import bump_setup
            # A pure scope/Lake-option request needs no fabricated source patch.
            for request in requests:
                options = request.get("requested_config", {})
                if options and not request["files"]:
                    current = self.state()
                    config = {**current["config"], **options}
                    setup = await self.checked_job(bump_setup.prepare_environment, self.paths.project_root,
                        current["target_version"], config.get("pins", {}), modules=config.get("modules", ()),
                        project_scope=config.get("project_scope", "build"), lake_args=config.get("lake_args", ()))
                    await self.checked_job(self.commit_setup, setup)
                    with bump_tasks.transaction(self.paths.forum) as changed:
                        changed["config"].update(options)
                        changed["config"]["revision"] += 1
                        changed["config_requests"][request["request_id"]]["status"] = "applied"
                        changed["review"] = {}
                    await self.prepare()
            requests = [r for r in requests if not (r.get("requested_config") and not r["files"])]
            with bump_tasks.transaction(self.paths.forum) as state:
                for request in requests:
                    key = request["request_id"]
                    files = set(request["files"]) | {p for p in CONFIG_FILES if (self.paths.project_root / p).exists()}
                    state["tasks"][key] = {**bump_tasks._task(key, "configuration", [], sorted(files)),
                        "reason": request["reason"], "proposed_changes": request["proposed_changes"],
                        "requested_config": request.get("requested_config", {})}
                    state["config_requests"][key]["status"] = "scheduled"
        return bool(requests)

    def commit_setup(self, setup):
        """Checkpoint only controller-owned configuration/vendor edits."""
        from . import bump_tasks
        paths = [name for name in CONFIG_FILES if (self.paths.project_root / name).exists()]
        if (self.paths.project_root / "vendor").exists():
            paths.append("vendor")
        if paths:
            git(self.paths.project_root, "add", "--", *paths)
        staged = git(self.paths.project_root, "diff", "--cached", "--name-only")
        before = self.state()["main_sha"]
        if staged:
            write_json(self.paths.forum / "setup-integration.json", {
                "before": before, "tree": git(self.paths.project_root, "write-tree"), "setup": setup})
            git(self.paths.project_root, "-c", "user.name=Unity", "-c", "user.email=unity@localhost",
                "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-m",
                "Bump: resolve target toolchain and dependency configuration")
        head = git(self.paths.project_root, "rev-parse", "HEAD")
        with bump_tasks.transaction(self.paths.forum) as state:
            state["main_sha"] = head
            state["setup"] = setup
            state["dependencies"] = setup.get("dependencies", {})
            state["config"]["scope"] = setup.get("scope", {})
            if head != before:
                state["config"]["revision"] += 1
                state["events"].append({"kind": "configuration", "before": before, "after": head,
                                        "reason": "Target toolchain/dependency preparation"})
        write_json(self.paths.unity / "bump" / "setup.json", setup)
        write_json(self.paths.forum / "setup-integration.json", {"status": "complete", "after": head})

    def recover_setup(self):
        from . import bump_tasks
        path = self.paths.forum / "setup-integration.json"
        if not path.exists():
            return
        journal = json.loads(path.read_text())
        if journal.get("status") == "complete":
            return
        head = git(self.paths.project_root, "rev-parse", "HEAD")
        if head == journal["before"]:
            return
        if (git(self.paths.project_root, "rev-parse", "HEAD^") != journal["before"]
                or git(self.paths.project_root, "rev-parse", "HEAD^{tree}") != journal["tree"]):
            raise ValueError("Current main does not match the interrupted configuration checkpoint")
        setup = journal["setup"]
        with bump_tasks.transaction(self.paths.forum) as state:
            if state["main_sha"] not in {head, journal["before"]}:
                raise ValueError("Configuration journal conflicts with Bump state")
            if state["main_sha"] != head:
                state["config"]["revision"] += 1
            state["main_sha"], state["setup"] = head, setup
            state["dependencies"] = setup.get("dependencies", {})
            state["config"]["scope"] = setup.get("scope", {})
        write_json(self.paths.unity / "bump" / "setup.json", setup)
        write_json(path, {"status": "complete", "after": head})

    async def prepare(self):
        """Dependencies finish first; recoverable setup failures become Forum work."""
        from . import bump_tasks, bump_setup, bump_recursive
        state = self.state()
        setup = state.get("setup") or bump_setup.read_setup(self.paths)
        if any(not task.get("retryable", True) for task in setup.get("pending_tasks", [])):
            # Explicit continuation after the user installs a missing compiler
            # must be able to retry its deterministic setup check.
            config = state["config"]
            setup = await self.checked_job(bump_setup.prepare_environment, self.paths.project_root,
                state["target_version"], config.get("pins", {}), modules=config.get("modules", ()),
                project_scope=config.get("project_scope", "build"), lake_args=config.get("lake_args", ()))
        async def migrate_child(child_paths, child_setup, *, ancestors):
            # Same pipeline, roster, Forum semantics and reviewer requirement.
            # Parent writers have not launched, so no nested writer duplicates.
            async def propagate_stop():
                while True:
                    if stopped(self.paths):
                        marker = child_paths.unity / "stop-requested"
                        if not marker.exists():
                            marker.touch(exist_ok=False)
                        return
                    await asyncio.sleep(0.1)
            watch = asyncio.create_task(propagate_stop())
            try:
                from .bump_processes import assert_closed
                assert_closed(child_paths.project_root)
                if not stopped(self.paths):
                    (child_paths.unity / "stop-requested").unlink(missing_ok=True)
                accepted = await run(child_paths, self.roster, max_attempts=self.max_attempts,
                    setup=child_setup, agent_locks=self.agent_locks,
                    dependency_runs=self.dependency_runs, ancestors=ancestors)
            finally:
                watch.cancel()
                await asyncio.gather(watch, return_exceptions=True)
            child = bump_tasks.load(child_paths.forum)
            if not accepted and stopped(self.paths):
                raise bump_jobs.JobCancelled("Parent migration stopped during dependency work")
            return {"build_passed": bool(accepted), "review_approved": bool(accepted),
                    "verified_commit": child["main_sha"],
                    "snapshot_id": child.get("review", {}).get("snapshot", {}).get("snapshot_id")}
        changed = await bump_recursive.run_children(self.paths, setup, migrate_child=migrate_child,
                        registry=self.dependency_runs, ancestors=self.ancestors, run_job=self.checked_job)
        if changed:
            config = state["config"]
            setup = await self.checked_job(bump_setup.prepare_environment, self.paths.project_root,
                state["target_version"], config.get("pins", {}), modules=config.get("modules", ()),
                project_scope=config.get("project_scope", "build"), lake_args=config.get("lake_args", ()))
        await self.checked_job(self.commit_setup, setup)
        if setup.get("phase") == "ready":
            return
        if any(not task.get("retryable", True) for task in setup.get("pending_tasks", [])):
            raise ValueError("Target toolchain is unavailable; setup receipt records the required user action")
        with bump_tasks.transaction(self.paths.forum) as state:
            for row in setup.get("pending_tasks", []):
                key = row["task_id"]
                if key not in state["tasks"]:
                    state["tasks"][key] = {**bump_tasks._task(key, "configuration", [],
                        [name for name in CONFIG_FILES if (self.paths.project_root / name).exists()]),
                        "reason": row.get("reason"), "step": row.get("step")}
        phase(self.paths, "preparing")
        while not stopped(self.paths):
            await self.reap_workers()
            await self.handle_requests()
            if await self.integrate_ready(reconcile=False):
                setup = self.state().get("setup", {})
                if setup.get("phase") == "ready":
                    with bump_tasks.transaction(self.paths.forum) as current:
                        for task in current["tasks"].values():
                            if task.get("step") and not task.get("owner"):
                                task["status"] = "complete"
                    await self.prepare()
                    return
            self.launch_ready()
            if not self.workers:
                if self.all_available_writers_cooling():
                    await asyncio.sleep(0.1)
                    continue
                raise ValueError("Bump setup repairs are blocked or exhausted; inspect the Forum")
            await asyncio.sleep(0.1)
        raise bump_jobs.JobCancelled("Bump stopped during preparation")

    def recover_integration(self):
        """Reconcile only a journaled fast-forward, never guess at external edits."""
        from . import bump_tasks
        journal_path = self.paths.forum / "integration.json"
        if not journal_path.exists():
            return
        journal = json.loads(journal_path.read_text())
        if journal.get("status") == "complete":
            return
        head = git(self.paths.project_root, "rev-parse", "HEAD")
        if head == journal["before"]:
            return  # Nothing was published; retained candidate is retried normally.
        if head != journal["after"]:
            raise ValueError("Bump main is neither side of the saved integration journal")
        with bump_tasks.transaction(self.paths.forum) as state:
            row = state["candidates"][journal["candidate_id"]]
            task = state["tasks"][row["task_id"]]
            if state["main_sha"] not in {journal["before"], head}:
                raise ValueError("Bump checkpoint conflicts with its integration journal")
            if journal.get("config"):
                if state["main_sha"] != head:
                    state["config"]["revision"] += 1
                state["config"]["scope"] = journal["config"]["scope"]
                for key in ("modules", "lake_args", "project_scope"):
                    state["config"][key] = journal["config"].get(key, state["config"].get(key))
                state["setup"] = journal["config"]
                state["dependencies"] = journal["config"].get("dependencies", state.get("dependencies", {}))
            # A mixed assignment can make genuine progress on live chunks,
            # but unavailable inherited chunks remain an independent B15
            # obligation and must not be marked complete with the candidate.
            missing = sorted(set(task.get("chunk_ids", [])) - state["chunks"].keys())
            if missing and task.get("kind") in {"repair", "command"}:
                orphan_id = "orphan-" + bump_tasks.digest([task["task_id"], row["candidate_id"], missing])[:16]
                if orphan_id in state["tasks"]:
                    raise ValueError("orphan obligation already exists")
                orphan = {**bump_tasks._task(orphan_id, "repair", missing, list(task["files"])),
                          "attempts": task["attempts"], "diagnostics": deepcopy(task.get("diagnostics", [])),
                          "dependencies": [dep for dep in task.get("dependencies", []) if dep != task["task_id"]],
                          "orphaned_from": task["task_id"], "source_candidate_id": row["candidate_id"],
                          "reason": "Unavailable chunks retained after partial candidate integration"}
                for key in ("audit_feedback", "critic_feedback", "review_snapshot_id", "grouped_review_feedback"):
                    if task.get(key): orphan[key] = deepcopy(task[key])
                state["tasks"][orphan_id] = orphan
                task["chunk_ids"] = sorted(set(task["chunk_ids"]) - set(missing))
                bump_tasks._event(state, "integration_orphaned_chunks", "controller", task_id=task["task_id"],
                                  orphan_task_id=orphan_id, missing_chunk_ids=missing, candidate_id=row["candidate_id"])
            state["main_sha"] = head
            row.update(status="merged", merged_sha=head)
            task["status"] = "complete"
            state["review"] = {}
        if task.get("owner"):
            bump_tasks.release(self.paths.forum, row["author"], row["task_id"], "Recovered integrated repair", closed=True)
        if journal.get("config"):
            write_json(self.paths.unity / "bump" / "setup.json", journal["config"])
        write_json(journal_path, {"status": "complete", "after": head})

    async def drain(self):
        self.build_cancel.set()
        errors = []
        try:
            await self.reconcile_helpers(drain=True)
        except SpawnCleanupError as exc:
            errors.append(exc)
        for author, record in list(self.workers.items()):
            try:
                await stop_worker(record["task"], record["interrupt"])
            except SpawnCleanupError as exc:
                errors.append(exc)
        if self.initial_build is not None:
            try:
                await self.initial_build
            except bump_jobs.JobCancelled:
                pass
        if errors:
            raise errors[0]
        await self.reap_workers()

    def integrate(self, candidate):
        """One private integration checkout; publish only checked commits to main."""
        from . import bump_tasks, bump_verification, bump_setup
        paths = self.paths
        with merge_lock(paths):
            state = self.state()
            task = state["tasks"][candidate["task_id"]]
            before = git(paths.project_root, "rev-parse", "HEAD")
            if before != state["main_sha"] or git(paths.project_root, "status", "--porcelain", "--untracked-files=no"):
                raise ValueError("Bump main changed outside integration")
            if candidate["config_revision"] != state["config"]["revision"]:
                return {"accepted": False, "reason": "Configuration changed; recheck and resubmit"}
            if candidate["task_revision"] != task["revision"]:
                return {"accepted": False, "reason": "Assignment changed; recheck and resubmit"}
            diff = git(paths.project_root, "diff", "--binary", "--full-index", "--no-ext-diff",
                       candidate["base_main_sha"], candidate["commit_sha"], check=False)
            if diff.returncode or not diff.stdout:
                return {"accepted": False, "reason": "Candidate has no reproducible patch"}
            changed = git(paths.project_root, "diff", "--name-only", "--no-renames", "-z",
                          candidate["base_main_sha"], candidate["commit_sha"], check=False).stdout.split("\0")
            changed = sorted(path for path in changed if path)
            if set(changed) - set(task["files"]):
                return {"accepted": False, "reason": "Candidate changes unleased files"}
            if any(state["leases"].get(path, {}).get("task_id") != task["task_id"] for path in changed):
                return {"accepted": False, "reason": "Candidate no longer owns its file leases"}
            if candidate.get("diff_sha256") and hashlib.sha256(diff.stdout.encode()).hexdigest() != candidate["diff_sha256"]:
                return {"accepted": False, "reason": "Candidate patch identity differs"}
            tree = task_worktree(paths, "integration", task["task_id"])
            # Config candidates must not mutate the parent's shared dependency cache.
            configuration = task.get("kind") in {"configuration", "dependency", "scope_review"}
            if configuration:
                package_link = tree / ".lake" / "packages"
                if package_link.is_symlink():
                    package_link.unlink()
                runtime_link = tree / ".unity"
                if not runtime_link.is_symlink() or runtime_link.resolve() != paths.unity.resolve():
                    raise ValueError("unexpected integration runtime path")
                runtime_link.unlink()
                runtime_link.mkdir()
            applied = git(tree, "apply", "--3way", "--index", "-", input=diff.stdout, check=False)
            if applied.returncode:
                return {"accepted": False, "reason": "Merge conflict; sync your worktree and resubmit"}
            config = {**state["config"], **task.get("requested_config", {})}
            renames = config.pop("module_renames", {})
            if renames:
                selected = state["config"].get("scope", {}).get("selected_modules", {})
                if not configuration or not set(renames) <= set(state["config"].get("modules", [])):
                    return {"accepted": False, "reason": "Module renames must replace existing supplemental coverage"}
                if any(old not in selected or (tree / selected[old]).exists()
                       or selected[old] not in changed for old in renames):
                    return {"accepted": False, "reason": "Module rename must remove the leased original source"}
                config["modules"] = sorted((set(config["modules"]) - set(renames)) | set(renames.values()))
            if configuration:
                setup = bump_setup.prepare_environment(tree, state["target_version"], config.get("pins", {}),
                    modules=config.get("modules", ()), project_scope=config.get("project_scope", "build"),
                    lake_args=config.get("lake_args", ()))
                if setup.get("phase") != "ready":
                    return {"accepted": False, "reason": "Configuration still has setup blockers", "setup": setup}
                if renames:
                    names = git(paths.project_root, "diff", "--name-status", "--find-renames=50%", "-z",
                                candidate["base_main_sha"], candidate["commit_sha"]).split("\0")
                    moved = {}
                    while names and names[0]:
                        status, *names = names
                        if status.startswith(("R", "C")):
                            old_path, new_path, *names = names
                            if status.startswith("R"):
                                moved[old_path] = new_path
                        else:
                            _, *names = names
                    for old, new in renames.items():
                        path = setup.get("scope", {}).get("selected_modules", {}).get(new)
                        if (not path or path not in changed or path not in task["files"]
                                or not (tree / path).is_file() or moved.get(selected[old]) != path):
                            return {"accepted": False, "reason": "Module rename must move its leased source to the replacement module"}
            else:
                try:
                    scope = self.discover_scope(tree, config)
                except ValueError as exc:
                    return {"accepted": False, "reason": "Current module/import scope needs repair: " + str(exc)}
                setup = ({**state.get("setup", {}), "scope": scope,
                          **{key: config.get(key, default) for key, default in
                             (("modules", []), ("lake_args", []), ("project_scope", "build"))}}
                         if scope != config.get("scope", {}) else None)
            audit = bump_verification.audit_sources(tree, state["base_sha"],
                                                   setup.get("scope", {}) if setup else config.get("scope", {}))
            if audit.get("issues"):
                previous_audit = bump_verification.audit_sources(paths.project_root, state["base_sha"],
                                                                 state["config"].get("scope", {}))
                previous_issues = {bump_tasks.digest(row) for row in previous_audit.get("issues", [])}
                introduced = [row for row in audit["issues"] if bump_tasks.digest(row) not in previous_issues]
                if introduced:
                    return {"accepted": False, "reason": "Source-hole/scope audit rejected patch",
                            "audit": audit, "introduced_issues": introduced}
            scope = setup.get("scope", {}) if setup else config.get("scope", {})
            modules = sorted(module for module, path in scope.get("selected_modules", {}).items()
                             if path in task["files"])
            build = bump_verification.build(tree, modules=modules, lake_args=config.get("lake_args", ()),
                                            scope=setup.get("scope") if setup else config.get("scope"),
                                            project_root=paths.project_root)
            # A validated environment can expose ordinary Lean repair work.
            # Requiring all source to compile here deadlocks configuration fixes.
            if configuration:
                result = {"accepted": bool(build.get("source_stable", True) and not build.get("unmapped")),
                          "reason": "configuration resolved; current compiler diagnostics retained"}
            else:
                candidate_chunks = None
                if task.get("chunk_ids") or task.get("kind") in {"command", "module_repair", "imports"}:
                    selected = set(scope.get("selected_modules", {}).values())
                    sources = set(bump_setup.current_source_files(tree, scope.get("build_dir", ".lake/build")))
                    try:
                        scanned = bump_tasks.scan_sources(tree, sorted(set(task["files"]) & sources & selected))
                        identity = build.get("source_identity") or {}
                        built_files = identity.get("files", {}) if isinstance(identity, dict) else {}
                        if not isinstance(built_files, dict):
                            built_files = {}
                        # The later lexical scan must describe the same bytes
                        # that the compiler actually checked.
                        candidate_chunks = {cid: chunk for cid, chunk in scanned["chunks"].items()
                                            if scanned["files"][chunk["path"]]["sha256"] == built_files.get(chunk["path"])}
                    except (ValueError, OSError):
                        candidate_chunks = {}  # Invalid source cannot prove declaration reach.
                result = bump_verification.candidate_progress(state.get("last_build", {}), build, task,
                                                               self.inventory(state), candidate_chunks=candidate_chunks)
            if not result["accepted"]:
                return {**result, "build": build}
            # The patch is already staged, including removals. Re-stage only
            # surviving paths after validation; deleted paths no longer exist in
            # the index and git add rejects them as unknown pathspecs.
            surviving = [path for path in changed if (tree / path).exists()]
            if surviving:
                git(tree, "add", "--", *surviving)
            if configuration:
                git(tree, "add", "--", "lean-toolchain", "lake-manifest.json",
                    *[name for name in ("lakefile.lean", "lakefile.toml", "vendor") if (tree / name).exists()])
            git(tree, "-c", "user.name=Unity", "-c", "user.email=unity@localhost", "commit", "-m",
                "UNITY: integrate Bump repair " + task["task_id"])
            accepted = git(tree, "rev-parse", "HEAD")
            if git(paths.project_root, "rev-parse", "HEAD") != before:
                raise ValueError("Bump main changed during verification")
            # Journal closes the Git/state crash window without a semantic ledger.
            write_json(paths.forum / "integration.json", {"candidate_id": candidate["candidate_id"],
                "before": before, "after": accepted, "config": setup})
            git(paths.project_root, "merge", "--ff-only", accepted)
            with bump_jobs.cancellation_disabled():
                self.recover_integration()
            return {**result, "build": build, "main_sha": accepted}

    async def integrate_ready(self, *, reconcile=True):
        from . import bump_tasks
        for candidate in self.state()["candidates"].values():
            if candidate.get("status") != "submitted":
                continue
            await self.reconcile_helpers(drain=True)
            owners = [(name, record) for name, record in self.workers.items()
                      if record["task_id"] == candidate["task_id"]]
            configuration = self.state()["tasks"][candidate["task_id"]]["kind"] in {"configuration", "dependency", "scope_review"}
            if configuration:
                owners = list(self.workers.items())
            for name, record in owners:
                await stop_worker(record["task"], record["interrupt"])
            await self.reap_workers()
            result = await self.checked_job(self.integrate, candidate)
            if not result["accepted"]:
                with bump_tasks.transaction(self.paths.forum) as state:
                    state["candidates"][candidate["candidate_id"]].update(status="rejected", feedback=result)
                    state["tasks"][candidate["task_id"]]["status"] = "pending"
                bump_tasks.release(self.paths.forum, candidate["author"], candidate["task_id"],
                                   result.get("reason", "Candidate rejected"), closed=True)
            # Reconcile all current modules; partial candidate builds never erase unrelated errors.
            if reconcile:
                if result["accepted"] and configuration:
                    await self.prepare()
                self.refresh(await self.build(), authoritative_full_build=True)
            return True
        return False

    async def review(self):
        from . import bump_tasks, bump_verification
        state = self.state()
        try:
            scope = await self.checked_job(self.discover_scope, self.paths.project_root, state["config"])
        except ValueError as exc:
            self.queue_audit_repairs([{"kind": "scope_discovery", "reason": str(exc),
                                      "paths": [p for p in CONFIG_FILES if (self.paths.project_root / p).exists()]}])
            return False
        if scope != state["config"]["scope"]:
            setup = {**state.get("setup", {}), "scope": scope}
            with bump_tasks.transaction(self.paths.forum) as current:
                current["config"]["scope"] = scope
                current["config"]["revision"] += 1
                current["setup"] = setup
                current["review"] = {}
            write_json(self.paths.unity / "bump" / "setup.json", setup)
            self.refresh(await self.build(), authoritative_full_build=True)
            return False
        snapshot = await self.checked_job(bump_verification.verify_final, self.paths.project_root,
            base_sha=state["base_sha"], scope=state["config"]["scope"],
            lake_args=state["config"].get("lake_args", ()), artifacts_dir=self.paths.artifacts,
            config_revision=state["config"]["revision"])
        with bump_tasks.transaction(self.paths.forum) as current:
            current["review"] = {"snapshot": snapshot, "reviewer": self.roster.primary.name, "verdicts": []}
        if not snapshot.get("passed"):
            repairable = self.queue_audit_repairs(snapshot.get("issues", []))
            if any(row.get("severity") == "error" for row in snapshot.get("diagnostics", [])):
                self.refresh(snapshot)
                return False
            if repairable:
                return False
            raise ValueError("Final Bump verification did not pass; inspect stored build/audit evidence")
        agent = self.roster.primary
        phase(self.paths, "critic")
        attempt = 0
        while attempt < self.max_attempts:
            if stopped(self.paths):
                return False
            if self.capacity_cooling(agent.name):
                await asyncio.sleep(0.1)
                continue
            attempt += 1
            tree = task_worktree(self.paths, agent.name + "-critic", "critic")
            interrupt = asyncio.Event()
            worker = asyncio.create_task(self.worker(agent, "critic", tree, "critic", interrupt))
            capacity = False
            try:
                while not worker.done():
                    if stopped(self.paths):
                        return False
                    await asyncio.sleep(0.1)
                await worker
            except BumpProviderCapacityError:
                self.record_capacity(agent.name, "critic")
                capacity = True
            finally:
                await stop_worker(worker, interrupt)
            if capacity:
                continue
            current = self.state()
            verdict = current.get("review", {}).get("verdict")
            if not verdict:
                continue
            if verdict.get("approved"):
                identity = await asyncio.to_thread(bump_verification.source_identity, self.paths.project_root,
                    lake_args=current["config"].get("lake_args", ()), scope=current["config"]["scope"])
                if identity != snapshot["source_identity"]:
                    raise ValueError("Sources/configuration changed during critic review")
                prospective = {**current, "phase": "complete"}
                receipt = bump_verification.persist_report(self.paths, prospective, snapshot, verdict, accepted=True)
                with bump_tasks.transaction(self.paths.forum) as accepted:
                    accepted["phase"] = "complete"
                    accepted["final_report"] = receipt["artifacts"]
                phase(self.paths, "complete")
                return True
            if verdict.get("reopen_task_ids"):
                with bump_tasks.transaction(self.paths.forum) as changed:
                    for key in verdict["reopen_task_ids"]:
                        changed["tasks"][key]["status"] = "pending"
                        changed["tasks"][key]["critic_feedback"] = verdict
                phase(self.paths, "repairing")
                return False
            raise ValueError("Primary migration review requested changes; inspect verdict")
        raise ValueError("Critic attempts ended without acceptance")

    def queue_audit_repairs(self, issues):
        """Source/scope findings are repair work; they are not transport faults."""
        from . import bump_tasks
        queued = False
        with bump_tasks.transaction(self.paths.forum) as state:
            for issue in issues:
                files = issue.get("paths", []) or ([issue["path"]] if issue.get("path") else [])
                if not files:
                    continue
                files = sorted({bump_tasks.relative_path(path) for path in files})
                key = "audit-" + bump_tasks.digest([issue["kind"], files])[:20]
                task = state["tasks"].get(key)
                if task is None:
                    configuration = any(path in CONFIG_FILES or path.startswith("vendor/") for path in files)
                    kind = "configuration" if configuration else "scope_review" if issue["kind"] in {
                        "scope_discovery", "unclassified_source_module", "unexplained_exclusion",
                        "unbuilt_selected_files"} else "module_repair"
                    task = bump_tasks._task(key, kind, [], files)
                    state["tasks"][key] = task
                task.update(status="pending", reason="Final source/scope audit requested repair", audit_feedback=issue)
                queued = True
            if queued:
                state["phase"] = "repairing"
        return queued

    async def run(self):
        from . import bump_tasks, bump_verification
        self.recover_setup()
        self.recover_integration()
        if git(self.paths.project_root, "rev-parse", "HEAD") != self.state()["main_sha"]:
            raise ValueError("Bump main differs from its checkpoint; no automatic reset is allowed")
        try:
            await self.prepare()
            phase(self.paths, "repairing")
            self.initial_build = asyncio.create_task(self.build(stream=True))
            streamed = ""
            last_parsed = None
            while not self.initial_build.done():
                if stopped(self.paths):
                    return False
                while not self.diagnostics_queue.empty():
                    _, text = self.diagnostics_queue.get_nowait()
                    streamed += text
                # Do not publish an unfinished diagnostic line.
                complete_text = streamed.rpartition("\n")[0]
                # Pathless streaming errors can precede the source diagnostic
                # that explains them. Only the completed build may schedule an
                # environment/configuration task from those provisional lines.
                parsed = [row for row in bump_verification._parse_diagnostics(complete_text, self.paths.project_root)
                          if row.get("path")]
                if parsed and parsed != last_parsed:
                    bump_tasks.refresh(self.paths.forum, self.inventory(), parsed,
                        main_sha=self.state()["main_sha"], config_revision=self.state()["config"]["revision"], complete=False)
                    last_parsed = parsed
                    if not self.resume_checkpoint:
                        self.launch_ready()
                await self.reap_workers()
                await asyncio.sleep(0.1)
            authoritative = self.refresh(await self.initial_build, authoritative_full_build=True)
            self.initial_build = None
            if self.resume_checkpoint and not authoritative:
                raise ValueError("Resumed Bump needs a current authoritative initial build before repair dispatch")
            while not stopped(self.paths):
                await self.reap_workers()
                await self.handle_requests()
                if await self.integrate_ready():
                    continue
                await self.recheck_orphaned_repairs()
                self.launch_ready()
                await self.reconcile_helpers(preempt=True)
                self.launch_ready()
                self.launch_assistance()
                state = self.state()
                pending = [task for task in state["tasks"].values()
                           if task.get("status") not in {"complete", "clean", "merged", "retired", "grouped"}]
                if not self.workers:
                    if pending:
                        if self.all_available_writers_cooling():
                            await asyncio.sleep(0.1)
                            continue
                        raise ValueError("Bump has blocked or exhausted repairs; inspect Forum task evidence")
                    await self.reconcile_helpers(drain=True)
                    if await self.review():
                        return True
                await asyncio.sleep(0.1)
            return False
        except bump_jobs.JobCancelled:
            if not stopped(self.paths):
                raise
            return False
        finally:
            await self.drain()


async def run(paths, roster, *, max_attempts=5, setup=None, **controller_options):
    from . import bump_tasks, bump_setup, bump_verification
    from .bump_processes import assert_closed
    setup = setup or bump_setup.read_setup(paths)
    migration = json.loads((paths.unity / "bump" / "migration.json").read_text())
    existing = (paths.forum / bump_tasks.STATE_FILE).exists()
    if not existing:
        bump_tasks.initialize(paths.forum, paths.project_root, run_id=migration["run_id"],
            base_sha=migration["original_commit"], version=migration["target_version"],
            config={"revision": 0, "scope": setup.get("scope", {}), "pins": migration.get("dependency_pins", {}),
                    "modules": migration.get("modules", []), "project_scope": migration.get("project_scope", "build"),
                    "lake_args": migration.get("lake_args", [])})
        with bump_tasks.transaction(paths.forum) as state:
            state["main_sha"] = git(paths.project_root, "rev-parse", "HEAD")
            state["setup"] = setup
    else:
        assert_closed(paths.project_root)
        # The CLI/child controller has established closure without signaling any
        # saved PID. Only now may stopped attempts yield their writer leases.
        state = bump_tasks.load(paths.forum)
        for task in state["tasks"].values():
            if task.get("owner") and task["status"] == "running":
                bump_tasks.release(paths.forum, task["owner"], task["task_id"],
                                   "Reopened interrupted attempt after verified process closure", closed=True)
        if state["phase"] == "complete":
            snapshot = state.get("review", {}).get("snapshot", {})
            identity = bump_verification.source_identity(paths.project_root,
                lake_args=state["config"].get("lake_args", ()), scope=state["config"].get("scope"))
            if snapshot.get("source_identity") != identity:
                raise ValueError("Completed migration changed since its accepted report")
            bump_verification.completion_report(state)
            return True
    controller = Controller(paths, roster, max_attempts=max_attempts, resume_checkpoint=existing, **controller_options)
    try:
        return await controller.run()
    finally:
        state = bump_tasks.load(paths.forum)
        if state["phase"] != "complete":
            phase(paths, "stopped")
            snapshot = state.get("review", {}).get("snapshot", {})
            bump_verification.persist_report(paths, bump_tasks.load(paths.forum), snapshot, None)
