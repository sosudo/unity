Workspace write policy

Your assigned working directory is `{cwd}`.
Your permitted write locations are: {write_scope}.

Keep shell commands rooted in your assigned working directory. Set that absolute
directory explicitly when a shell tool accepts a working-directory argument.
Do not modify another checkout through absolute paths, `cd`, or symlinks.
Shared coordination and candidate integration must use Unity's MCP tools.
Supplied source documents remain read-only. Normal Unity/Lean tools may manage
their own runtime and build outputs; do not manually edit shared dependency sources.
Give any delegated subagent this same policy and assigned working directory.

If you accidentally modify a forbidden location, stop modifying it and report
the exact path through the Forum. Unity will preserve and recover confidently
attributed misplaced work; ambiguous incidents require intervention. Do not
attempt your own cleanup, staging, or commit in the forbidden checkout.
