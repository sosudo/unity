# Targeted representation review tools

Use `artifact_info` / bounded `artifact_read` for the assigned immutable review artifact.
`solve_task(task_id)` provides current context, but submit only for the assigned input hash.
`solve_requirements` retrieves original source anchors and requirements; `read_finding`
retrieves advisory evidence. Findings do not override exact source or Lean definitions.

Call `submit_representation_review(author, task_id, review)` with:
`{"input_sha256":"<assigned hash>","verdict":"aligned|encoding_error|source_issue|uncertain",
"checked_anchor_ids":["<every assigned anchor ID>"],"rationale":"specific correspondence or defect",
"evidence":"exact source passages and Lean definitions/API evidence"}`.

This tool neither submits proof candidates nor grants final faithfulness approval. A stale response
means the representation changed; do not approve the new encoding without inspecting its new input.
