"""Prove-only MCP bindings for the checkout used by an individual agent."""

from pathlib import Path


def for_checkout(servers: dict, checkout: Path) -> dict:
    """Bind fresh Lean LSP sessions without rebinding the shared Forum server."""
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
