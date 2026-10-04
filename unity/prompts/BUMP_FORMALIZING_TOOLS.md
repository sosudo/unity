# Available tools for `unity bump` — declaration migration

Start with `bump_brief(author)` and refresh frequently for source identity, ready tasks, strategies,
findings and candidate events. These tools belong to Bump's independent Forum; there is no solving phase.
The source is the immutable original Lean snapshot. Compiler errors create declaration-level repairs;
the full original obligation ledger is larger than the active repair queue. Preserve the frozen
build/all selected scope, original/excluded bytes, definition behavior and per-declaration trust.

- `register_strategy(author, description, target?, strategy_family?)` registers a materially distinct
  approach for the declaration task ID. Prefer a suitable existing strategy. Use `claim_strategy`,
  `assist_strategy`, `unclaim_strategy` or `mark_strategy_incorrect`; own one claimed strategy at a time.
- `yield_task(author, task_id, reason, waiting_for?)` ends your blocked attempt. `waiting_for` is an
  optional list of existing dependency task IDs. It releases your claims and assistance for that task,
  and defers that task for you only; other workers can continue. End the turn after a `yielded` response;
  if helpers are already ready, refresh and continue; if a candidate is pending, follow its review
  interrupt. Unity handles reassignment and preserves private work. Use `unclaim_strategy` for ownership transfer or a
  new approach while continuing, not for ending a blocked attempt. Do not repeat unchanged blocker
  posts. There is no per-turn call limit.
- `publish_finding(author, kind, title, content, confidence, target?, strategy_id?, evidence?, supersedes?, declarations?, files?)`
  shares reusable checked APIs, proof patterns and concrete failures. Publish before substantial
  follow-on work, with the formal task ID and check/artifact evidence; reuse existing findings.
  `confidence` is an integer from 0 to 100: use `95`, not `0.95`. `kind` is an agent-chosen string,
  not a fixed enum. Use `supersedes` when new evidence replaces an active finding.
  For reusable Lean code, pass exact fully-qualified names in `declarations=["Project.helper"]` and
  explicit worktree-relative `.lean` paths in `files=["Project/Helper.lean", "Project/PrivateSupport.lean"]`.
  Include private imported files needed to integrate the helper; only explicitly named files in your
  existing worktree are captured, not an automatically discovered import closure. The saved bytes are
  immutable code artifacts. Names, confidence and reported local checks are agent-reported evidence,
  not Unity acceptance; publishing neither merges code nor verifies a task.
- `read_finding(finding_id)` retrieves the exact finding, including declarations, code artifact paths
  and capture context; legacy and superseded findings remain readable by ID. A `potentially stale`
  context warning does not discard the preserved bytes. Use `artifact_read` on each code attachment,
  consume its returned `content`, and follow `next_offset` until null to retrieve complete source.
  Inspect the exact names and imports, then integrate suitable code into your own assigned worktree
  and check it there, preserving existing edits. Never edit another worker's tree or assume that
  unpublished-on-main helper code is already present in your tree.
- `report_obstacle(author, goal_state, target?, tried?, hypothesis?)` records a concrete blocker.
- `ask_question(author, body, to?, target?)` asks for help; `answer_question(question_id, author, body)`
  answers an existing question.
- `forum_post(thread_id, author, content, reply_to?)` posts free-form discussion; `reply_to` is an
  optional list of post IDs. A post does not reserve work or submit a candidate.
- `reserve_files(author, task_id, paths, share_with?)` reserves worktree-relative paths for a claimed
  task. Same-task workers share them. Only the owner task can grant cross-task sharing with a list of
  task IDs in `share_with`. Independent declarations may share one module; do not replace declaration
  tasks with whole-file assignments. Main integration is serialized; conflicts preserve private edits.
