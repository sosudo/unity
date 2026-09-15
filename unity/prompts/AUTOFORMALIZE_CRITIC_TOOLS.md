# Available tools for `unity autoformalize` — critic

Start with `autoformalize_brief(author)` and use `autoformalize_status()` for exact task/candidate state. Inspect the
supplied source snapshot and current review artifacts using `artifact_info`, bounded `artifact_read`,
and the source file paths in `.unity/formalization-plan.json`.

If the current snapshot has `passed=false`, this is diagnostic review of an exhausted or incomplete
round. Read yielded attempts, the last-round blockers and preserved-work checkpoints. Use `lean_reopen`
with exact task IDs and actionable next steps, or request an evidenced replan/source repair.
Use `not_checked` for unchecked requirements. A failed snapshot cannot approve, and unfinished
tasks do not justify reopening unrelated completed work. Repeated unchanged feedback is not a new attempt.

- `submit_formalization_verdict(author, verdict, summary, review, reopen_tasks?, evidence?)` submits
  `approved` or `lean_reopen`. `review` contains the current `snapshot_id` and `requirements` entries
  `{requirement_id, status, declarations, checked_anchor_ids, checked_prerequisite_ids, rationale, argument_rationale}`,
  a global `scope_rationale`, and `repair_reviews` with `{repair_id,status,rationale}` for every adopted
  correction. Approval requires every recorded requirement
  exactly once, all passing, with all adopted outputs of its implementing nodes listed in
  `declarations` (definitions/structures/instances as well as theorems) and concrete reasoning. Rejections can be partial;
  a Lean reopen requires exact affected task IDs. Free-text `evidence` alone cannot approve.
  Each entry's `checked_anchor_ids` must contain only IDs from that requirement's own `anchor_ids`,
  without duplicates; approval requires all of them. Put ancillary source/argument context in
  `rationale` or `argument_rationale`, not in `checked_anchor_ids`.
  Every passing entry must list in `checked_prerequisite_ids` the union of its argument mapping's
  prerequisite IDs and prerequisites whose `needed_by` includes any of its implementing tasks
  (empty when none, no extras or duplicates). Check each witness against the English statement.
  For `kind="argument"`, explain its inline discharge or target-citation accounting in
  `argument_rationale`; proof completion alone does not establish that correspondence.
- `autoformalize_requirements(offset?, limit?)` pages the complete requirement manifest. Keep one revision
  and continue until `next_offset` is null; the task-filtered brief is not the entire checklist.
  `autoformalize_task(task_id)` retrieves exact task evidence/anchors/prerequisites.
- `request_rechunk(author, reason, task_ids?)` queues corrected task organization, dependencies or
  requirement-to-task/prerequisite/argument mappings under the same frozen source obligations.
  It cannot revise the original requirement statements, anchors or scope; report such defects explicitly
  in the Forum and verdict instead. Ordinary mutable interpretation/implementation defects use `lean_reopen`;
  formalizers can refine nodes and submit new candidate versions. Original source is preserved.
- `publish_finding(author, kind, title, content, confidence, target?, strategy_id?, evidence?, supersedes?)`
  shares reusable review facts and concrete failures with evidence. `confidence` is an integer from
  0 to 100: use `95`, not `0.95`. `kind` is an agent-chosen string, not a fixed enum. Reuse existing
  findings; use `supersedes` when new evidence replaces an active finding.
- `report_obstacle(author, goal_state, target?, tried?, hypothesis?)` records a concrete blocker.
- `ask_question(author, body, to?, target?)` asks for help; `answer_question(question_id, author, body)`
  answers an existing question.
- `forum_post(thread_id, author, content, reply_to?)` posts free-form discussion; `reply_to` is an
  optional list of post IDs. A post is not a verdict. `forum_read` provides discussion detail.
- `report_source_issue(author, anchor_ids, description, task_ids?)` routes source defects to optional
  exploration/repair work. End the current review after reporting; it cannot approve unresolved issues.

For shell access, serialize the full JSON argument object, including `author`, `verdict`, `summary`
and the nested `review`, to a file; do not save only the `review` object. Use a JSON serializer to
escape multiline rationales and quotes, rather than hand-quoting the payload in the shell:

```sh
unity mcp unity-forum submit_formalization_verdict --args-file /path/to/review.json
```

Native MCP accepts the same structured arguments directly, not a JSON string. The critic instructions
show the nested `review` shape; wrap it with the tool arguments above, including `reopen_tasks` for
`lean_reopen`. Replace example IDs and rationales with current reviewed evidence.

No paper rewriting or mandatory informal-solving phase is available. Report substantive source defects
with evidence; never approve an altered statement to avoid the defect. The critic does not
merge code, edit Lean or adopt representations. Distinguish historical representation candidates from
current machine-review evidence. Assignment, representation, verification and faithfulness are separate;
the critic provides diagnostic feedback or the final source-faithfulness verdict for current outputs.
