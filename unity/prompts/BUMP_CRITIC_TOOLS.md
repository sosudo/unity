# Bump critic tools

Read bump_status(), bump_brief(author), bump_task(task_id), and all pages of bump_requirements(offset?, limit?). For policy 2 also page bump_migration_plan: coverage includes every original occurrence, not just active repair groups. Inspect artifact_info/artifact_read and forum_read for exact retained evidence. publish_finding and report_obstacle may record justified review observations; forum_post is discussion, not a verdict.

Submit submit_formalization_verdict(author, verdict, summary, review, reopen_tasks?, evidence?) through native Forum MCP. verdict is "approved" or "lean_reopen". Do not pass representation_repairs or call refine_migration. Do not publish through a shell bridge. Missing native tools or an approval denial is a configuration blocker, not permission to change transports.

review has:
- snapshot_id: exact current snapshot.
- requirements: one entry per reviewed requirement, with requirement_id, status ("pass", "fail", "not_checked"), declarations, checked_anchor_ids, checked_prerequisite_ids, rationale, argument_rationale, repair_steps.
- scope_rationale: concrete original/target preservation and coverage rationale.
- repair_reviews: [] (Bump has no source-repair workflow).

Approval requires every requirement exactly once, status pass, every fixed output declaration listed (empty only for import-only modules), all its own anchor IDs and applicable prerequisite IDs checked, and substantive rationales. Do not use IDs from another requirement. pass and not_checked require repair_steps=[]; fail requires 1–8 nonblank actionable steps, each no more than 1000 characters. Rejection also names exact affected task IDs in reopen_tasks. Never substitute free-text evidence for structured review.

For migration policy 2, `declarations` must list the exact original occurrence IDs from each group's `binding.obligation_ids` in bump_migration_plan, including every original in a split/merge correspondence. Target names are not a substitute: several originals may map to the same target, or names may change. Inspect each mapping and its current target evidence, then report judgment against all preserved original IDs. An empty list is allowed only when that original module genuinely has no original declarations.

Do not claim compiled/native verification of uninspected modules, elimination of inherited holes, or human certification. A current passed machine snapshot is necessary but not sufficient for semantic approval.
