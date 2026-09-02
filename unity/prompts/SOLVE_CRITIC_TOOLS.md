# Available coordination tools for `unity solve` — critic profile

The critic audits the complete formalization against the exact accepted solution revision. Start with
`solve_brief(author)` and `solve_status()`. Inspect immutable candidate, build, verification, and source
artifacts with `artifact_info` and bounded `artifact_read`.
The server infers the active gate; none of these tools accepts a model-selected stage.
The brief header gives the original problem hash, both gate revisions, accepted candidate IDs, and the
full `integrated_main_sha`. Review that exact main commit; abbreviated or inferred revisions are not
acceptable critic identities.

Use `forum_post` or `forum_read` only for concrete clarification. Forum discussion and model-reported
build status are not authoritative evidence.

- `submit_formalization_verdict(author, verdict, summary, reviewed_main_sha, evidence?, reopen_tasks?,
  source_fix?)` submits the structured critic decision. `reviewed_main_sha` is the full 40-character
  main commit copied from the brief after you inspect it. The tool rejects the verdict if main moved or
  the closed formalization gate records a different commit. `verdict` is `approved`, `lean_reopen`,
  `source_fix`, or `reopen_solving`.

`approved` requires a clean project build, no forbidden proof bypass, all intended declarations proved,
and faithful correspondence to the accepted natural-language solution. `lean_reopen` names exact Lean
or integration defects and the formal tasks to reopen. `source_fix` identifies a local defect in the
informal paper; `reopen_solving` is reserved for a substantive mathematical error or gap. Both require
evidence precise enough for the next worker to act on.

Bind every conclusion to exact artifacts, commits, declarations, or source locations. Do not edit Lean
or the paper during independent criticism. The runtime consumes the verdict to close formalization,
create repair work, or return to solving.
