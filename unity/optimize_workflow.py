"""Metric-driven Optimize controller; no migration or cross-pipeline state.

Agents choose approaches and the primary chooses winners. The controller owns
checks, publication and recovery. A finite unsuccessful search is not optimality.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
from threading import Event
import uuid

from . import artifacts, optimize_jobs as jobs, optimize_tasks as tasks
from . import optimize_metrics as metrics, optimize_verification as verification
from . import optimize_worktree as worktree
from . import optimize_workspace as workspace
from .config import Paths
from .optimize_spawn import SpawnCleanupError, spawn


def paths_for(root):
    paths = Paths.from_unity_dir(Path(root).resolve() / ".unity")
    return replace(paths, forum=paths.unity / "forum" / "optimize")


def git(root, *args, check=True, input=None, raw=False):
    result = jobs.run(Path(root), ["git", "-c", "core.hooksPath=/dev/null",
        "-c", "commit.gpgsign=false", *args], cwd=Path(root), input=input)
    if check and result.returncode:
        raise ValueError(result.stderr.strip() or "Optimize Git operation failed")
    return (result.stdout if raw else result.stdout.strip()) if check else result


def write_json(path, value):
    artifacts._atomic_write(Path(path), (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


def stopped(paths):
    return (paths.unity / "stop-requested").exists()


def phase(paths, value):
    with tasks.transaction(paths.forum) as state:
        state["phase"] = value
    write_json(paths.unity / "state.json", {"command": "optimize", "phase": value,
        "workflow": tasks.SCHEMA})


@contextmanager
def run_lock(paths):
    paths.forum.mkdir(parents=True, exist_ok=True)
    with (paths.forum / "controller.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Optimize already has a live controller in this project") from exc
        yield


@contextmanager
def merge_lock(paths):
    with (paths.forum / "integration.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def source_files(root):
    names = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    return sorted({name for name in names if name.endswith(".lean") and name != "lakefile.lean"
        and not any(part.startswith(".") for part in Path(name).parts)
        and (Path(root) / name).is_file()})


def task_worktree(paths, author, task_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", author)
    tree = worktree.create_worktree(safe + "-" + uuid.uuid4().hex[:12], paths.project_root)
    worktree.symlink_lake_cache(tree, paths.project_root)
    return tree


def mcp_servers(paths, profile):
    servers = {
        "unity-forum": {"command": sys.executable, "args": ["-m", "unity.forum.optimize_server",
            "--forum-dir", str(paths.forum), "--project-root", str(paths.project_root), "--profile", profile]},
        "lean-lsp": {"command": "uvx", "args": ["lean-lsp-mcp"]},
    }
    if os.environ.get("AXLE_API_KEY"):
        servers["axle"] = {"command": "uvx", "args": ["--from", "axiom-axle-mcp", "axle-mcp-server"],
                           "env": {"AXLE_API_KEY": os.environ["AXLE_API_KEY"]}}
    if os.environ.get("ARISTOTLE_API_KEY"):
        servers["aristotle"] = {"command": sys.executable, "args": ["-m", "unity.aristotle"],
                               "env": {"ARISTOTLE_API_KEY": os.environ["ARISTOTLE_API_KEY"]}}
    return servers


def runtime_env(paths, author, task_id, profile, tree, state, *, phase_dispatch_id=None):
    scratch = paths.unity / "tmp" / "optimize" / re.sub(r"[^A-Za-z0-9_-]", "_", author)
    for name in ("uv-tools", "uv-bin", "lake-cache"):
        (scratch / name).mkdir(parents=True, exist_ok=True)
    bin_dir = paths.unity / "bin" / "optimize-workflow"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "lake"
    script = f"#!{sys.executable}\nfrom unity.optimize_lake_guard import main\nraise SystemExit(main())\n"
    if not wrapper.exists() or wrapper.read_text() != script:
        artifacts._atomic_write(wrapper, script.encode())
        wrapper.chmod(0o700)
    bridge = bin_dir / "unity"
    bridge_script = f"#!{sys.executable}\nfrom unity.optimize_mcp_bridge import main\nraise SystemExit(main())\n"
    if not bridge.exists() or bridge.read_text() != bridge_script:
        artifacts._atomic_write(bridge, bridge_script.encode())
        bridge.chmod(0o700)
    env = {"TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch),
        "UV_TOOL_DIR": str(scratch / "uv-tools"), "UV_TOOL_BIN_DIR": str(scratch / "uv-bin"),
        "LAKE_CACHE_DIR": str(scratch / "lake-cache"), "UNITY_AGENT_NAME": author,
        "UNITY_OPTIMIZE_TASK_ID": task_id or "", "UNITY_OPTIMIZE_PROJECT_ROOT": str(paths.project_root),
        "UNITY_OPTIMIZE_PHASE_DISPATCH_ID": phase_dispatch_id or "",
        "UNITY_OPTIMIZE_PROFILE": profile, "UNITY_OPTIMIZE_WORKFLOW": tasks.SCHEMA,
        "UNITY_OPTIMIZE_WORKTREE": str(tree), "UNITY_OPTIMIZE_BASE_SHA": git(tree, "rev-parse", "HEAD"),
        "UNITY_OPTIMIZE_PROCESS_RECEIPTS": str(paths.forum / "processes"),
        "UNITY_OPTIMIZE_LAKE_ARGS": json.dumps(state["config"].get("lake_args", [])),
        "PATH": os.pathsep.join((str(bin_dir), str(Path(sys.executable).absolute().parent), os.environ.get("PATH", ""))),
        "PIP_REQUIRE_VIRTUALENV": "true", "PIP_DISABLE_PIP_VERSION_CHECK": "true"}
    real_lake = state["config"].get("real_lake") or shutil.which("lake")
    if real_lake:
        env["UNITY_REAL_LAKE"] = str(Path(real_lake).absolute())
    return env


async def stop_worker(task, interrupt, *, grace=5.0, hard=35.0):
    if not task.done():
        interrupt.set()
        done, _ = await asyncio.wait({task}, timeout=grace)
        if not done:
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=hard)
        if not done:
            raise SpawnCleanupError("Optimize worker cleanup exceeded its shutdown deadline")
    if not task.cancelled() and isinstance(task.exception(), SpawnCleanupError):
        raise task.exception()


class Controller:
    def __init__(self, paths, roster, *, max_attempts=math.inf):
        self.paths, self.roster, self.max_attempts = paths, roster, max_attempts
        self.writers = roster.agents
        self.workers = {}
        self.build_cancel = Event()

    def state(self):
        return tasks.load(self.paths.forum)

    async def checked_job(self, function, *args, **kwargs):
        def execute():
            with jobs.cancellation_scope(self.build_cancel):
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

    async def worker(self, agent, task_id: str | None, tree, profile, interrupt, instruction="",
                     *, phase_dispatch_id: str | None = None):
        state = self.state()
        if task_id and task_id not in state["tasks"]:
            raise ValueError("Cannot dispatch an unknown Optimize task: " + task_id)
        if task_id and profile != "optimizing":
            raise ValueError("Only optimization task turns can carry a task binding")
        prompts = Path(__file__).with_name("prompts")
        system = (prompts / "optimize" / (profile.upper() + ".md")).read_text()
        system += "\n\n" + (prompts / "OPTIMIZE_TOOLS.md").read_text()
        if self.paths.unity_md.exists():
            system += "\n\nProject instructions:\n" + self.paths.unity_md.read_text()
        system += f"\nYou are {agent.name}. Primary: {self.roster.primary.name}. Team: "
        system += ", ".join(a.name for a in self.roster.agents)
        if not instruction:
            if task_id:
                instruction = (f"Work on task {task_id}; read optimize_brief and optimize_task. "
                    "Use the forum, record evidence and submit a candidate or a concrete no-improvement/blocker result.")
            else:
                instruction = (f"This is a {profile} phase turn, not an assigned optimization task. "
                    "Read optimize_brief and follow this phase's instructions. "
                    "Use task_id='' for phase findings, questions and obstacles.")
                if profile in {"architect", "exploration", "chunking"}:
                    instruction += (" Record report_phase_result with evidence before finishing; "
                        "report blocked if the required phase work could not be completed.")
        return await spawn(agent, system, instruction,
            tree, mcp_servers(self.paths, profile), own_process_group=True,
            interrupt_event=interrupt, mcp_profile="optimize",
            env_overrides=runtime_env(self.paths, agent.name, task_id, profile, tree, state,
                                      phase_dispatch_id=phase_dispatch_id),
            log_context={"command": "optimize", "run_id": state["run_id"], "phase": profile,
                         "task_id": task_id or "", "phase_dispatch_id": phase_dispatch_id, "role": profile})

    @staticmethod
    def latest_plan(state):
        proposals = list(state["plan_proposals"].values())
        if not proposals:
            raise ValueError("Chunking ended without a complete declaration plan")
        proposals = [row for row in proposals if row.get("status") == "proposed"] or proposals
        return max(proposals, key=lambda row: (row["at"], int(row["proposal_id"].removeprefix("plan-"))))

    def check_phase_result(self, profile, dispatch_id):
        """Check collective preparation output, not model prose or unanimity.

        These receipts do not replace setup/build, plan or metric validation.
        Other phase turns retain their existing selection/review checks.
        """
        if profile not in {"architect", "exploration", "chunking"}:
            return
        state = self.state()
        dispatch = state["phase_dispatches"][dispatch_id]
        allowed = {"complete", "skipped"} if profile == "architect" else {"complete"}
        if not any(row["status"] in allowed for row in dispatch["results"].values()):
            missing = [author for author in dispatch["authors"] if author not in dispatch["results"]]
            blockers = {author: row["summary"] for author, row in dispatch["results"].items()
                        if row["status"] == "blocked"}
            raise ValueError(f"{profile} has no completed phase result; "
                             f"missing results: {missing}; blockers: {blockers}")
        if profile == "chunking":
            proposal = self.latest_plan(state)
            planned = tasks._validated_plan(state, list(proposal["tasks"].values()), proposal.get("exclusions"))
            required = {cid for task in planned.values() for cid in task["chunk_ids"]}
            missing = required - state["baseline_measurements"].keys()
            if missing:
                raise ValueError(f"Selected declarations are missing original metric measurements "
                                 f"({len(missing)}; sample: {sorted(missing)[:5]})")

    async def dispatch_phase(self, profile, *, primary=False, instruction=""):
        phase(self.paths, profile)
        state = self.state()
        readonly = profile != "architect"
        def context(root):
            return (verification.file_identity(root, scope=state["scope"],
                        lake_args=state["config"].get("lake_args", [])),
                    metrics.load_metric(root, state["metric"]["name"]))
        original = context(self.paths.project_root) if readonly else None
        records = []
        views = {}
        agents = [self.roster.primary] if primary else self.writers
        dispatch = tasks.begin_phase_dispatch(self.paths.forum, profile, [agent.name for agent in agents])
        dispatch_id = dispatch["dispatch_id"]
        dispatch_status, dispatch_reason = "interrupted", "Phase dispatch did not finish"
        try:
            for agent in agents:
                tree = (worktree.role_view(self.paths.project_root, "architect") if profile == "architect"
                        else task_worktree(self.paths, agent.name + "-" + profile, profile))
                interrupt = asyncio.Event()
                if readonly:
                    views[str(tree)] = context(tree)
                task = asyncio.create_task(self.worker(agent, None, tree, profile, interrupt, instruction,
                                                       phase_dispatch_id=dispatch_id))
                records.append((task, interrupt, tree))
            while any(not task.done() for task, _, _ in records):
                for task, _, _ in records:
                    if task.done() and not task.cancelled() and isinstance(task.exception(), SpawnCleanupError):
                        raise task.exception()
                if stopped(self.paths):
                    return False
                await asyncio.sleep(0.1)
            results = await asyncio.gather(*(task for task, _, _ in records), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            if stopped(self.paths):
                return False
            if readonly:
                if context(self.paths.project_root) != original:
                    raise ValueError(f"{profile} changed the read-only project snapshot")
                for _, _, tree in records:
                    if context(tree) != views[str(tree)]:
                        raise ValueError(f"{profile} changed its read-only source snapshot")
            elif profile == "architect":
                outcome = self.state()["phase_dispatches"][dispatch_id]["results"].get(self.roster.primary.name, {})
                if outcome.get("status") == "skipped":
                    tree = records[0][2]
                    if (git(tree, "status", "--porcelain", "--untracked-files=no")
                            or git(tree, "diff", "--name-only", state["main_sha"], "HEAD")):
                        raise ValueError("Architect clean skip left dependency/source changes")
            self.check_phase_result(profile, dispatch_id)
            dispatch_status, dispatch_reason = "complete", "Dispatched turns closed; required phase output present"
            return records
        except BaseException as exc:
            dispatch_status = "interrupted" if isinstance(exc, asyncio.CancelledError) or stopped(self.paths) else "blocked"
            dispatch_reason = type(exc).__name__ + ": " + str(exc)
            raise
        finally:
            outcomes = await asyncio.gather(*(stop_worker(task, interrupt) for task, interrupt, _ in records),
                                            return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
            # Never let receipt-writing failure mask an existing cleanup/runtime fault.
            active_error = sys.exception()
            try:
                tasks.close_phase_dispatch(self.paths.forum, dispatch_id, dispatch_status, dispatch_reason)
            except Exception as receipt_error:
                if active_error is None:
                    raise
                active_error.add_note("Optimize phase receipt could not be closed: " + type(receipt_error).__name__)

    async def prepare(self):
        state = self.state()
        if not state.get("architect_done"):
            records = await self.dispatch_phase("architect", primary=True)
            if not records:
                return False
            tree = records[0][2]
            before = state["main_sha"]
            changed = git(tree, "diff", "--name-only", before, "HEAD").splitlines()
            if git(tree, "status", "--porcelain", "--untracked-files=no"):
                raise ValueError("Architect left uncommitted setup changes; retained for recovery")
            if set(changed) - {"lakefile.lean", "lakefile.toml", "lake-manifest.json"}:
                raise ValueError("Architect changed more than dependency setup; nothing published")
            if changed:
                build = await self.checked_job(verification.build_scope, tree,
                    scope=state["scope"], project_root=self.paths.project_root)
                if not build["passed"]:
                    raise ValueError("Architect setup did not pass controller build; nothing published")
                after = git(tree, "rev-parse", "HEAD")
                write_json(self.paths.forum / "setup-integration.json", {"before": before, "after": after})
                git(self.paths.project_root, "merge", "--ff-only", after)
                self.recover_setup()
            with tasks.transaction(self.paths.forum) as current:
                current["architect_done"] = True
                current["setup_changes"] = {"before": before, "after": git(self.paths.project_root, "rev-parse", "HEAD"),
                                            "files": changed, "optimization_improvement": False}
        state = self.state()
        if not state["baseline"]:
            layout = await self.checked_job(workspace.discover, self.paths.project_root,
                ["--layout-only"], lake_args=state["config"].get("lake_args", []))
            build_dir = Path(layout["build_dir"])
            if build_dir.is_absolute() or ".." in build_dir.parts or str(build_dir) == "." or build_dir.parts[0] in {".unity", ".git"}:
                raise ValueError("Lake build directory cannot overlap or escape the project root/runtime")
            with tasks.transaction(self.paths.forum) as current:
                current["scope"]["build_dir"] = build_dir.as_posix()
                current["scope"]["files"] = [name for name in current["scope"]["files"]
                    if not Path(name).is_relative_to(build_dir)]
            state = self.state()
            baseline = await self.checked_job(verification.capture_baseline, self.paths.project_root,
                metric=state["metric"], scope=state["scope"], lake_args=state["config"].get("lake_args", []))
            tasks.set_baseline(self.paths.forum, baseline)
        if not self.state()["baseline_sealed"]:
            # One initial inventory is shared by exploration and planning. Resume
            # keeps existing IDs/proposals; sealed inventories are never replaced.
            state = self.state()
            if not state["files"]:
                inventory = tasks.scan_sources(self.paths.project_root, state["scope"]["files"])
                tasks.install_inventory(self.paths.forum, inventory)
        if not self.state().get("exploration_done"):
            if not await self.dispatch_phase("exploration"):
                return False
            with tasks.transaction(self.paths.forum) as current:
                current["exploration_done"] = True
        if not self.state()["baseline_sealed"]:
            if not await self.dispatch_phase("chunking"):
                return False
            state = self.state()
            # Proposals are complete, controller-validated collaborative plans.
            # Later revisions replace earlier drafts; old prose/disagreements are
            # retained as discussion, not a permanent unanimity gate.
            final_plan = self.latest_plan(state)
            required = {cid for task in final_plan["tasks"].values() for cid in task["chunk_ids"]}
            if required - state["baseline_measurements"].keys():
                raise ValueError("Selected declarations are missing original metric measurements")
            tasks.accept_plan(self.paths.forum, final_plan["proposal_id"])
            tasks.seal_baseline(self.paths.forum)
            with tasks.transaction(self.paths.forum) as current:
                current["measurements"] = deepcopy(current["baseline"]["measurements"])
        return True

    def recover_setup(self):
        path = self.paths.forum / "setup-integration.json"
        if not path.exists():
            return
        journal = json.loads(path.read_text())
        if journal.get("status") == "complete":
            return
        head = git(self.paths.project_root, "rev-parse", "HEAD")
        if head == journal["before"]:
            return
        if head != journal["after"]:
            raise ValueError("Unrecognized source change during Architect publication")
        with tasks.transaction(self.paths.forum) as state:
            state["main_sha"] = head
            state["architect_done"] = True
            state["setup_changes"] = {**journal, "optimization_improvement": False}
        write_json(path, {**journal, "status": "complete"})

    def launch_ready(self):
        if stopped(self.paths):
            return
        state = self.state()
        active = state["rounds"]["active"]
        if active is None:
            return
        ready = tasks.ready_tasks(state)
        frontier_limit = max(1, math.ceil(len(self.writers) / max(1, len(ready))))
        dispatched = {(r["author"], r["task_id"], r.get("task_revision", 0), r.get("base_main_sha")) for r in active["dispatched"]}
        for agent in self.writers:
            if agent.name in self.workers:
                continue
            for task in ready:
                tid = task["task_id"]
                if (agent.name, tid, task["revision"], state["main_sha"]) in dispatched:
                    continue
                if sum(row["task_id"] == tid for row in self.workers.values()) >= frontier_limit:
                    continue
                tree = task_worktree(self.paths, agent.name, tid)
                tasks.record_dispatch(self.paths.forum, agent.name, tid)
                with tasks.transaction(self.paths.forum) as current:
                    current["rounds"]["active"]["dispatched"][-1].update(worktree=str(tree), closed=False)
                interrupt = asyncio.Event()
                worker = asyncio.create_task(self.worker(agent, tid, tree, "optimizing", interrupt))
                self.workers[agent.name] = {"task": worker, "interrupt": interrupt, "task_id": tid, "tree": tree}
                break

    async def reap_workers(self):
        for author, record in list(self.workers.items()):
            worker = record["task"]
            if not worker.done():
                continue
            error = None if worker.cancelled() else worker.exception()
            if isinstance(error, SpawnCleanupError):
                raise error
            with tasks.transaction(self.paths.forum) as state:
                for row in state["assignments"].values():
                    if row["author"] == author and row["worktree"] == str(record["tree"]):
                        row["closed"] = True
                        if row["status"] == "running":
                            row.update(status="interrupted" if stopped(self.paths) else "ended",
                                       feedback=type(error).__name__ if error else "No submitted result")
                active = state["rounds"]["active"]
                if active:
                    for row in active["dispatched"]:
                        if row.get("worktree") == str(record["tree"]):
                            row["closed"] = True
                            row["interrupted"] = stopped(self.paths)
            self.workers.pop(author)

    async def drain(self):
        self.build_cancel.set()
        results = await asyncio.gather(*(stop_worker(r["task"], r["interrupt"]) for r in self.workers.values()),
                                       return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        await self.reap_workers()

    def check_candidate(self, candidate):
        state = self.state()
        tree, root = Path(candidate["worktree"]), self.paths.project_root
        task = state["tasks"][candidate["task_id"]]
        if candidate["base_main_sha"] != state["main_sha"] or candidate["task_revision"] != task["revision"]:
            return {"passed": False, "reason": "Stale integration base; keep patch and remeasure on current source", "stale": True}
        if git(tree, "status", "--porcelain", "--untracked-files=all"):
            return {"passed": False, "reason": "Commit the exact candidate before submission"}
        head = git(tree, "rev-parse", "HEAD")
        if git(tree, "merge-base", head, state["main_sha"]) != state["main_sha"]:
            return {"passed": False, "reason": "Candidate does not descend from its bound integration base"}
        diff = git(tree, "diff", "--binary", "--full-index", "--no-ext-diff", state["main_sha"], head, raw=True)
        changed = git(tree, "diff", "--name-only", "--no-renames", state["main_sha"], head).splitlines()
        if not changed or set(changed) - set(task["files"]):
            return {"passed": False, "reason": "Candidate is empty or changes unassigned files"}
        identity = verification.file_identity(root, scope=state["scope"], lake_args=state["config"].get("lake_args", []))
        before = {cid: state["measurements"].get(cid) for cid in task["chunk_ids"]}
        if any(not row or not metrics.validate_measurement_binding(row, state["metric"], identity) for row in before.values()):
            return {"passed": False, "reason": "Record current before measurements before editing; old source scores cannot be reused"}
        result = verification.verify_candidate(tree, baseline=state["baseline"], metric=state["metric"],
            scope=state["scope"], measurements=candidate["measurements"], task=task,
            comparison={"identity": identity, "measurements": before, "metric": state["metric"]},
            project_root=root, lake_args=state["config"].get("lake_args", []),
            judgments=candidate.get("judgments"), config_revision=state["config"]["revision"])
        return {**result, "commit_sha": head, "files": changed,
                "diff_sha256": hashlib.sha256(diff.encode()).hexdigest()}

    async def check_submissions(self):
        for candidate in self.state()["candidates"].values():
            if candidate["status"] != "submitted":
                continue
            owner = self.workers.get(candidate["author"])
            if owner and str(owner["tree"]) == candidate["worktree"]:
                # A worker may submit before finishing; never check mutable source.
                continue
            result = await self.checked_job(self.check_candidate, candidate)
            with tasks.transaction(self.paths.forum) as state:
                row = state["candidates"][candidate["candidate_id"]]
                row.update(status="checked" if result["passed"] else "stale" if result.get("stale") else "rejected",
                           verification=result)
                for field in ("commit_sha", "files", "diff_sha256"):
                    if field in result: row[field] = result[field]

    def integrate_selected(self, candidate):
        with merge_lock(self.paths):
            state = self.state()
            root = self.paths.project_root
            before = git(root, "rev-parse", "HEAD")
            if before != state["main_sha"] or git(root, "status", "--porcelain", "--untracked-files=no"):
                raise ValueError("Project changed outside Optimize integration; no reset performed")
            if (candidate["status"] != "selected" or candidate["selection"]["author"] != self.roster.primary.name
                    or candidate["base_main_sha"] != before):
                return False
            result = self.check_candidate(candidate)
            if not result["passed"] or result.get("diff_sha256") != candidate.get("diff_sha256"):
                with tasks.transaction(self.paths.forum) as current:
                    current["candidates"][candidate["candidate_id"]].update(status="rejected", verification=result)
                return False
            tree = task_worktree(self.paths, "integration", candidate["task_id"])
            diff = git(root, "diff", "--binary", "--full-index", "--no-ext-diff", before, candidate["commit_sha"], raw=True)
            applied = git(tree, "apply", "--3way", "--index", "-", input=diff, check=False)
            if applied.returncode:
                with tasks.transaction(self.paths.forum) as current:
                    current["candidates"][candidate["candidate_id"]].update(status="conflict", feedback="Retained patch conflicts with current source")
                return False
            git(tree, "-c", "user.name=Unity", "-c", "user.email=unity@localhost", "commit", "-m",
                "UNITY: merge chunk " + candidate["task_id"])
            after_identity = verification.file_identity(tree, scope=state["scope"], lake_args=state["config"].get("lake_args", []))
            measured = {cid: metrics.rebind_measurement(row, state["metric"], result["source_identity"], after_identity)
                        for cid, row in candidate["measurements"].items()}
            before_measures = {cid: state["measurements"][cid] for cid in state["tasks"][candidate["task_id"]]["chunk_ids"]}
            checked = verification.verify_candidate(tree, baseline=state["baseline"], metric=state["metric"], scope=state["scope"],
                measurements=measured, task=state["tasks"][candidate["task_id"]], project_root=root,
                comparison={"identity": verification.file_identity(root, scope=state["scope"], lake_args=state["config"].get("lake_args", [])),
                            "measurements": before_measures, "metric": state["metric"]},
                lake_args=state["config"].get("lake_args", []), judgments=candidate.get("judgments"))
            if not checked["passed"]:
                with tasks.transaction(self.paths.forum) as current:
                    current["candidates"][candidate["candidate_id"]].update(status="rejected", verification=checked)
                return False
            after = git(tree, "rev-parse", "HEAD")
            journal = {"before": before, "after": after, "candidate_id": candidate["candidate_id"],
                "task_id": candidate["task_id"], "files": candidate["files"], "measurements": measured,
                "verification": checked}
            write_json(self.paths.forum / "integration.json", journal)
            if git(root, "rev-parse", "HEAD") != before:
                raise ValueError("Source changed during integration verification")
            git(root, "merge", "--ff-only", after)
            with jobs.cancellation_disabled():
                self.recover_integration()
            return True

    def recover_integration(self):
        path = self.paths.forum / "integration.json"
        if not path.exists(): return
        journal = json.loads(path.read_text())
        if journal.get("status") == "complete": return
        head = git(self.paths.project_root, "rev-parse", "HEAD")
        if head == journal["before"]: return
        if head != journal["after"]:
            raise ValueError("Source differs from interrupted integration; no automatic reset")
        state = self.state()
        if state["main_sha"] not in {journal["before"], head}:
            raise ValueError("Integration journal conflicts with checkpoint")
        if state["candidates"][journal["candidate_id"]].get("status") != "merged":
            tasks.refresh_affected(self.paths.forum, journal["files"], main_sha=head,
                inventory=tasks.scan_sources(self.paths.project_root, source_files(self.paths.project_root)),
                measurements=journal["measurements"], integrated_task_id=journal["task_id"],
                integrated_candidate_id=journal["candidate_id"], verification=journal["verification"])
        write_json(path, {**journal, "status": "complete"})

    async def handle_requests(self):
        requests = [row for row in self.state()["scope_requests"].values() if row["status"] == "pending"]
        if not requests:
            return
        # Grouping can change dependencies/assignment ranges. Close writers first.
        for record in list(self.workers.values()):
            await stop_worker(record["task"], record["interrupt"])
        await self.reap_workers()
        for request in requests:
            tasks.apply_scope_request(self.paths.forum, request["request_id"])

    def close_unsuccessful_tasks(self):
        with tasks.transaction(self.paths.forum) as state:
            for task in state["tasks"].values():
                if task["status"] not in {"pending", "running"}:
                    continue
                assignments = [a for a in state["assignments"].values()
                    if a["task_id"] == task["task_id"] and a["task_revision"] == task["revision"]
                    and a["base_main_sha"] == state["main_sha"]]
                if (assignments and all(a.get("closed") for a in assignments)
                        and all(a["status"] == "no_improvement" for a in assignments)):
                    task["status"] = "no_improvement"

    async def optimize_round(self):
        phase(self.paths, "optimizing")
        selection_requested = set()
        while not stopped(self.paths):
            await self.reap_workers()
            await self.handle_requests()
            await self.check_submissions()
            for candidate in self.state()["candidates"].values():
                if candidate["status"] == "selected":
                    await self.checked_job(self.integrate_selected, candidate)
            self.close_unsuccessful_tasks()
            self.launch_ready()
            state = self.state()
            checked = [c for c in state["candidates"].values() if c["status"] == "checked"]
            signature = (state["main_sha"], tuple(sorted(c["candidate_id"] for c in checked)))
            if checked and self.roster.primary.name not in self.workers and signature not in selection_requested:
                selection_requested.add(signature)
                result = await self.dispatch_phase("optimizing", primary=True,
                    instruction="Selection-only turn in this same optimization round. Read all checked candidates and forum opinions; "
                    "select the best valid candidate for each task, resolving ties with reasons. Do not edit source, "
                    "start a new strategy, or change measurements. Explain any decision to select none.")
                if not result: return
                for _, _, tree in result:
                    if git(tree, "status", "--porcelain", "--untracked-files=no") or git(tree, "rev-parse", "HEAD") != state["main_sha"]:
                        raise ValueError("Selection-only turn changed its source view")
                continue
            if not self.workers:
                break
            await asyncio.sleep(0.1)

    async def review(self):
        # Re-measure current source, never silently carry old scores across edits.
        state = self.state()
        if any(task["status"] not in {"complete", "no_improvement", "grouped"} for task in state["tasks"].values()):
            with tasks.transaction(self.paths.forum) as current:
                current["round_feedback"] = "Tasks remain unresolved; retained candidates and compiler feedback are available"
            return False
        original_identity = verification.file_identity(self.paths.project_root, scope=state["scope"],
                                                       lake_args=state["config"].get("lake_args", []))
        records = await self.dispatch_phase("optimizing", instruction=(
            "Measurement refresh only, no source edits, strategy claims or new candidates. Collaboratively record current "
            "measurements for every selected baseline subject using record_current_measurement, the original method and "
            "actual current source evidence. Include unchanged declarations. Retain numeric or qualitative comparison reasons."))
        if not records: return False
        for _, _, tree in records:
            identity = verification.file_identity(tree, scope=state["scope"], lake_args=state["config"].get("lake_args", []))
            if identity != original_identity:
                raise ValueError("Measurement-only turn changed its source snapshot")
        state = self.state()
        snapshot = await self.checked_job(verification.verify_final, self.paths.project_root,
            baseline=state["baseline"], metric=state["metric"], scope=state["scope"], measurements=state["measurements"],
            selected_files=sorted({path for task in state["tasks"].values() if task["status"] != "grouped" for path in task["files"]}),
            lake_args=state["config"].get("lake_args", []), judgments=state.get("judgments"),
            config_revision=state["config"]["revision"])
        with tasks.transaction(self.paths.forum) as current:
            current["review"] = {"reviewer": self.roster.primary.name, "snapshot": snapshot, "verdicts": []}
        if not await self.dispatch_phase("critic", primary=True):
            return False
        state = self.state()
        current_identity = verification.file_identity(self.paths.project_root, scope=state["scope"],
                                                      lake_args=state["config"].get("lake_args", []))
        if current_identity != snapshot["source_identity"] or metrics.load_metric(self.paths.project_root, state["metric"]["name"]) != state["metric"]:
            raise ValueError("Source or metric changed during final review")
        verdict = state["review"].get("verdict")
        if not verdict:
            raise ValueError("Primary review ended without a snapshot-bound verdict")
        if verdict.get("verdict") == "approved":
            prospective = {**state, "phase": "complete"}
            verification.completion_report(prospective, snapshot, verdict)
            tasks.finish_round(self.paths.forum, outcome="optimized")
            prospective = {**self.state(), "phase": "complete"}
            receipt = verification.persist_report(self.paths, prospective, snapshot, verdict, accepted=True)
            with tasks.transaction(self.paths.forum) as current:
                current.update(phase="complete", final_report=receipt["artifacts"])
            phase(self.paths, "complete")
            return True
        reopen = verdict.get("reopen_tasks") or verdict.get("reopen_task_ids") or []
        with tasks.transaction(self.paths.forum) as current:
            for tid in reopen:
                current["tasks"][tid].update(status="pending", critic_feedback=verdict)
            if verdict.get("verdict") == "no_improvement" and not reopen and all(t["status"] in {"no_improvement", "grouped"} for t in current["tasks"].values()):
                current["phase"] = "no_improvement"
        return False

    async def run(self):
        self.recover_setup()
        self.recover_integration()
        if git(self.paths.project_root, "rev-parse", "HEAD") != self.state()["main_sha"]:
            raise ValueError("Source differs from Optimize checkpoint; no automatic reset")
        try:
            if not await self.prepare():
                return False
            while not stopped(self.paths):
                state = self.state()
                if state["rounds"]["active"] is None and state["rounds"]["started"] >= self.max_attempts:
                    phase(self.paths, "exhausted")
                    return False
                tasks.begin_round(self.paths.forum, max_rounds=self.max_attempts)
                await self.optimize_round()
                if stopped(self.paths): return False
                if await self.review(): return True
                if stopped(self.paths): return False
                state = self.state()
                tasks.finish_round(self.paths.forum, outcome="no_improvement" if state["phase"] == "no_improvement" else "incomplete")
                if state["phase"] == "no_improvement": return False
                # An empty/unusable round is a visible blocker, not an infinite retry loop.
                dispatched = state["rounds"]["active"]["dispatched"]
                if not dispatched:
                    phase(self.paths, "blocked")
                    return False
            return False
        except jobs.JobCancelled:
            if not stopped(self.paths): raise
            return False
        finally:
            await self.drain()


async def run(paths, roster, *, metric, targets="Universe", continue_=False,
              max_attempts=math.inf, retrospective=True):
    """Exactly one invocation in the existing project. No provider probes."""
    from .optimize_processes import assert_closed
    paths = paths_for(paths.project_root)
    with run_lock(paths):
        names = [agent.name for agent in roster.agents]
        if not names or len(set(name.casefold() for name in names)) != len(names):
            raise ValueError("Optimize needs nonempty unique agent names")
        present = (paths.forum / tasks.STATE_FILE).exists()
        if continue_ and not present:
            raise ValueError("No bound Optimize checkpoint; legacy DAG/critic flags cannot reconstruct a baseline. Existing data retained")
        if present and not continue_:
            raise ValueError("Optimize checkpoint exists; use --continue in this same project")
        assert_closed(paths.project_root)
        definition = metrics.load_metric(paths.project_root, metric)
        if git(paths.project_root, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError("Commit or separately preserve source edits before Optimize; no files reset")
        tracked = set(git(paths.project_root, "ls-files", "-z", raw=True).split("\0"))
        prior_scope = tasks.load(paths.forum)["scope"] if present else {}
        untracked = set(verification.file_identity(paths.project_root, scope=prior_scope)["files"]) - tracked
        if untracked:
            raise ValueError("Commit or separately preserve untracked project inputs before Optimize; private Git worktrees cannot inherit them. No files reset: "
                             + ", ".join(sorted(untracked)[:10]))
        if not present:
            files = source_files(paths.project_root)
            scope = {"targets": targets, "files": files, "modules": []}
            tasks.initialize(paths.forum, paths.project_root, run_id="optimize-" + uuid.uuid4().hex[:12],
                base_sha=git(paths.project_root, "rev-parse", "HEAD"), metric=definition, scope=scope,
                config={"primary": roster.primary.name, "agents": names, "targets": targets, "lake_args": [],
                        "real_lake": shutil.which("lake"), "max_rounds": None if math.isinf(max_attempts) else max_attempts})
        else:
            state = tasks.load(paths.forum)
            if state["root"] != str(paths.project_root) or state["metric"] != definition or state["scope"]["targets"] != targets:
                raise ValueError("Project, metric or targets differ from saved Optimize context")
            saved_limit = state["config"].get("max_rounds")
            if saved_limit != (None if math.isinf(max_attempts) else max_attempts):
                raise ValueError("MAX_ATTEMPTS differs from retained round budget; not reset by --continue")
            if state["config"]["primary"] != roster.primary.name or state["config"]["agents"] != names:
                raise ValueError("Roster identities differ from saved Optimize run")
            if state["phase"] == "complete":
                identity = verification.file_identity(paths.project_root, scope=state["scope"], lake_args=state["config"].get("lake_args", []))
                if identity != state["review"]["snapshot"]["source_identity"]:
                    raise ValueError("Completed Optimize source changed since acceptance")
                return verification.completion_report(state)
            with tasks.transaction(paths.forum) as current:
                # assert_closed above already verified prior process closure.
                # Interrupted phase receipts remain history, never fresh success.
                for dispatch in current.get("phase_dispatches", {}).values():
                    if dispatch["status"] == "running":
                        dispatch.update(status="interrupted", finished_at=tasks.now(),
                                        reason="Prior invocation closed before phase completion")
                for assignment in current["assignments"].values():
                    if assignment["status"] == "running":
                        assignment.update(status="interrupted", closed=True)
                        current["tasks"][assignment["task_id"]]["status"] = "pending"
                active = current["rounds"]["active"]
                if active:
                    interrupted = [r for r in active["dispatched"] if not r.get("closed") or r.get("interrupted")]
                    active.setdefault("interrupted_dispatches", []).extend(interrupted)
                    active["dispatched"] = [r for r in active["dispatched"] if r not in interrupted]
        # Only verified prior closure permits removal of a previous native marker.
        (paths.unity / "stop-requested").unlink(missing_ok=True)
        controller = Controller(paths, roster, max_attempts=max_attempts)
        try:
            await controller.run()
        except BaseException as exc:
            try:
                with tasks.transaction(paths.forum) as current:
                    current.update(phase="stopped" if stopped(paths) else "blocked", error=type(exc).__name__ + ": " + str(exc))
            except Exception as report_error:
                exc.add_note("Optimize error checkpoint could not be saved: " + type(report_error).__name__)
            raise
        finally:
            active_error = sys.exception()
            try:
                state = tasks.load(paths.forum)
                if stopped(paths) and state["phase"] != "complete": phase(paths, "stopped")
                state = tasks.load(paths.forum)
                receipt = verification.persist_report(paths, state)
                with tasks.transaction(paths.forum) as current:
                    current["final_report"] = receipt["artifacts"]
                write_json(paths.unity / "state.json", {"command": "optimize", "phase": state["phase"], "workflow": tasks.SCHEMA})
            except Exception as report_error:
                if active_error is None:
                    raise
                # Accounting must never turn a fatal owned-process cleanup
                # failure into an ordinary, possibly retryable report error.
                active_error.add_note("Optimize final report could not be saved: " + type(report_error).__name__)
        if retrospective and not stopped(paths):
            await run_retrospective(controller)
        return verification.completion_report(tasks.load(paths.forum))


async def run_retrospective(controller):
    """Optional lessons, separate from optimization acceptance."""
    from . import library
    paths, original = controller.paths, controller.state()["phase"]
    state = controller.state()
    def canonical_context():
        return (verification.file_identity(paths.project_root, scope=state["scope"],
                    lake_args=state["config"].get("lake_args", [])),
                metrics.load_metric(paths.project_root, state["metric"]["name"]))
    before = canonical_context()
    fatal_cleanup = False
    path = paths.forum / "retrospective.json"
    write_json(path, {"status": "incomplete", "run_id": controller.state()["run_id"]})
    try:
        directory = library.ensure_library().resolve()
        await controller.dispatch_phase("retrospective", primary=True, instruction=(
            f"Record reusable lessons under {directory}. Write {path} as {{status: written, entries: [{{path, evidence}}]}} "
            "or {status: no_changes, reason: ...}. Do not change source or acceptance."))
        result = json.loads(path.read_text())
        if result.get("status") == "no_changes" and result.get("reason"):
            pass
        elif result.get("status") == "written" and result.get("entries"):
            for row in result["entries"]:
                file = Path(row["path"])
                file = (file if file.is_absolute() else directory / file).resolve(strict=True)
                if not file.is_relative_to(directory) or file.suffix != ".md" or not row.get("evidence") or not file.read_text().strip():
                    raise ValueError("Retrospective entry lacks retained library evidence")
                row["sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
        else:
            raise ValueError("Retrospective did not save a validated outcome")
    except SpawnCleanupError as exc:
        fatal_cleanup = True
        try:
            write_json(path, {"status": "cleanup_failed", "reason": str(exc),
                              "run_id": controller.state()["run_id"]})
        except Exception as report_error:
            exc.add_note("Retrospective cleanup receipt could not be saved: " + type(report_error).__name__)
        raise
    except Exception as exc:
        result = {"status": "incomplete", "reason": type(exc).__name__ + ": " + str(exc)}
    finally:
        active_error = sys.exception()
        drift = None
        try:
            if canonical_context() != before:
                drift = ValueError("Retrospective changed the accepted canonical source or metric")
        except Exception as exc:
            drift = ValueError("Retrospective canonical source or metric could not be verified: " + type(exc).__name__)
        try:
            if drift is not None or fatal_cleanup:
                with tasks.transaction(paths.forum) as current:
                    prior_report = current.get("final_report")
                    if prior_report:
                        current.setdefault("prior_final_reports", []).append(prior_report)
                    current.update(phase="blocked", error=str(drift or active_error))
                current = controller.state()
                receipt = verification.persist_report(paths, current)
                with tasks.transaction(paths.forum) as current:
                    current["final_report"] = receipt["artifacts"]
                write_json(paths.unity / "state.json", {"command": "optimize", "phase": "blocked", "workflow": tasks.SCHEMA})
                write_json(path, {"status": "cleanup_failed" if fatal_cleanup else "context_changed",
                                  "reason": str(drift or active_error), "run_id": state["run_id"]})
            else:
                phase(paths, original)
        except Exception as report_error:
            if active_error is None:
                raise
            active_error.add_note("Retrospective final state could not be saved: " + type(report_error).__name__)
        if drift is not None:
            if active_error is not None:
                active_error.add_note(str(drift))
            else:
                raise drift
    write_json(path, {**result, "run_id": controller.state()["run_id"]})
