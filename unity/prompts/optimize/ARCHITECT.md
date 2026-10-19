As configured primary, perform Optimize's existing fresh-run LeanArchitect setup
in this isolated private checkout. This is a phase turn, with no task ID; read
optimize_brief and use task_id="" for phase-scoped forum communication. Read
lean-toolchain; find an exactly matching
LeanArchitect ref. If absent, report a clean skip. Add/fix only that pinned
dependency, run lake update LeanArchitect and build. Revert only your dependency
edits and skip if it breaks the project; never reset unrelated files. Commit
successful lakefile/manifest changes. Do not change toolchain or Lean source.
Record the decision/evidence in the forum. Unity checks/publishes setup separately
before measuring the optimization baseline; setup is not an improvement.
Before returning, call report_phase_result with your author, a concrete summary
and retained evidence: status complete for successful setup, skipped for a
justified clean skip, or blocked for an unresolved required operation. The report
does not replace Unity's setup checks. Do not submit an optimization candidate.
