"""Prove-only MCP bindings for the checkout used by an individual agent."""

from pathlib import Path


def for_run(servers: dict, paths) -> dict:
    """Expose only external proving services, without coordination tools."""
    configured = {name: spec for name, spec in servers.items()
                  if name not in {"unity-forum", "forum", "unity-prove"}}
    return for_checkout(configured, paths.project_root)


def for_checkout(servers: dict, checkout: Path) -> dict:
    """Bind fresh Lean LSP sessions without rebinding shared proof controls."""
    if "lean-lsp" not in servers:
        return servers
    spec = servers["lean-lsp"]
    return {
        **servers,
        "lean-lsp": {
            **spec,
            "env": {
                **spec.get("env", {}),
                "LEAN_PROJECT_PATH": str(Path(checkout).resolve()),
            },
        },
    }
