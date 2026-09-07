## Lean LSP — inspect and drive the local Lean project

Use MCP for normal proof development: inspect goals, search APIs, and check proof edits with the
tools below. When enabled Axle offers an equivalent check in a compatible environment, prefer Axle;
use Lean LSP for local goals, editor state, project-only dependencies, or unsupported external checks.
Direct `lake env lean` is a fallback, not the default proof-development loop.

Use native MCP when exposed; otherwise call
`unity mcp lean-lsp <tool> '<json-args>'` with the tool's actual schema. This shell transport still
calls MCP; it does not mean bypassing MCP with local compiler commands.

- `lean_goal` — inspect proof goals at a source position.
- `lean_term_goal` — inspect the expected type at a position.
- `lean_diagnostic_messages` — retrieve compiler diagnostics for a local file.
- `lean_file_outline` — inspect imports and declarations with their signatures.
- `lean_hover_info`, `lean_completions`, `lean_declaration_file`, and `lean_references` — inspect
  symbols and APIs.
- `lean_local_search` — verify that a declaration exists in this project's installed libraries.
- `lean_leansearch`, `lean_loogle`, and `lean_leanfinder` — search Mathlib by language, type, or
  mathematical meaning.
- `lean_state_search` and `lean_hammer_premise` — search from the current goal state.
- `lean_code_actions` and `lean_multi_attempt` — inspect suggestions and test multiple tactics.
- `lean_run_code` — compile a self-contained snippet with explicit imports.
- `lean_verify` — inspect theorem axioms and scan source for prohibited proof shortcuts.
- `lean_minimal_hypotheses`, `lean_profile_proof`, `lean_get_widgets`, and
  `lean_get_widget_source` — specialized proof inspection tools.
- `lean_build` — project build/LSP restart capability; not a routine diagnostic. Autoformalize workers
  must not use it to initiate project-wide builds: Unity owns the authoritative full main build.

Keep the controller-owned shared `.lake/packages` cache intact. Do not run `lake clean`, `lake update`,
`lake upgrade`, `lake exe cache`, or a worker project-wide build through either MCP or shell.
Use a targeted `lake build <target>` only when compiled local artifacts are needed; otherwise use
MCP diagnostics or the permitted local fallback. Reuse successful checks of unchanged source.

Respect the current phase: chunking may inspect APIs and validate statement scaffolds, not complete
proofs; critics remain read-only and reuse current verification records unless a concrete concern
requires another check. External success never replaces validation in the actual local project.
