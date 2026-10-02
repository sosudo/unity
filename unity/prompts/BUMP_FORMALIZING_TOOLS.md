# Bump worker tools

Use native Forum calls when available. Shell fallback uses the same phase-scoped server: unity mcp unity-forum TOOL '<JSON object>'. Use your bound author name.

- bump_brief(author), bump_status(), bump_task(task_id), bump_metrics(), bump_requirements(offset?, limit?): current inventory, compiler evidence, fixed outputs, dependencies, requirements and history. Page requirements until next_offset is null.
- bump_migration_plan(offset?, limit?): policy-2 original coverage, current diagnostic generation and explicit correspondences. Page the global ledger separately from active repairs; compiled/undiagnosed originals still require final review.
- refine_migration(author, task_id, expected_revision, subtasks, dependencies?, mapping_proposal?, reason?): policy-2 constrained diagnostic partition and optional complete correspondence proposal. Preserve all current original/diagnostic references and module dependencies. Requires your claimed strategy and fresh revision. Cannot change files, coverage, budgets, receipts or acceptance; correspondence adoption is controller-only.
- register_strategy(author, description, target?, strategy_family?), claim_strategy(strategy_id, author), assist_strategy, unclaim_strategy, mark_strategy_incorrect: coordinate a concrete approach for the assigned task; one owned claim at a time.
- publish_finding(author, kind, title, content, confidence, target?, strategy_id?, evidence?, supersedes?, declarations?, files?): share checked API/proof findings. confidence is an integer 0–100. Declared code files are captured as immutable artifacts, not automatically merged or accepted.
- read_finding(finding_id), artifact_info(artifact_id), artifact_read(artifact_id, offset?, limit?): inspect evidence; consume returned content and page until next_offset is null.
- report_obstacle(author, goal_state, target?, tried?, hypothesis?), ask_question(author, body, to?, target?), answer_question(question_id, author, body), forum_post, forum_read: exchange exact evidence. Discussion does not submit work.
- finalize_formalization(strategy_id, author, task_id, changed_paths?, notes?, supersedes?, stage="complete", outputs?): capture an immutable candidate. Pass exact original outputs from bump_task, not newly invented bindings. Only the assigned original module can change; no deletions/configuration changes. Empty outputs are valid only for inventoried import-only modules. The controller owns build, meaning/trust checks and merge.
- emit_formalization_candidate: compatibility submission of an existing exact commit; identical controller checks apply.
- sync_from_main(author): synchronize accepted progress without discarding private edits; inspect and resolve conflicts.
- yield_task(author, task_id, reason, waiting_for?): retain work and relinquish a blocked attempt; end the turn after yielding.

Never call tools not exposed in this phase. No source repair, generic rechunking, representation-only acceptance, or cross-task file reservations are available.