- `finalize_formalization(strategy_id, author, task_id, changed_paths?, notes?, supersedes?, stage?, outputs?, obsolete_files?)` commits the
  current exact worktree bytes and submits an immutable candidate for authoritative compiler-progress
  checks against main. A repaired declaration may integrate while unrelated declarations in the same
  file still fail. A `diagnostic_repair` receipt is provisional, not native verification or final acceptance.
  Final acceptance requires the complete selected build, all-original native review and independent critic.
  A `kind=command` task repairs only its recorded original source line/range and may submit
  `outputs=[]`; never invent a declaration for an import-only source or edit the entire file.
  Omit optional `changed_paths` to include all non-ignored project changes.
  Submission preflight runs before staging/committing; a blocked or stale request preserves private
  work and does not queue verification. Refresh the exact task context and address its blocker.
  Read `bump_task(task_id).critic_feedback` before repair work: `direct` feedback applies to
  this task, while `upstream` feedback records relevant dependency defects. Its verdict, snapshot,
  reviewed main, requirement/task mappings, and historical/shared/lineage flags are provenance;
  revalidate historical feedback against the current task and exact bytes.
  Apply the repair checklist only where it is relevant to the assigned task. In `notes`, map every
  applicable `repair_steps` item to the actual declarations or dependency resolutions changed and the
  checked evidence, or state the exact remaining blocker. Compilation and reflexively repeating formula
  or declaration names are not semantic repair evidence. Address unresolved semantic steps and the
  concrete candidate-rejection blocker before finalizing; once they are actually addressed and targeted checks pass, resubmit promptly instead of
  waiting indefinitely for Forum approval. The independent critic, not the implementing migration worker,
  decides whether the new exact candidate resolves the source-faithfulness defect. An unrelated
  remaining compiler error does not require you to take over its declaration assignment.
- `emit_formalization_candidate(strategy_id, author, task_id, commit_sha, notes?, supersedes?, stage?, outputs?, obsolete_files?)` is the
  compatibility route for already-committed bytes; normally use `finalize_formalization`.
- `sync_from_main(author, reason?)` merges accepted main without discarding local work. Uncommitted
  tracked edits and pending candidates block sync; unrelated untracked files do not. Git refuses to
  overwrite colliding untracked files. Conflicts remain for resolution and claims are retained. Do not sync
  just because an unrelated task merged.
- `refine_chunks(author, expected_revision, changes)` transactionally revises informal interpretations,
  predicted kinds, hints and typed dependencies, or adds/splits/combines nodes. Use `upserts`
  (complete node rows) and `replacements` (`{old_ids,new_ids,reason}` rows) in `changes`. Preserve existing
  node IDs for unchanged mathematics and all original obligations. Stale revisions fail atomically;
  refresh the brief before retrying. Original source requirements/anchors/scope are read-only;
  the implementation resolution of an existing prerequisite is editable as described below.
  To correct an adopted Lean encoding without changing its informal mathematics, add
  `reopen_representations=[{"task_id":"stable-node-id","reason":"why the encoding must change"}]`
  to `changes`. This explicitly revises the representation and invalidates its dependent evidence;
  do not rewrite correct informal prose merely to unlock a different Lean type/name/file.
  Resolve an existing planned prerequisite with
  `prerequisite_resolutions=[{"id":"P1","resolution":{"kind":"declaration","declaration":"Exact.name"}}]`
  in `changes`. Unity checks external or project-local provenance; a local witness need not be another
  output/task. Use the existing task-resolution shape for genuinely separate provider work. Inline
  discharge or target-citation accounting uses `{"kind":"argument","rationale":"specific evidence"}`;
  Unity derives the consuming requirements and the critic checks correspondence. Preserve the source
  statement/anchors; do not invent a self-edge or mark an obligation true without evidence.
  Before yielding for a missing helper, actually add its node and the consumer's typed dependency edge
  through `refine_chunks`, then pass the helper task ID to `yield_task`. A finding or help request alone
  does not create a task or its dependency.
- `request_rechunk(author, reason, task_ids?)` queues a revised informal plan and argument/prerequisite
  mapping, without rewriting original source obligations. Give precise source locations/evidence; ordinary formal drafts use refinement or a new
  candidate, not mandatory rechunking. The supplied source is unchanged.
