# Available tools for `unity autoformalize` — Lean formalization

Start with `autoformalize_brief(author)` and refresh frequently for source identity, ready tasks, strategies,
findings and candidate events. These tools belong to the autoformalization runtime; there is no solving phase.

- `register_strategy(author, description, target?, strategy_family?)` registers a materially distinct
  approach for the formal task ID. Prefer a suitable existing strategy. Use `claim_strategy`,
  `assist_strategy`, `unclaim_strategy` or `mark_strategy_incorrect`; own one claimed strategy at a time.
- `publish_finding(author, kind, title, content, confidence, target?, strategy_id?, evidence?, supersedes?)`
  shares reusable checked APIs, proof patterns and concrete failures. Publish before substantial
  follow-on work, with the formal task ID and check/artifact evidence; reuse existing findings.
  `report_obstacle`, `ask_question` and `answer_question` expose blockers and requests for help.
- `finalize_formalization(strategy_id, author, task_id, changed_paths?, notes?, supersedes?)` commits the
  current exact worktree bytes and submits an immutable candidate for the authoritative main build and
  declaration review. Omit optional `changed_paths` to include all non-ignored project changes.
- `emit_formalization_candidate(strategy_id, author, task_id, commit_sha, notes?, supersedes?)` is the
  compatibility route for already-committed bytes; normally use `finalize_formalization`.
- `sync_from_main(author, reason?)` merges accepted main without discarding local work. Dirty trees and
  pending candidates block sync; conflicts remain for resolution and claims are retained. Do not sync
  just because an unrelated task merged.
- `request_rechunk(author, reason)` requests a corrected protected specification when the Lean encoding
  misrepresents the supplied source. Give precise source locations/evidence; the source is unchanged.
- `forum_post`, `forum_read`, `autoformalize_status`, `artifact_info` and bounded `artifact_read` provide detail.

Source-rewrite and informal-solving tools are not part of this pipeline. Report original-source defects
with evidence instead of modifying the input or silently formalizing a different statement.

For a backend without native MCP, use `unity mcp unity-forum <tool> '<json-args>'`. For multiline Lean or
quoted content, serialize the JSON argument object and pass a file/stdin rather than hand-quoting Lean:

```sh
unity mcp axle check --args-file /path/to/request.json
unity mcp axle check --args-file - < /path/to/request.json
```

Use positional JSON or `--args-file`, not both. This works for all MCP servers. Native MCP uses structured
arguments directly. The bridge stores server stderr separately; a diagnostics artifact alone does not
mean failure. Inspect actual result/exit status and retrieve diagnostics only as needed.

Use enabled compatible Axle tools preferentially over equivalent Lean LSP tools. Use Lean LSP for local
goals/project diagnostics. Consult the injected catalogs for actual tool schemas and Aristotle availability.
The shared `.lake/packages` cache is controller-owned: do not run `lake clean`, `lake update`, `lake upgrade`,
`lake exe cache` or bare `lake build`; do not bypass these restrictions with `lean_build`.

Use the supplied non-login shell environment. Only fall back to `unity capture -- lake env lean
Project/File.lean` when MCP cannot provide the needed check, or targeted `unity capture -- lake build
Project.Module` when compiled artifacts are needed. Preserve exit status, poll a running check's same
session, and do not start duplicate checks for empty output. Finalize promptly after a successful targeted
local check of the exact current source; do not repeat checks against unchanged bytes.
