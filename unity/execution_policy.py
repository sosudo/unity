"""Opt-in execution options for the shared backend loops.

No caller gets a policy implicitly. Pipeline policy owns permissions, local
configuration and process receipts; this module does not select providers,
change retries, or run another backend loop.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class RuntimeHooks(Protocol):
    """Lifecycle and telemetry hooks used by the common execution loops."""
    _last_run_stats: dict
    _CODEX_INTERRUPT_REQUEST_TIMEOUT: float
    _CODEX_INTERRUPT_DRAIN_TIMEOUT: float
    _CODEX_CLOSE_TIMEOUT: float

    def _agent_env(self, agent, codex_home=None, env_overrides=None) -> dict: ...
    def _stop_requested(self, cwd) -> bool: ...
    def _process_group_wrapper(self, executable: Path, directory: Path, label: str) -> tuple[Path, Path]: ...
    async def _terminate_process_group(self, pid_file, **kwargs) -> None: ...
    def _idle_guard(self, iterator, timeout): ...
    def _log(self, name, message, cwd=None) -> None: ...
    def _give_up(self, error, attempt: int) -> bool: ...
    def _retry_sleep(self, error) -> float: ...
    def _write_codex_agents(self, home, subagents) -> None: ...
    def _codex_notifications(self, handle, codex, idle_timeout, stream_started, pid_file=None): ...
    async def _cleanup_codex_attempt(self, codex, interrupt_task, pid_file) -> None: ...
    async def _drain_spawn_cleanup(self, cleanup) -> None: ...
    async def _completion_feedback(self, callback, final, cwd): ...
    def _write_run_log(self, agent, cwd, seconds, log_context=None) -> None: ...
    def _sum_usage(self, previous, current): ...
    def _emit_delta(self, name, text): ...
    def _flush_delta(self, name): ...
    def _tool_log(self, cwd, name, tool, detail=""): ...


@dataclass(frozen=True)
class ExecutionPolicy:
    """Explicit adapters around one shared set of Claude/Codex/AG loops.

    Preparation hooks return backend option dictionaries, not running workers.
    Claude preparation may return `sdk_cwd` for a separate SDK startup directory.
    It defaults to the caller cwd and does not change lifecycle/source bindings.
    `on_normal_completion` preserves an existing opt-in continuation API; normal
    pipeline callers and the Optimize controller do not enable it implicitly.
    """
    hooks: RuntimeHooks
    claude_options: Callable[..., dict[str, Any]]
    finish_claude: Callable[..., None]
    codex_options: Callable[..., dict[str, Any]]
    antigravity_options: Callable[..., dict[str, Any]]
    callback_error: type[Exception]
    unsuccessful_error: type[Exception]
    cleanup_error: type[Exception]
    on_normal_completion: Callable[[str | None], Awaitable[str | None]] | None = None
    started_at: float | None = None
