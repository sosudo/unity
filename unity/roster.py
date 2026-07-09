"""Parse `agents.yaml` into a validated roster of agents to spawn."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

BACKENDS = {"claude_code", "codex"}
_VAR = re.compile(r"\$\{(\w+)\}|\$(\w+)")


@dataclass
class Agent:
    name: str
    model: str
    provider: str
    backend: str  # "claude_code" | "codex"
    strength: float  # autostrength (learned per model) unless overridden in agents.yaml
    base_url: str | None
    api_key: str | None
    auth_token: str | None
    budget: float | None  # USD per instance; None = unlimited
    is_primary: bool


@dataclass
class Roster:
    agents: list[Agent]

    @property
    def primary(self) -> Agent:
        return self.agents[0]


def _interp(value, where: str):
    """Resolve ${VAR}/$VAR against the environment; missing var is an error."""
    if not isinstance(value, str):
        return value

    def sub(m):
        name = m.group(1) or m.group(2)
        if name not in os.environ:
            raise ValueError(f"{where}: undefined environment variable ${name}")
        return os.environ[name]

    return _VAR.sub(sub, value)


def load_roster(path: Path) -> Roster:
    """Load and validate `agents.yaml`, flattening groups into per-instance agents."""
    if not path.is_file():
        raise ValueError(f"agents.yaml not found at {path}")

    doc = yaml.safe_load(path.read_text()) or {}
    groups = doc.get("agents")
    if not groups:
        raise ValueError("agents.yaml: 'agents' is empty or missing")

    agents: list[Agent] = []
    for i, g in enumerate(groups):
        where = f"agents.yaml: group #{i + 1}"
        names = g.get("names")
        if not names or not isinstance(names, list):
            raise ValueError(f"{where}: 'names' must be a non-empty list")

        instances = g.get("instances", len(names))
        if instances != len(names):
            raise ValueError(f"{where}: 'instances' ({instances}) != len(names) ({len(names)})")

        backend = g.get("backend")
        if backend not in BACKENDS:
            raise ValueError(f"{where}: 'backend' must be one of {sorted(BACKENDS)}")

        model = g.get("model")
        if not model:
            raise ValueError(f"{where}: 'model' is required")

        api_key = _interp(g.get("api_key"), where)
        base_url = _interp(g.get("base_url"), where)
        auth_token = _interp(g.get("auth_token"), where)

        # codex authenticates via login_api_key/CODEX_API_KEY; auth_token is not used there.
        if backend == "codex" and not api_key:
            raise ValueError(f"{where}: codex backend requires 'api_key'")

        budget = g.get("budget")
        budget = float(budget) if budget not in (None, "") else None

        # Autostrength: learned per-model (EMA of forum credit across runs); an explicit
        # strength in agents.yaml overrides.
        raw_strength = g.get("strength")
        if raw_strength in (None, ""):
            from .library import learned_strength
            strength = learned_strength(model)
        else:
            strength = float(raw_strength)

        for name in names:
            agents.append(Agent(
                name=name,
                model=model,
                provider=g.get("provider", ""),
                backend=backend,
                strength=strength,
                base_url=base_url,
                api_key=api_key,
                auth_token=auth_token,
                budget=budget,
                is_primary=(len(agents) == 0),
            ))

    return Roster(agents=agents)
