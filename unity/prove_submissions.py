"""Controller-owned reservations and Git-only proof submissions.

``prepare(author, decl)`` raises ValueError if an attempt cannot safely start or
resume. ``poll({author: decl})`` returns newly submitted candidates; malformed,
stale, or otherwise ineligible commits are ignored and can be retried later.
A submission is a commit with subject exactly ``prove-candidate: <decl>``.
"""

import subprocess
import uuid
from pathlib import Path

from . import prove_state, worktree


def _git(path, *args, strip=True):
    result = subprocess.run(["git", *args], cwd=path, text=True, capture_output=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "git command failed")
    return result.stdout.strip() if strip else result.stdout.removesuffix("\n")


class NoToolSubmissions:
    def __init__(self, paths, worktrees):
        self.root = Path(paths.project_root)
        self.state_dir = Path(paths.forum)
        self.worktrees = {name: Path(path) for name, path in worktrees.items()}

    def _tree(self, author):
        tree = self.worktrees.get(author)
        if tree is None or tree.resolve() != worktree.agent_worktree(self.root, author).resolve():
            raise ValueError(f"invalid worktree for {author}")
        if _git(tree, "symbolic-ref", "--short", "HEAD") != worktree.agent_branch(author):
            raise ValueError(f"invalid branch for {author}")
        return tree

    @staticmethod
    def _ready(state, decl):
        declaration = state["declarations"].get(decl)
        if not declaration or declaration.get("status") != "unresolved":
            return False
        by_chunk = {d.get("chunk_id"): d for d in state["declarations"].values()}
        for dep in declaration.get("dependencies", []):
            target = state["declarations"].get(dep) or by_chunk.get(dep)
            if not target or target.get("status") != "solved":
                return False
        return True

    def _ancestor(self, tree, sha):
        return subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"],
                              cwd=tree, capture_output=True).returncode == 0

    def prepare(self, author, decl):
        tree = self._tree(author)
        state = prove_state.load_state(self.state_dir)
        if not self._ready(state, decl):
            raise ValueError(f"declaration {decl!r} is not ready")
        main_sha = worktree.main_commit(self.root)
        if state.get("main_sha") != main_sha:
            raise ValueError("recorded main commit does not match repository HEAD")
        # Only reservations created by this adapter may preserve an existing tree.
        for strategy in state["strategies"].values():
            if (strategy.get("creator") == author and strategy.get("owner") == author
                    and strategy.get("decl") == decl and strategy.get("status") == "claimed"
                    and strategy.get("description", "").startswith("no-tool reservation ")
                    and strategy.get("claimed_main_sha") == main_sha
                    and self._ancestor(tree, strategy["claimed_main_sha"])):
                return strategy
        if not worktree.worktree_matches_main(self.root, author):
            raise ValueError(f"worktree for {author} must be clean and match main")
        result = prove_state.register_strategy(
            self.state_dir, author, "no-tool reservation " + uuid.uuid4().hex, decl=decl)
        if result.get("status") != "registered":
            raise ValueError(f"cannot reserve {decl!r}: {result.get('status')}")
        result = prove_state.claim_strategy(self.state_dir, result["strategy"]["strategy_id"], author, main_sha)
        if result.get("status") != "claimed":
            raise ValueError(f"cannot claim {decl!r}: {result.get('status')}")
        return result["strategy"]

    def poll(self, worker_decls):
        submitted = []
        for author, decl in worker_decls.items():
            try:
                tree = self._tree(author)
                state = prove_state.load_state(self.state_dir)
                if (not self._ready(state, decl)
                        or state.get("main_sha") != worktree.main_commit(self.root)):
                    continue
                strategies = [s for s in state["strategies"].values()
                              if s.get("creator") == author and s.get("owner") == author
                              and s.get("decl") == decl and s.get("status") == "claimed"
                              and s.get("description", "").startswith("no-tool reservation ")]
                for strategy in strategies:
                    base = strategy.get("claimed_main_sha")
                    if base != state.get("main_sha") or not self._ancestor(tree, base):
                        continue
                    commits = _git(tree, "rev-list", "--reverse", f"{base}..HEAD").splitlines()
                    for sha in commits:
                        if _git(tree, "show", "-s", "--format=%s", sha, strip=False) != f"prove-candidate: {decl}":
                            continue
                        # The unchanged merger cherry-picks one commit, not an attempt's history.
                        # Require a complete patch based directly on the reserved main revision.
                        if _git(tree, "show", "-s", "--format=%P", sha) != base:
                            continue
                        if any(c.get("author") == author and c.get("decl") == decl
                               and c.get("commit_sha") == sha for c in state["candidates"].values()):
                            continue
                        resolved = worktree.verify_candidate_commit(self.root, author, sha)
                        result = prove_state.emit_candidate(self.state_dir, strategy["strategy_id"],
                                                            author, resolved, decl=decl)
                        submitted.append(result["candidate"])
                        break
                    if submitted and submitted[-1]["author"] == author:
                        break
            except (ValueError, RuntimeError, OSError):
                continue
        return submitted
