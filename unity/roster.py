"""Parse `agents.yaml` into a validated roster of agents to spawn."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

BACKENDS = {"claude_code", "codex"}
# user-facing aliases: the yaml (and web UI) may say which API an agent speaks
_BACKEND_ALIASES = {"anthropic": "claude_code", "openai": "codex",
                    "claude_code": "claude_code", "codex": "codex"}
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
        return next((a for a in self.agents if a.is_primary), self.agents[0])


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

    # The primary group: explicit `primary: true` flag, else the first group.
    primary_idx = next((i for i, g in enumerate(groups) if g.get("primary")), 0)

    agents: list[Agent] = []
    for i, g in enumerate(groups):
        where = f"agents.yaml: group #{i + 1}"
        # `name: X` (one agent per entry, the default) or legacy `names: [X, Y]`
        names = g.get("names") or ([g["name"]] if g.get("name") else None)
        if not names or not isinstance(names, list):
            raise ValueError(f"{where}: 'name' (or 'names') is required")

        backend = _BACKEND_ALIASES.get(str(g.get("backend", "")).lower())
        if backend not in BACKENDS:
            raise ValueError(f"{where}: 'backend' must be one of "
                             f"{sorted(BACKENDS)} (or aliases 'anthropic'/'openai')")

        model = g.get("model")
        if not model:
            raise ValueError(f"{where}: 'model' is required")

        api_key = _interp(g.get("api_key"), where)
        base_url = _interp(g.get("base_url"), where)
        auth_token = _interp(g.get("auth_token"), where)

        # codex: custom providers need api_key; without one, the agent rides the
        # user's ChatGPT/Codex subscription login (~/.codex/auth.json).
        if backend == "codex" and g.get("base_url") and not api_key:
            raise ValueError(f"{where}: codex with a custom base_url requires 'api_key'")

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

        for j, name in enumerate(names):
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
                is_primary=(i == primary_idx and j == 0),
            ))

    return Roster(agents=agents)
