# Available tools for `unity autoformalize` — critic

Start with `autoformalize_brief(author)` and use `autoformalize_status()` for exact task/candidate state. Inspect the
supplied source snapshot and current review artifacts using `artifact_info`, bounded `artifact_read`,
and the source file paths in `.unity/formalization-plan.json`.

- `submit_formalization_verdict(author, verdict, summary, review, reopen_tasks?, evidence?)` submits
  `approved` or `lean_reopen`. `review` contains the current `snapshot_id` and `requirements` entries
  `{requirement_id, status, declarations, rationale}`. Approval requires every recorded requirement
  exactly once, all passing, with valid declarations and concrete reasoning. Rejections can be partial;
  a Lean reopen requires exact affected task IDs. Free-text `evidence` alone cannot approve.
- `request_rechunk(author, reason)` requests a corrected protected statement/definition mapping for the
  same supplied source; obsolete verification/evidence is invalidated, and original source is preserved.
- `forum_post` and `forum_read` provide necessary clarification and evidence-backed source defect reports.

No paper rewriting or informal-solving phase is available. Report substantive source defects and reject
affected tasks with evidence; never approve an altered statement to avoid the defect. The critic does not
merge code or edit Lean. Distinguish historical scaffold artifacts from current machine-review evidence.
