# Optimize read-only Claude startup

Scoring and final review keep their exact source worktree read-only. Claude
starts in an empty, private `runner` directory under the existing short socket
scratch directory. The startup directory is outside the source tree. Mandatory
sandbox mount targets such as `.gitconfig` therefore do not enter the source
snapshot, including while score reports are submitted.

The shared Claude launcher accepts an optional execution-policy `sdk_cwd`.
This changes only `ClaudeAgentOptions.cwd`. Without that option, behavior is
unchanged. Logs, stop checks, completion callbacks, score-request bindings and
source identities continue to use the original source worktree. Optimize writer
turns and other backends do not request the override.

Read-only turns omit the source and its real descendants from writable roots.
The source deny, native edit denial and sandbox-bypass denial remain enabled.
No additional-directory permission is added. Native file tools use absolute
source paths. Source shell commands use `cd -- <source> && ...`. The shell bridge
still requires its cwd to be in the assigned worktree.

Local stdio MCP servers start through `unity.optimize_stdio_cwd`, which changes
to the original source worktree and sets `PWD` before replacing itself with the
configured server. Arguments, other environment values, standard streams, exit
codes and signals retain their existing meaning. HTTP servers are unchanged.
The adapter itself starts by absolute file path with Python's isolated mode;
the writable runner and `PYTHONPATH` cannot replace its imports. The configured
server still receives its original environment and arguments.
Process receipts record the real process cwd; the source remains separately
bound by `UNITY_OPTIMIZE_WORKTREE`.

Scratch cleanup still requires SDK and owned-process closure. A failed cleanup
retains scratch. No source placeholders, new source exclusions, permission
fallback, dependency update or separate backend loop is introduced.

## Offline regression coverage

The runner, shared default behavior, stdio adapter, source identities and cleanup
are tested without model calls. Linux Bubblewrap tests use generated Optimize
settings and reproduce the relevant missing-file mount sequence from the pinned
Claude CLI. They are component tests, not end-to-end Claude tests. They skip
explicitly when an already installed local Linux Bubblewrap is unavailable;
they do not install tools or start a VM. A skipped Linux test is not evidence
that Linux sandbox startup passed.
