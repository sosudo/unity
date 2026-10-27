# Compiler-driven Bump forum tools

Read `bump_brief()`, `bump_status()`, and `bump_task(task_id)` first. Status includes current configuration, tasks, candidates and the exact review snapshot. Read the supplied project/evidence files for details; bounded summaries are not complete source audits.

Coordinate through `forum_post` and `forum_read`, `publish_finding(author, task_id, finding, evidence)`, `report_obstacle(author, task_id, obstacle, evidence)`, `ask_question` and `answer_question`.

Preparing/repairing agents also use:
- `register_strategy(author, task_id, title, description)`, `claim_strategy`, `assist_strategy`, and `yield_task`.
- `request_repair_scope(author, task_id, chunk_ids, files, reason, expected_revision)`: request necessary declaration/module/API grouping and file leases before editing.
- `request_configuration_change(author, task_id, files, reason, proposed_changes, modules?, lake_args?, project_scope?, module_renames?)`: request controller-owned serialized configuration work when you lack a lease for the files that need changing, or request additional module coverage, a module rename, or Lake options. Optional modules add supplemental coverage; for a source move supply module_renames={old_module: new_module} and lease both source paths, so the controller can verify and transfer coverage; they cannot silently drop existing coverage. Supply consistent `-K` Lake arguments when needed. A scope-only request may use `files=[]`; the controller still owns the transaction. If your assigned configuration task leases the relevant config files, edit them in your private worktree and submit the candidate with `submit_repair`; a configuration request is not a substitute for submitting leased-file edits. The controller performs shared `lake update`, validation, builds, and integration. Never edit shared configuration directly or run shared dependency updates yourself.
- `sync_from_main(author, task_id)`: refresh only when the controller permits the current task to synchronize.
- `submit_repair(author, task_id, notes)`: submit useful, checked partial work; end your turn after submission so the owned session can close and integration can begin.

All phases can use `record_workflow_change(author, original, problem, evidence, revised, result)`. Distinguish observed results from proposals.

Only the configured primary during the critic phase has `submit_migration_verdict(author, snapshot_id, approved, reasoning, reopen_task_ids)` and `request_review_repair(author, snapshot_id, chunk_ids, files, reason)`. The latter creates precise controller-owned repair tasks for previously clean declarations or configuration; reject with its returned task IDs in `reopen_task_ids`. It grants no source-edit lease or snapshot mutation. All configured agents participate in repairs, including the primary; prior contributions do not disqualify the primary from final review. Critic is a temporary read-only phase, not a separate roster role. Approval requires the identical current passed snapshot with no queued issues. Final-report persistence and acceptance transitions belong to the controller, not workers.
