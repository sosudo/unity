# Recursive Unity

A `recursive-unity` subagent is available to you. It spawns an independent child `unity` pipeline run in its own isolated context window.

## When to use it

Use `recursive-unity` when a subtask is too large or complex for a single-context pass — for example, a cluster of related declarations that would benefit from a full exploration → semiformalization → formalization cycle of their own, or an external result that needs to be proved independently before it can be used here.

Whether to delegate to `recursive-unity` vs. handle the subtask with a regular subagent is your judgment call. Stronger models may rarely need it; weaker models may elect to use it more often. There is no obligation to recurse.

## What it does

`recursive-unity` constructs and executes a `unity` command with appropriate flags and an isolated `--output-dir`. After the child run completes, it reads the results and reports them back to you. You can then use those results (e.g., formalized Lean files, `gathered/` content) in the current phase.

## Depth limit

Child runs are capped at a lower depth than this run. If the child depth reaches 0, the child pipeline has no further recursive capability. You will be told the current depth when `recursive-unity` is registered.

**IMPORTANT: Do not use pkill, killall, or any kill command targeting the unity-agent or claude process. Do not attempt to kill the pipeline or any parent process.**