- `forum_read`, `bump_status`, `artifact_info` and bounded `artifact_read` provide detail.
- `bump_task(task_id)` retrieves source anchors, requirements, argument mapping and prerequisites
  for a task, including relevant dependency findings and current machine-verified dependency outputs.
  Its `critic_feedback.direct` and `critic_feedback.upstream` sections preserve critic repair checklists
  with verdict/snapshot/reviewed-main provenance, requirement/task mappings, and historical/shared/
  lineage flags. Historical feedback is context to revalidate,
  not an instruction to weaken current source or proof obligations and not current approval.
  `verification_blockers` records actual checked candidate failures. `submission_blockers` is current
  prospective submission preflight. `readiness` lists declared dependencies. `remaining_global_requirements`
  is completion accounting, not an additional task dependency. Do not stop independent work for it.
  Those verified outputs do not require a finding; representation adoption alone is not verification.
  `manifest_repairs` lists current focused requests with their context, blockers and prior attempts.
  Read these before repeating a repair. Preserve unrelated proofs and outputs. A request classified
  `output_manifest` is not proof that the change is metadata-only; missing mathematical witnesses or
  changed meanings need representation work and independent source-faithfulness review.
  Correct an adopted encoding with the existing `refine_chunks`/`reopen_representations` path.
  A `cleared` repair is a cleared diagnostic, not an accepted proof or a semantic approval.
  The normal brief prioritizes your task, its candidates and blockers, then reusable findings before
  the coverage summaries. Machine verification is not a source-faithfulness approval.
- `report_source_issue(author, anchor_ids, description, task_ids?)` records a suspected source defect.
  `submit_source_repair(author, issue_id, explanation, evidence, replacement?)` proposes an explicit
  evidence-backed spot repair. Unity diagnoses source-vs-encoding errors first; only a confirmed
  source-defect proposal automatically queues chunking. A false report does not require a new plan.

Original source bytes must not change. Diagnose source/mapping conflicts explicitly; a proposed
correction cannot waive the frozen original meanings, coverage or per-declaration trust boundary.
Compatible upgraded imports are assumed, not recursively migrated. Do not change dependencies or scope.

Both candidate tools default to `stage="complete"`. Pass the first output binding as
`outputs=[{"declaration":"Project.name","file":"Project/File.lean"}]`; a node may have multiple outputs.
A complete declaration repair can be submitted without waiting for unrelated same-file repairs.
No new holes, axioms or unfinished meaning-bearing definitions are allowed in either `stage="complete"`
or the inherited `stage="representation"` interface. Existing trust must not spread to another
declaration. Representation adoption and provisional integration are not native proof completion
or faithfulness. Candidate versions retain exact commits/manifests.
After adoption, Unity schedules targeted representation review before own/dependent proof work.
Unrelated work continues. Unchanged encodings reuse that evidence; final critic review remains.
After alignment approval, work on the remaining proof/construction and prerequisites; do not resubmit the
unchanged representation. Use `stage="complete"` when ready, or `refine_chunks` with
`reopen_representations` if the adopted encoding needs correction. An `already_adopted` response
queues no new candidate or review interrupt. Follow its `next_action`: continue useful proof work,
or call `yield_task` with a concrete blocker before ending the attempt; do not repeat the submission.
Likewise, `blocked` or `unchanged_failed` preserves the commit but queues no new review. Correct the
identified prerequisite/source inputs, not unrelated outputs or commit messages. If you cannot,
yield with the exact blocker IDs in the reason. Repeated receipt IDs or findings are not new proof
progress. Metadata-only corrections preserve unchanged Lean proof evidence but require semantic review.
Task details distinguish assignment (claims/assistants), representation, verification and faithfulness,
all with current revision-bound evidence; proof-only prerequisites need not delay statement work.

Only files introduced by this Bump run may be removed as superseded drafts.
Never delete a file captured in the original project baseline, even with replacement evidence.
For explicit superseded-draft cleanup, delete the file privately and pass
`obsolete_files=[{"path":"Project/Old.lean","replacement_candidate_id":"<current merged candidate>"}]`.
No automatic deletion occurs. The replacement must be integrated, adopted bindings preserved, and
imports/dependencies must still build. Normal edits to your own unbound files need no cleanup metadata.

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
session, and do not start duplicate checks for empty output. Finalize promptly when targeted diagnostics
show the assigned declaration error repaired, even if an unrelated declaration in the same file still
fails; report that distinction honestly. Do not repeat checks or representation submissions against
already adopted, unchanged bytes. Final whole-selected native verification remains controller-owned.
