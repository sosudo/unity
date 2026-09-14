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
  `{requirement_id, status, declarations, checked_anchor_ids, rationale, argument_rationale}`,
  a global `scope_rationale`, and `repair_reviews` with `{repair_id,status,rationale}` for every adopted
  correction. Approval requires every recorded requirement
  exactly once, all passing, with all adopted outputs of its implementing nodes listed in
  `declarations` (definitions/structures/instances as well as theorems) and concrete reasoning. Rejections can be partial;
  a Lean reopen requires exact affected task IDs. Free-text `evidence` alone cannot approve.
- `autoformalize_requirements(offset?, limit?)` pages the complete requirement manifest. Keep one revision
  and continue until `next_offset` is null; the task-filtered brief is not the entire checklist.
  `autoformalize_task(task_id)` retrieves exact task evidence/anchors/prerequisites.
- `request_rechunk(author, reason, task_ids?)` queues corrected task organization, dependencies or
  requirement-to-task/prerequisite/argument mappings under the same frozen source obligations.
  It cannot revise the original requirement statements, anchors or scope; report such defects explicitly
  in the Forum and verdict instead. Ordinary mutable interpretation/implementation defects use `lean_reopen`;
  formalizers can refine nodes and submit new candidate versions. Original source is preserved.
- `forum_post` and `forum_read` provide necessary clarification and evidence-backed source defect reports.
- `report_source_issue(author, anchor_ids, description, task_ids?)` routes source defects to optional
  exploration/repair work. End the current review after reporting; it cannot approve unresolved issues.

No paper rewriting or mandatory informal-solving phase is available. Report substantive source defects
with evidence; never approve an altered statement to avoid the defect. The critic does not
merge code, edit Lean or adopt representations. Distinguish historical representation candidates from
current machine-review evidence. Assignment, representation, verification and faithfulness are separate;
the critic provides diagnostic feedback or the final source-faithfulness verdict for current outputs.
