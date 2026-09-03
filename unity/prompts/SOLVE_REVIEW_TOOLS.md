# Available tools for `unity solve` — solution review

Start with `solve_brief(author)`. Review only the candidate artifact and SHA-256 recorded there.

- `artifact_info(artifact_id)` and `artifact_read(artifact_id, offset?, limit?)` inspect exact bytes.
- `review_solution_candidate(candidate_id, author, verdict, review, evidence?, issues?)` records one
  independent `approve` or `object` verdict. On objection, `issues` may contain objects with `kind`,
  `description`, and optional `component_ids`; each becomes a repair task automatically.
- `publish_finding(...)` records a reusable fact discovered during review.
- `ask_question`, `answer_question`, `forum_post`, and `forum_read` support focused clarification.
- `solve_status()` returns exact state when the brief is insufficient.

Reviewers cannot mutate strategies, formal tasks, or candidate bytes.
