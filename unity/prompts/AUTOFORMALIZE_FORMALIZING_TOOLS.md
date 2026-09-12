# Available tools for `unity autoformalize` — Lean formalization

Start with `autoformalize_brief(author)` and refresh frequently for source identity, ready tasks, strategies,
findings and candidate events. These tools belong to the autoformalization runtime; there is no solving phase.

- `register_strategy(author, description, target?, strategy_family?)` registers a materially distinct
  approach for the formal task ID. Prefer a suitable existing strategy. Use `claim_strategy`,
  `assist_strategy`, `unclaim_strategy` or `mark_strategy_incorrect`; own one claimed strategy at a time.
- `publish_finding(author, kind, title, content, confidence, target?, strategy_id?, evidence?, supersedes?)`
  shares reusable checked APIs, proof patterns and concrete failures. Publish before substantial
  follow-on work, with the formal task ID and check/artifact evidence; reuse existing findings.
  `report_obstacle`, `ask_question` and `answer_question` expose blockers and requests for help.
- `finalize_formalization(strategy_id, author, task_id, changed_paths?, notes?, supersedes?, stage?, outputs?)` commits the
  current exact worktree bytes and submits an immutable candidate for the authoritative main build and
  declaration review. Omit optional `changed_paths` to include all non-ignored project changes.
- `emit_formalization_candidate(strategy_id, author, task_id, commit_sha, notes?, supersedes?, stage?, outputs?)` is the
  compatibility route for already-committed bytes; normally use `finalize_formalization`.
- `sync_from_main(author, reason?)` merges accepted main without discarding local work. Uncommitted
  tracked edits and pending candidates block sync; unrelated untracked files do not. Git refuses to
  overwrite colliding untracked files. Conflicts remain for resolution and claims are retained. Do not sync
  just because an unrelated task merged.
- `refine_chunks(author, expected_revision, changes)` transactionally revises informal interpretations,
  predicted kinds, hints and typed dependencies, or adds/splits/combines nodes. Use `upserts`
  (complete node rows) and `replacements` (`{old_ids,new_ids,reason}` rows) in `changes`. Preserve existing
  node IDs for unchanged mathematics and all original obligations. Stale revisions fail atomically;
  refresh the brief before retrying. Source requirements/specification are read-only through this tool.
  To correct an adopted Lean encoding without changing its informal mathematics, add
  `reopen_representations=[{"task_id":"stable-node-id","reason":"why the encoding must change"}]`
  to `changes`. This explicitly revises the representation and invalidates its dependent evidence;
  do not rewrite correct informal prose merely to unlock a different Lean type/name/file.
  Resolve an existing planned prerequisite with
  `prerequisite_resolutions=[{"id":"P1","resolution":{"kind":"library","declaration":"Exact.name"}}]`
  in `changes`, or use the existing task-resolution shape for a supporting node. This changes only
  its implementation resolution, not the cited statement/anchors. Inspect the exact API and update
  corresponding node dependency edges as needed; unresolved choices never count as final evidence.
- `request_rechunk(author, reason, task_ids?)` queues a revised informal plan and argument/prerequisite
  mapping, without rewriting original source obligations. Give precise source locations/evidence; ordinary formal drafts use refinement or a new
  candidate, not mandatory rechunking. The supplied source is unchanged.
- `forum_post`, `forum_read`, `autoformalize_status`, `artifact_info` and bounded `artifact_read` provide detail.
- `autoformalize_task(task_id)` retrieves source anchors, requirements, argument mapping and prerequisites
  for a task. The normal brief prioritizes your task, its candidates and blockers.
- `report_source_issue(author, anchor_ids, description, task_ids?)` records a source defect.
  `submit_source_repair(author, issue_id, explanation, evidence, replacement?)` proposes an explicit
  evidence-backed spot repair. Unity routes it through chunking and independent review.

Original source bytes must not change. Explore source defects and propose explicit corrections instead
of modifying the input or silently formalizing a different statement.

Both candidate tools default to `stage="complete"`. Pass the first output binding as
`outputs=[{"declaration":"Project.name","file":"Project/File.lean"}]`; a node may have multiple outputs.
A complete implementation can adopt the representation and verify its proof in one submission.
`stage="representation"` shares a locally checked representation before proof completion; it may
contain theorem proof holes, never unfinished meaning-bearing definitions. Representation adoption
does not mean proof completion or faithfulness. Candidate versions retain exact commits/manifests.
Task details distinguish assignment (claims/assistants), representation, verification and faithfulness,
all with current revision-bound evidence; proof-only prerequisites need not delay statement work.

For a backend without native MCP, use `unity mcp unity-forum <tool> '<json-args>'`. For multiline Lean or
quoted content, serialize the JSON argument object and pass a file/stdin rather than hand-quoting Lean:

```sh
unity mcp axle check --args-file /path/to/request.json
unity mcp axle check --args-file - < /path/to/request.json
```

Use positional JSON or `--args-file`, not both. This works for all MCP servers. Native MCP uses structured
arguments directly. The bridge stores server stderr separately; a diagnostics artifact alone does not
mean failure. Inspect actual result/exit status and retrieve diagnostics only as needed.

Use enabled compatible Axle tools preferentially over equivalent Lean LSP tools. Use Lean LSP for local
goals/project diagnostics. Consult the injected catalogs for actual tool schemas and Aristotle availability.
The shared `.lake/packages` cache is controller-owned: do not run `lake clean`, `lake update`, `lake upgrade`,
`lake exe cache` or bare `lake build`; do not bypass these restrictions with `lean_build`.

Use the supplied non-login shell environment. Only fall back to `unity capture -- lake env lean
Project/File.lean` when MCP cannot provide the needed check, or targeted `unity capture -- lake build
Project.Module` when compiled artifacts are needed. Preserve exit status, poll a running check's same
session, and do not start duplicate checks for empty output. Finalize promptly after a successful targeted
local check of the exact current source; do not repeat checks against unchanged bytes.
