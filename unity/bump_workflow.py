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
from threading import Event
import uuid
from copy import deepcopy

CONFIG_FILES = ("lean-toolchain", "lakefile.lean", "lakefile.toml", "lake-manifest.json")

from . import artifacts, bump_jobs, bump_worktree
from .bump_spawn import SpawnCleanupError, spawn
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


def runtime_env(paths, author, task_id, profile, config):
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
                 dependency_runs=None, ancestors=()):
        self.paths, self.roster, self.max_attempts = paths, roster, max_attempts
        self.agent_locks = agent_locks if agent_locks is not None else {
            agent.name: asyncio.Lock() for agent in roster.agents}
        self.dependency_runs = dependency_runs if dependency_runs is not None else {}
        self.ancestors = ancestors
        self.workers = {}
        self.initial_build = None
        self.build_cancel = Event()
        self.diagnostics_queue = asyncio.Queue()
        # Reserve an actual independent reviewer, not a contributor wearing a
        # different role label. A one-agent roster cannot meet this contract.
        if len(roster.agents) < 2:
            raise ValueError("Bump requires a writer and an independent reviewer")
        self.writers = roster.agents[:-1]

    def state(self):
        from . import bump_tasks
        return bump_tasks.load(self.paths.forum)

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

    def refresh(self, result):
        from . import bump_tasks
        state = self.state()
        bump_tasks.refresh(self.paths.forum, self.inventory(state), result.get("diagnostics", []),
                           main_sha=git(self.paths.project_root, "rev-parse", "HEAD"),
                           config_revision=state["config"]["revision"],
                           checked_files=result.get("checked_files", ()), complete=False)
        with bump_tasks.transaction(self.paths.forum) as state:
            # Large compiler output belongs in artifacts, not duplicated task state.
            state["last_build"] = {key: value for key, value in result.items()
                                   if key not in {"output", "stdout", "stderr", "source_identity"}}
            for task in state["tasks"].values():
                if task["kind"] == "configuration" and not task["files"]:
                    task["files"] = [name for name in CONFIG_FILES if (self.paths.project_root / name).exists()]

    async def build(self, *, modules=(), stream=False):
        from . import bump_verification
        state = self.state()
        loop = asyncio.get_running_loop()
        def on_output(channel, text):
            loop.call_soon_threadsafe(self.diagnostics_queue.put_nowait, (channel, text))
        def execute():
            with bump_jobs.cancellation_scope(self.build_cancel):
                return bump_verification.build(self.paths.project_root, modules=modules,
                    lake_args=state["config"].get("lake_args", ()), scope=state["config"].get("scope"),
                    on_output=on_output if stream else None)
        return await self.checked_job(execute)

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
        system += "\nYou are " + agent.name + ". Team: " + ", ".join(a.name for a in self.roster.agents)
        instruction = (f"Your task is {task_id}. Read bump_task and bump_brief, communicate useful findings, "
                       "and work only on leased files. Submit a repair or yield with a concrete blocker. "
                       "Do not run Unity internals, edit controller state, or invoke another pipeline.")
        if profile == "critic":
            instruction = "Independently review the current snapshot and full migration diff; submit_migration_verdict."
        async with self.agent_locks[agent.name]:
            return await spawn(agent, system, instruction, tree,
                mcp_servers(self.paths, profile), own_process_group=True,
                interrupt_event=interrupt, mcp_profile="bump",
                env_overrides=runtime_env(self.paths, agent.name, task_id, profile, state["config"]),
                log_context={"command": "bump", "run_id": state["run_id"],
                             "phase": profile, "task_id": task_id, "role": profile})

    def launch_ready(self):
        from . import bump_tasks
        if stopped(self.paths):
            return
        state = self.state()
        configuration_pending = any(t["kind"] in {"configuration", "dependency", "scope_review"}
                                    and t["status"] not in {"complete", "grouped", "retired"}
                                    for t in state["tasks"].values())
        for agent in self.writers:
            if agent.name in self.workers or self.agent_locks[agent.name].locked():
                continue
            ready = sorted(state["tasks"].values(), key=lambda row: (
                row.get("kind") not in {"configuration", "dependency", "imports"}, row["task_id"]))
            for item in ready:
                configuration = item["kind"] in {"configuration", "dependency", "scope_review"}
                if configuration_pending and (not configuration or self.workers):
                    continue
                if item.get("status") not in {"pending", "needs_repair", "ready"}:
                    continue
                if any(state["tasks"].get(key, {}).get("status") not in {"complete", "clean", "merged"}
                       for key in item.get("dependencies", [])):
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

    async def reap_workers(self):
        from . import bump_tasks
        for author, record in list(self.workers.items()):
            task = record["task"]
            if not task.done():
                continue
            error = task.exception() if not task.cancelled() else None
            if isinstance(error, SpawnCleanupError):
                raise error
            state = self.state()
            pending = any(row["task_id"] == record["task_id"] and row.get("status") in {"submitted", "merging"}
                          for row in state["candidates"].values())
            if not pending:
                bump_tasks.release(self.paths.forum, author, record["task_id"],
                    "Worker failed: " + type(error).__name__ if error else "Worker ended without a submitted repair", closed=True)
            self.workers.pop(author)

    async def handle_requests(self):
        """Configuration work gets an exclusive writer and a drained environment."""
        from . import bump_tasks
        state = self.state()
        requests = [r for r in state.get("config_requests", {}).values() if r["status"] == "pending"]
        for author, record in list(self.workers.items()):
            task = state["tasks"][record["task_id"]]
            if requests or task.get("yield_requested"):
                await stop_worker(record["task"], record["interrupt"])
        await self.reap_workers()
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
            if configuration:
                setup = bump_setup.prepare_environment(tree, state["target_version"], config.get("pins", {}),
                    modules=config.get("modules", ()), project_scope=config.get("project_scope", "build"),
                    lake_args=config.get("lake_args", ()))
                if setup.get("phase") != "ready":
                    return {"accepted": False, "reason": "Configuration still has setup blockers", "setup": setup}
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
            result = ({"accepted": bool(build.get("source_stable", True) and not build.get("unmapped")),
                       "reason": "configuration resolved; current compiler diagnostics retained"} if configuration else
                      bump_verification.candidate_progress(state.get("last_build", {}),
                                                           build, task, self.inventory(state)))
            if not result["accepted"]:
                return {**result, "build": build}
            git(tree, "add", "--", *changed)
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
                self.refresh(await self.build())
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
            self.refresh(await self.build())
            return False
        snapshot = await self.checked_job(bump_verification.verify_final, self.paths.project_root,
            base_sha=state["base_sha"], scope=state["config"]["scope"],
            lake_args=state["config"].get("lake_args", ()), artifacts_dir=self.paths.artifacts,
            config_revision=state["config"]["revision"])
        with bump_tasks.transaction(self.paths.forum) as current:
            current["review"] = {"snapshot": snapshot, "verdicts": []}
        if not snapshot.get("passed"):
            repairable = self.queue_audit_repairs(snapshot.get("issues", []))
            if any(row.get("severity") == "error" for row in snapshot.get("diagnostics", [])):
                self.refresh(snapshot)
                return False
            if repairable:
                return False
            raise ValueError("Final Bump verification did not pass; inspect stored build/audit evidence")
        authors = {row["author"].casefold() for row in state["candidates"].values() if row.get("status") == "merged"}
        critics = [agent for agent in self.roster.agents if agent.name.casefold() not in authors]
        if not critics:
            raise ValueError("No independent critic remains; migration is built but unreviewed")
        phase(self.paths, "critic")
        for agent in critics:
            attempt = 0
            while attempt < self.max_attempts:
                attempt += 1
                if stopped(self.paths):
                    return False
                tree = task_worktree(self.paths, agent.name + "-critic", "critic")
                interrupt = asyncio.Event()
                worker = asyncio.create_task(self.worker(agent, "critic", tree, "critic", interrupt))
                try:
                    while not worker.done():
                        if stopped(self.paths):
                            return False
                        await asyncio.sleep(0.1)
                    await worker
                finally:
                    await stop_worker(worker, interrupt)
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
                raise ValueError("Independent migration review requested changes; inspect verdict")
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
                parsed = bump_verification._parse_diagnostics(complete_text, self.paths.project_root)
                if parsed and parsed != last_parsed:
                    bump_tasks.refresh(self.paths.forum, self.inventory(), parsed,
                        main_sha=self.state()["main_sha"], config_revision=self.state()["config"]["revision"], complete=False)
                    last_parsed = parsed
                    self.launch_ready()
                await self.reap_workers()
                await asyncio.sleep(0.1)
            self.refresh(await self.initial_build)
            self.initial_build = None
            while not stopped(self.paths):
                await self.reap_workers()
                await self.handle_requests()
                if await self.integrate_ready():
                    continue
                self.launch_ready()
                state = self.state()
                pending = [task for task in state["tasks"].values()
                           if task.get("status") not in {"complete", "clean", "merged", "retired", "grouped"}]
                if not self.workers:
                    if pending:
                        raise ValueError("Bump has blocked or exhausted repairs; inspect Forum task evidence")
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
    controller = Controller(paths, roster, max_attempts=max_attempts, **controller_options)
    try:
        return await controller.run()
    finally:
        state = bump_tasks.load(paths.forum)
        if state["phase"] != "complete":
            phase(paths, "stopped")
            snapshot = state.get("review", {}).get("snapshot", {})
            bump_verification.persist_report(paths, bump_tasks.load(paths.forum), snapshot, None)
