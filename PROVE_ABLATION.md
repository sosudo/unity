# Prove without shared proof controls

Branch: `prove/no-forum`. Base: `prove/formalqualbench-evals` at
`b45e6ff2ad0e8a177370850014c08f805dd18989` (the recorded P20 version).

Run `unity prove` as usual. Proving agents work independently on assigned targets and use ordinary
Git commits to preserve progress. A complete proof is submitted in a commit with the exact subject
`prove-candidate: <exact declaration name>`. This commit must contain the complete patch and have
the supplied main revision as its sole parent. Squash intermediate progress before submission;
the existing merger applies one commit. The controller verifies and merges complete proof
commits, then synchronizes worker worktrees. Agents do not manually merge to main.

Prompts expose no Forum or proof-control tools or instructions. The critic and retrospective read the
controller's run snapshot from `.unity/prove-status.json`. Large output captured with `unity capture`
is retained locally and the command reports the blob path directly.

External Lean LSP, Axle, and Aristotle support remain available. Preserve the P16/P20 proof-integrity
and safety requirements, including exact target statements, compiler-verified proofs, and rejection of
`sorry`, `admit`, replacement axioms, `native_decide`, fake tactics, or other proof bypasses. Existing
Critic and retrospective phases remain part of the pipeline.

Removing agent-side controls requires a different submission and allocation path. The controller
reserves attempts internally, assigns ready targets, polls marked commits, and handles mechanical
review, failure feedback, integration retries, and synchronization. Internal reservations persist
across stopped or quiescent turns so unfinished work can resume. The existing proof-state store
remains at `.unity/forum/prove-state.json`; no MCP server exposes it. Strategy registration,
claiming, submission, synchronization, status, and artifact tools are unavailable to agents.

The roster and reasoning-effort configuration, worktree identity checks, target contracts,
mechanical verifier, merge lock, critic approval and reopening rules, and optional external prover
services remain. After an accepted merge, synchronization retains the baseline behavior of replacing
worker attempts with main; branches with other pending submitted candidates wait for their review.
Question wakeups and discussion briefs are removed. Other Unity commands retain their default tools.

This is a tool and prompt ablation, not a filesystem isolation boundary. Use a fresh benchmark
workspace when comparing variants. Offline tests use real Git with fake workers and compiler/reviewer
responses to check control flow. They do not establish benchmark success or validate real Lean proofs.
