# Optimize tools and authority

Read optimize_status and optimize_brief for the current phase, source, metric,
baseline, round, dependencies, competing candidates and review. Status, brief,
task details and candidate acknowledgments are bounded summaries, not the full
verification evidence. A candidate's records.candidate points to its complete
record; records.tasks/candidates/rounds/review retain the full collections. If a
response says summary_only, read records.response for the complete summary index
and its evidence references. Use artifact_read and follow next_offset for all
required pages, including during primary selection and final review. Artifact
hashes identify the retained snapshot; summaries do not replace source/revision
checks or establish acceptance. Use optimize_inventory to page canonical
declaration IDs, filtering by exact project-relative path or declaration name as
needed, and retain the returned inventory_sha256 for a plan submission.

Call optimize_task only when the controller supplies a real bound task ID.
Architect, exploration, chunking, selection, measurement refresh and review are
phase turns, not invented task IDs. For phase-scoped findings, questions and
obstacles, pass task_id=""; the forum binds them to your current phase/dispatch.
Only an actual optimization assignment submits a candidate. Follow this turn's
instructions and exposed tools, never another pipeline's state or direct JSON
edits. A missing phase-specific tool is not permission to bypass its restriction.

If native tools are unavailable for this backend, the shell bridge is
`unity mcp unity-forum <tool> '<json-args>'` (server alias `forum`, not
`unity_forum` or `optimize`). For non-review turns, put inventory exports, plan
drafts, tool-argument JSON and temporary planning helpers in the exact private
directory supplied as `UNITY_OPTIMIZE_SCRATCH`. Large arguments can use
`unity mcp unity-forum <tool> --args-file "$UNITY_OPTIMIZE_SCRATCH/plan.json"`
instead of positional JSON. Do not create administrative files at the source
root or in an ad hoc `.unity-tmp/` directory. Actual source/proof edits remain
subject to the assigned phase's permissions; this scratch directory does not
authorize source edits in read-only planning phases. Review remains read-only:
use native tools or inline JSON arguments, without writing planning/scratch files.
Large output includes an artifact reference; read every required page with
artifact_read and next_offset rather than guessing omitted IDs or values.

All agents optimize; only the configured primary selects winners/breaks ties and
submits final review, even if it authored candidates. No reserved reviewer.
When the assigned turn permits source edits, commit in your assigned private tree.
Primary records the choice; Unity checks
and publishes. Never merge/reset main, alter shared dependencies, overwrite
baseline or redefine a metric to improve scores. Review is read-only; fixes go
through normal candidates. Stale bases require new checks.
Use configured external tools only, never fabricate outputs or expose credentials.
