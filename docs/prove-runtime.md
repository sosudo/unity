# Event-driven `prove` runtime

`unity prove` is intentionally separate from the older phase orchestrator used by the other commands.
Its lifecycle is:

1. deterministically inspect the toolchain, Lake project, Git baseline, and build;
2. mechanically discover named `sorry`/`axiom` targets as goals;
3. create a run under `.unity/runs/<run-id>/` and seed generic coordination slots for runnable goals;
4. activate those slots up to roster capacity; agents inspect shared state and atomically publish their own plans;
5. accept dynamic tasks and live findings through atomic Forum APIs;
6. capture submitted candidate commits, source bytes, hashes, and full baseline diffs;
7. verify the exact detached revision (target/signature, forbidden constructs, new/custom axioms,
   and `lake build`);
8. cancel dominated search immediately, schedule independent review, and turn objections into fix tasks;
9. atomically reserve an acceptable candidate, cherry-pick its complete revision chain, close the goal,
   and cancel every remaining task for it.

## State and locking

The current proof-search graph is `state/runtime.json`, guarded by an exclusive `fcntl` lock and replaced
atomically after every transition. It contains current goals, tasks, findings, content-addressed artifact
metadata, immutable candidate identities plus mutable review state, workers, policy, and compact telemetry.
The append-only `logs/events.jsonl` is telemetry only and is never prompt memory.

Task claims are leases. One owner is allowed by default; deliberate independent redundancy creates a
separate sibling task. Expired leases become stale and are reclaimable. Candidate acceptance uses an
`acceptable -> accepting` reservation so an objection and merge cannot race through each other.

A single unresolved theorem fans out into generic coordination slots. The scheduler does not prescribe a
menu of approaches: each agent reads the Forum brief and atomically turns its slot into a theorem-specific
task kind, normalized strategy key, and plan. Conflicting plans identify the existing owner and must be
changed or explicitly coordinated. This makes the configured roster useful even for one target without
assigning two workers the same exclusive task.
`UNITY_PROVE_SWARM_LIMIT` optionally caps this fan-out; by default it uses the full roster. Once any candidate
passes deterministic verification, the remaining speculative tasks and workers are dominated and cancelled.

## Compatibility boundary

Legacy Forum threads, votes, chunk claims/results, handoffs, DAG views, and all non-`prove` pipelines remain
available. During `prove`, new structured APIs are authoritative; legacy `forum_result(build_ok=true)` has no
effect on goal/candidate trust. ICRL balances remain legacy telemetry and do not affect prove routing.

Static project configuration remains in `.unity/`. Runtime state, forum posts, artifacts, candidate patches,
verification logs, and model/event logs are run-scoped. Worktrees receive a `.unity` symlink and all MCP/shell
bridges follow `.unity/current-run.json`, so source is isolated while coordination is shared.

## Default acceptance and budget policy

The default gate is a passing machine verification, one independent endorsement, and zero open objections.
`UNITY_PROVE_REVIEW_QUORUM` can change the endorsement count. Task leases, wall/token/tool limits, attempts,
allowed target axioms (`UNITY_PROVE_ALLOWED_AXIOMS`), and scheduler polling have `UNITY_PROVE_*` environment
overrides. Exhaustion never weakens verification or review policy.
