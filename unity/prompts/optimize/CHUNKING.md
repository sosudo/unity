Collaboratively plan Optimize work through forum plan proposals. This is a phase
turn, not a task: read optimize_brief, do not invent an optimize_task ID, and use
task_id="" for findings/questions/obstacles. Read optimize_inventory with an exact
project-relative path or declaration-name filter, paging with next_offset and
retaining inventory_sha256. Use the returned canonical chunk IDs, not fabricated
IDs derived from declaration names. Read full retained records through
artifact_read when the brief supplies references.
Enumerate every declaration in requested targets, including clean declarations. Default one declaration per chunk; document evidence for mutual/overlap/file-level or metric-specific grouping and retain covered declaration IDs. Record dependencies; group/report cycles rather than ignoring them. Inventory is a scheduling hint, not a semantic parser.
Record original measurements with the agreed method and source identity using the forum measurement tools. Raw value and improvement differ. Baseline is immutable after planning; future scores are separate. Use measured only for actual retained tool evidence, otherwise agent_judged. No invented aggregate or LOC substitute. Submit proposals instead of editing dag.json. Do not edit Lean source. Omitted declarations need explicit out-of-scope reasons; Universe covers all eligible declarations.

For a narrow file target, propose_chunk_plan may specify exclude_outside_paths
as the list of exact known selected paths, together with exclusion_reason and
the current inventory_sha256. The controller expands this selector to explicit
per-declaration exclusions outside those paths before existing validation. For
example, exclude_outside_paths=["Batteries/Data/List/Perm.lean"] keeps that file
selected; it does not exclude that file. No natural-language scope is silently
parsed, no omission implies exclusion, and every selected declaration must still
be covered. Universe forbids this selector. Explicit declaration exclusions are
still supported. The full configured build scope is unchanged. Store inventory
exports, plan drafts, tool-argument JSON and temporary planning helpers only in
the exact private directory supplied as UNITY_OPTIMIZE_SCRATCH, never the source
root or an ad hoc .unity-tmp/ directory. For large plan arguments, use
`unity mcp unity-forum propose_chunk_plan --args-file "$UNITY_OPTIMIZE_SCRATCH/plan.json"`
if needed. This administrative scratch location does not authorize Lean source edits.

Collaborate and revise plans through the forum. Submit the complete converged plan last: the controller validates and installs that final proposal, retaining earlier drafts as discussion rather than requiring identical historical proposals or an extra primary planning approval.
Before returning, call report_phase_result with your author, status complete or
blocked, a concrete summary and retained plan/measurement evidence. Report
validation failures concretely, using returned missing/unknown IDs and inventory
pages to correct the proposal. A complete report does not substitute for an
accepted plan and required original measurements. Do not submit candidates here.
