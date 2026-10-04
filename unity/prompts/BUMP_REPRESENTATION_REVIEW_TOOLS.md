# Targeted representation review tools

Use `artifact_info` / bounded `artifact_read` for the assigned immutable review artifact.
`bump_task(task_id)` provides current context, but submit only for the assigned input hash.
`bump_requirements` retrieves original source anchors and requirements; `read_finding`
retrieves advisory evidence. Findings do not override exact source or Lean definitions.

Call `submit_representation_review(author, task_id, review)` with:
`{"input_sha256":"<assigned hash>","verdict":"aligned|encoding_error|source_issue|uncertain",
"checked_anchor_ids":["<every assigned anchor ID>"],"rationale":"specific correspondence or defect",
"evidence":"exact source passages and Lean definitions/API evidence"}`.

This tool neither submits proof candidates nor grants final faithfulness approval. A stale response
means the representation changed; do not approve the new encoding without inspecting its new input.
Check against the immutable original Lean declaration and explicit correspondence, not a replacement
natural-language claim. No new holes or expanded per-declaration trust are allowed, including in a
representation stage. Final all-selected native comparison and independent critic review remain required.
