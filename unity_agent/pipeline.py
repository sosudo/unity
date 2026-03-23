"""Main autoformalization pipeline for Unity Agent."""

import os
import sys
import json
import logging
import subprocess
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from dotenv import load_dotenv
from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, ResultMessage, AgentDefinition, TaskStartedMessage, TaskProgressMessage, TaskNotificationMessage

_console: Console | None = None


def _log_agent_message(message) -> None:
    """Log an agent SDK message with rich formatting."""
    if isinstance(message, TaskStartedMessage):
        _console.print(f"[bold green]↳ Agent spawned[/bold green]  [dim]{message.task_id}[/dim] — {message.description!r}")
    elif isinstance(message, TaskProgressMessage):
        last = getattr(message, "last_tool_name", None)
        tool = f"  [cyan]{last}[/cyan]" if last else ""
        _console.print(f"[blue]→[/blue] Progress  [dim]{message.task_id}[/dim]{tool}")
    elif isinstance(message, TaskNotificationMessage):
        color = {"completed": "green", "failed": "red", "stopped": "yellow"}.get(message.status, "white")
        summary = getattr(message, "summary", None)
        suffix = f" — {summary!r}" if summary else ""
        _console.print(f"[bold {color}]✓ Agent {message.status}[/bold {color}]  [dim]{message.task_id}[/dim]{suffix}")
    elif isinstance(message, AssistantMessage):
        agent = f"[dim]\\[{message.parent_tool_use_id[:8]}][/dim] " if message.parent_tool_use_id else ""
        for block in message.content:
            if hasattr(block, "thinking"):
                _console.print(f"{agent}[dim italic]💭 {block.thinking[:300]}[/dim italic]")
            elif hasattr(block, "text") and block.text.strip():
                _console.print(f"{agent}{block.text[:500]}")
            elif hasattr(block, "name") and hasattr(block, "input"):
                _console.print(f"{agent}[cyan]⚙  {block.name}[/cyan]({str(block.input)[:200]})")
    elif isinstance(message, ResultMessage):
        _console.print(f"[bold green]✓ Complete[/bold green] — {message.num_turns} turns, [yellow]${message.total_cost_usd:.4f}[/yellow], stop={message.stop_reason}")


def _run(cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True)


def _is_lean_repo(path: Path) -> bool:
    return (path / "lean-toolchain").exists() and (
        (path / "lakefile.lean").exists() or (path / "lakefile.toml").exists()
    )


def _get_prompts_dir() -> Path:
    """Get the PROMPTS directory relative to this package."""
    return Path(__file__).parent / "PROMPTS"


def _get_teams_dir() -> Path:
    """Get the TEAMS directory relative to this package."""
    return Path(__file__).parent / "TEAMS"


def _get_subagents_dir() -> Path:
    """Get the SUBAGENTS directory relative to this package."""
    return Path(__file__).parent / "SUBAGENTS"


def _get_default_prompts_dir() -> Path:
    return Path(__file__).parent / "DEFAULT_PROMPTS"


def _get_default_subagents_dir() -> Path:
    return Path(__file__).parent / "DEFAULT_SUBAGENTS"


def _get_default_teams_dir() -> Path:
    return Path(__file__).parent / "DEFAULT_TEAMS"


def _get_library_dir() -> Path:
    return Path.home() / ".unity" / "library"


def _get_project_notes_dir() -> Path:
    return Path.cwd() / ".unity"


def _init_library() -> None:
    """Create global library and project notes directories if they don't exist."""
    lib = _get_library_dir()
    for subdir in ("tactics", "lemmas", "ir-patterns", "subagents"):
        (lib / subdir).mkdir(parents=True, exist_ok=True)
    _get_project_notes_dir().mkdir(exist_ok=True)


def _load_library_context() -> str:
    """Read all library files and project notes into an injectable string.

    Returns an empty string if the library and project notes are all empty.
    """
    sections: list[str] = []

    lib = _get_library_dir()
    for subdir in ("tactics", "lemmas", "ir-patterns"):
        subdir_path = lib / subdir
        for md_file in sorted(subdir_path.glob("*.md")):
            text = md_file.read_text().strip()
            if text:
                label = f"*{subdir.capitalize()} — {md_file.stem}*"
                sections.append(f"{label}\n\n{text}")

    notes_dir = _get_project_notes_dir()
    for note_file in sorted(notes_dir.glob("*.md")):
        text = note_file.read_text().strip()
        if text:
            label = f"*Project notes — {note_file.stem}*"
            sections.append(f"{label}\n\n{text}")

    if not sections:
        return ""
    return "**Library Context**\n\n" + "\n\n---\n\n".join(sections)


# Populated at run_pipeline startup; available to all query() agents= dicts.
LIBRARY_SUBAGENTS: dict = {}


def _load_library_subagents() -> dict:
    """Parse ~/.unity/library/subagents/*.md into AgentDefinition objects."""
    import re
    result = {}
    subagents_dir = _get_library_dir() / "subagents"
    for md_file in sorted(subagents_dir.glob("*.md")):
        raw = md_file.read_text()
        # Parse YAML-lite frontmatter between --- delimiters
        fm_match = re.match(r"^---\n(.*?)\n---\n(.*)", raw, re.DOTALL)
        if not fm_match:
            continue
        fm_text, body = fm_match.group(1), fm_match.group(2).strip()
        meta: dict = {}
        for line in fm_text.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        name = meta.get("name", md_file.stem)
        description = meta.get("description", "")
        tools_raw = meta.get("tools", "Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch,Agent,Skill")
        tools = [t.strip() for t in tools_raw.split(",")]
        result[name] = AgentDefinition(description=description, prompt=body, tools=tools)
    return result


async def _infer_flags() -> tuple[str | None, str | None, bool]:
    """Run a lightweight inference agent to detect source, project, and prove from CWD."""
    cwd_env = Path.cwd() / ".env"
    package_env = Path(__file__).parent.parent / ".env"
    load_dotenv(dotenv_path=cwd_env)
    load_dotenv(dotenv_path=package_env, override=False)

    infer_file = Path(".unity_infer.json")
    infer_file.unlink(missing_ok=True)

    with open(_get_prompts_dir() / "INFERENCE.md") as f:
        infer_prompt = f.read()

    async for _ in query(
        prompt="Infer the source, project, and prove flag for the current working directory.",
        options=ClaudeAgentOptions(
            tools=["Read", "Glob", "Grep", "Write", "Bash"],
            allowed_tools=["Read", "Glob", "Grep", "Write", "Bash"],
            agents={},
            system_prompt=infer_prompt,
            permission_mode="bypassPermissions",
            continue_conversation=False,
            disallowed_tools=[],
            model="sonnet",
            fallback_model="haiku",
            env={
                "ANTHROPIC_BASE_URL": os.getenv("ANTHROPIC_BASE_URL"),
                "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY"),
                "ANTHROPIC_AUTH_TOKEN": os.getenv("ANTHROPIC_AUTH_TOKEN"),
                "ANTHROPIC_DEFAULT_OPUS_MODEL": os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                "ANTHROPIC_DEFAULT_SONNET_MODEL": os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
            },
            extra_args={},
        ),
    ):
        pass

    if not infer_file.exists():
        return None, None, False
    try:
        data = json.loads(infer_file.read_text())
        infer_file.unlink(missing_ok=True)
        return data.get("source"), data.get("project"), bool(data.get("prove", False))
    except Exception:
        infer_file.unlink(missing_ok=True)
        return None, None, False


async def run_pipeline(source: str | None, project_dir: str, context: bool, prove: bool = False):
    """Run the full autoformalization pipeline."""
    global _console
    PROJECT_DIR = project_dir

    # Helper functions for environment variable parsing
    def parse_bool(val: str | None) -> bool:
        """Parse string env var to boolean."""
        return val is not None and val.lower() in ("true", "1", "yes")
    
    def parse_float(val: str | None) -> float | None:
        """Parse string env var to float, or None if not set/empty."""
        if not val or val.lower() == "none":
            return None
        return float(val)

    def parse_int(val: str | None) -> int | None:
        """Parse string env var to int, or None if not set/empty."""
        if not val or val.lower() == "none":
            return None
        return int(val)

    # Load environment
    try:
        cwd_env = Path.cwd() / ".env"
        package_env = Path(__file__).parent.parent / ".env"
        load_dotenv(dotenv_path=cwd_env)
        load_dotenv(dotenv_path=package_env, override=False)
        silent = parse_bool(os.getenv("SILENT"))
        recording = parse_bool(os.getenv("RECORDING"))

        # Reroute output if needed
        if silent:
            sys.stdout = open("unity.out", 'w')
            sys.stderr = open("unity.err", 'w')
        elif recording:
            class _Tee:
                def __init__(self, stream, path):
                    self._file = open(path, 'w')
                    self._stream = stream
                def write(self, data):
                    self._stream.write(data)
                    self._file.write(data)
                def flush(self):
                    self._stream.flush()
                    self._file.flush()
                def isatty(self):
                    return hasattr(self._stream, "isatty") and self._stream.isatty()
            sys.stdout = _Tee(sys.stdout, "unity.out")
            sys.stderr = _Tee(sys.stderr, "unity.err")

        _console = Console(file=sys.stdout)
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(message)s",
            datefmt="[%X]",
            handlers=[RichHandler(console=_console, rich_tracebacks=True, show_path=False)],
            force=True,
        )

        logging.info("Loading environment...")
        
        # Set environment
        save_spec = parse_bool(os.getenv("SAVE_SPECIFICATION"))
        no_bypass = parse_bool(os.getenv("NO_BYPASS"))
        generation_budget = parse_float(os.getenv("GENERATION_BUDGET"))
        semiformalization_budget = parse_float(os.getenv("SEMIFORMALIZATION_BUDGET"))
        exploration_budget = parse_float(os.getenv("EXPLORATION_BUDGET"))
        preparation_budget = parse_float(os.getenv("PREPARATION_BUDGET"))
        formalization_budget = parse_float(os.getenv("FORMALIZATION_BUDGET"))
        critic_budget = parse_float(os.getenv("CRITIC_BUDGET"))
        save_semiformalization = parse_bool(os.getenv("SAVE_SEMIFORMALIZATION"))
        autofix = parse_bool(os.getenv("AUTOFIX"))
        exploration = parse_bool(os.getenv("EXPLORATION"))
        recurse = parse_bool(os.getenv("RECURSE"))
        max_critic_iterations = parse_int(os.getenv("MAX_CRITIC_ITERATIONS"))
        anthropic_base_url = os.getenv("ANTHROPIC_BASE_URL")
        anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
        anthropic_auth_token = os.getenv("ANTHROPIC_AUTH_TOKEN")
        anthropic_default_opus_model = os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL")
        anthropic_default_sonnet_model = os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL")
        anthropic_default_haiku_model = os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL")
        claude_code_experimental_agent_teams = os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
        
        # Print environment
        logging.info("Environment:")
        logging.info(f"SAVE_SPECIFICTION: {save_spec}")
        logging.info(f"NO_BYPASS: {no_bypass}")
        logging.info(f"GENERATION_BUDGET: {generation_budget}")
        logging.info(f"SEMIFORMALIZATION_BUDGET: {semiformalization_budget}")
        logging.info(f"EXPLORATION_BUDGET: {exploration_budget}")
        logging.info(f"PREPARATION_BUDGET: {preparation_budget}")
        logging.info(f"FORMALIZATION_BUDGET: {formalization_budget}")
        logging.info(f"CRITIC_BUDGET: {critic_budget}")
        logging.info(f"SILENT: {silent}")
        logging.info(f"RECORDING: {recording}")
        logging.info(f"SAVE_SEMIFORMALIZATION: {save_semiformalization}")
        logging.info(f"AUTOFIX: {autofix}")
        logging.info(f"EXPLORATION: {exploration}")
        logging.info(f"RECURSE: {recurse}")
        logging.info(f"ANTHROPIC_BASE_URL: {anthropic_base_url}")
        logging.info(f"ANTHROPIC_API_KEY: {anthropic_api_key}")
        logging.info(f"ANTHROPIC_AUTH_TOKEN: {anthropic_auth_token}")
        logging.info(f"ANTHROPIC_DEFAULT_OPUS_MODEL: {anthropic_default_opus_model}")
        logging.info(f"ANTHROPIC_DEFAULT_SONNET_MODEL: {anthropic_default_sonnet_model}")
        logging.info(f"ANTHROPIC_DEFAULT_HAIKU_MODEL: {anthropic_default_haiku_model}")
        logging.info(f"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS: {claude_code_experimental_agent_teams}")
        
        # Check for conflicts
        if not autofix and context:
            logging.critical("CRITICAL: cannot have context without autofix enabled")
            exit(1)
            
        if not exploration and recurse:
            logging.critical("CRITICAL: cannot have recurse without exploration enabled")
            exit(1)

        if recording and silent:
            logging.critical("CRITICAL: cannot have recording enabled with silent enabled")
            exit(1)

        # Select prompts directory
        _teams = parse_bool(claude_code_experimental_agent_teams)
        PROMPTS_DIR = _get_teams_dir() if _teams else _get_prompts_dir()
        PROVE_PROMPTS_DIR = PROMPTS_DIR / "PROVE"
        PROVE_SUBAGENTS_DIR = _get_subagents_dir() / "PROVE"
        # Active dirs: prove mode swaps generation/semiformalization/formalization/critic
        ACTIVE_PROMPTS_DIR = PROVE_PROMPTS_DIR if prove else PROMPTS_DIR
        ACTIVE_SUBAGENTS_DIR = PROVE_SUBAGENTS_DIR if prove else _get_subagents_dir()
        logging.info(f"Prompts directory: {PROMPTS_DIR}")

        # Prove-mode conflict checks
        if prove and source is None and not context:
            logging.critical("CRITICAL: --prove without --source requires --context/-c")
            exit(1)

        if prove and source is None and not exploration:
            logging.critical("CRITICAL: --prove without --source requires EXPLORATION=true")
            exit(1)

        # Set permissions

        if no_bypass:
            PERMISSIONS="acceptEdits"
        else:
            PERMISSIONS="bypassPermissions"
        
        logging.info("Environment loaded successfully!")
    except Exception as e:
        logging.critical(f"CRITICAL (environment loading): {e}")
        exit(1)
        
    # Lean project initialization

    logging.info("Initializing Lean project...")
    try:
        # Create Lean project (if needed), get cache, and update
        project_path = Path(PROJECT_DIR).expanduser().resolve()

        if not project_path.exists():
            project_path.parent.mkdir(parents=True, exist_ok=True)
            _run(["lake", "new", project_path.name, "math"], cwd=project_path.parent)
        elif not _is_lean_repo(project_path):
            _run(["lake", "init", project_path.name, "math"], cwd=project_path)

        _run(["lake", "exe", "cache", "get"], cwd=project_path)
        _run(["lake", "update"], cwd=project_path)
        
        logging.info("Lean project initialized successfully!")
    except Exception as e:
        logging.critical(f"CRITICAL (project initialization): {e}")
        exit(1)

    # Configure lean-lsp-mcp server for all agents
    LEAN_MCP_SERVER = {
        "lean-lsp": {
            "command": "uvx",
            "args": ["lean-lsp-mcp", "--lean-project-path", str(project_path)],
            "cwd": str(project_path)
        }
    }

    # Library initialization and context loading
    _init_library()
    library_context = _load_library_context()
    global LIBRARY_SUBAGENTS
    LIBRARY_SUBAGENTS = _load_library_subagents()
    if library_context:
        logging.info("Library context loaded.")
    if LIBRARY_SUBAGENTS:
        logging.info(f"Loaded {len(LIBRARY_SUBAGENTS)} library subagent(s): {', '.join(LIBRARY_SUBAGENTS)}")

    def with_library(prompt: str) -> str:
        """Append library context to a prompt if any exists."""
        if not library_context:
            return prompt
        return prompt + "\n\n---\n\n" + library_context

    # ── Path 2: prove mode, no source ─────────────────────────────────────────
    # Flow: exploration → generation → semiformalization (TT) → preparation → loop
    if prove and source is None:

        # Exploration phase
        _console.rule("[bold blue]Exploration Phase[/bold blue]")
        try:
            with open(ACTIVE_PROMPTS_DIR / "EXPLORATION.md", "r") as f:
                EXPLORATION_PROMPT = with_library(f.read())
            with open(ACTIVE_SUBAGENTS_DIR / "EXPLORATION/EXPLORER.md", "r") as f:
                EXPLORER_SUBAGENT = f.read()

            async for message in query(
                prompt=f"Survey the Lean project at {project_path} for declarations needing proofs, then gather mathematical content for each.",
                options=ClaudeAgentOptions(
                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                    allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                    agents={
                        "explorer": AgentDefinition(
                            description="Explorer subagent. Capable of searching the web and gathering mathematical content for a specific Lean declaration needing a proof.",
                            prompt=EXPLORER_SUBAGENT,
                            tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                        ),
                        **LIBRARY_SUBAGENTS
                    },
                    system_prompt=EXPLORATION_PROMPT,
                    mcp_servers=LEAN_MCP_SERVER,
                    permission_mode=PERMISSIONS,
                    continue_conversation=False,
                    max_budget_usd=exploration_budget,
                    disallowed_tools=[],
                    enable_file_checkpointing=True,
                    model="opus",
                    fallback_model="sonnet",
                    env={
                        "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                        "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                        "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                        "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                        "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                    },
                    extra_args={},
                ),
            ):
                _log_agent_message(message)

            logging.info("Exploration phase completed successfully!")
        except Exception as e:
            logging.critical(f"CRITICAL (exploration phase): {e}")
            exit(1)

        # Generation phase
        _console.rule("[bold blue]Generation Phase[/bold blue]")
        try:
            with open(ACTIVE_PROMPTS_DIR / "GENERATION.md", "r") as f:
                GENERATION_PROMPT = with_library(f.read())
            with open(_get_subagents_dir() / "GENERATION/GENERATOR.md", "r") as f:
                GENERATOR_SUBAGENT = f.read()

            async for message in query(
                prompt=f"Generate the specification language for the gathered mathematical content in `gathered/`.",
                options=ClaudeAgentOptions(
                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                    allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                    agents={
                        "generator": AgentDefinition(
                            description="Generator subagent. Capable of assisting in the design of a semiformal specification language for a given source.",
                            prompt=GENERATOR_SUBAGENT,
                            tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                        ),
                        **LIBRARY_SUBAGENTS
                    },
                    system_prompt=GENERATION_PROMPT,
                    mcp_servers=LEAN_MCP_SERVER,
                    permission_mode=PERMISSIONS,
                    continue_conversation=False,
                    max_budget_usd=generation_budget,
                    disallowed_tools=[],
                    enable_file_checkpointing=True,
                    model="opus",
                    fallback_model="sonnet",
                    env={
                        "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                        "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                        "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                        "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                        "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                    },
                    extra_args={},
                ),
            ):
                _log_agent_message(message)

            logging.info("Generation phase completed successfully!")
        except Exception as e:
            logging.critical(f"CRITICAL (generation phase): {e}")
            exit(1)

        # Semiformalization phase (always TT: autofix + context, required for Path 2)
        _console.rule("[bold blue]Semiformalization Phase[/bold blue]")
        try:
            with open(ACTIVE_PROMPTS_DIR / "SEMIFORMALIZATION/TT.md", "r") as f:
                SEMIFORMALIZATION_PROMPT = with_library(f.read())
            with open(ACTIVE_SUBAGENTS_DIR / "SEMIFORMALIZATION/TT.md", "r") as f:
                SEMIFORMALIZER_SUBAGENT = f.read()

            async for message in query(
                prompt=f"Semiformalize the gathered content in `gathered/` using the specification language in `language/`. The Lean project is {project_path}.",
                options=ClaudeAgentOptions(
                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                    allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                    agents={
                        "semiformalizer": AgentDefinition(
                            description="Semiformalizer subagent. Capable of producing faithful semiformal translations of gathered mathematical content into the IR specification language located in `language/`.",
                            prompt=SEMIFORMALIZER_SUBAGENT,
                            tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                        ),
                        **LIBRARY_SUBAGENTS
                    },
                    system_prompt=SEMIFORMALIZATION_PROMPT,
                    mcp_servers=LEAN_MCP_SERVER,
                    permission_mode=PERMISSIONS,
                    continue_conversation=False,
                    max_budget_usd=semiformalization_budget,
                    disallowed_tools=[],
                    enable_file_checkpointing=True,
                    model="opus",
                    fallback_model="sonnet",
                    env={
                        "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                        "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                        "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                        "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                        "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                    },
                    extra_args={},
                ),
            ):
                _log_agent_message(message)

            logging.info("Semiformalization phase completed successfully!")
        except Exception as e:
            logging.critical(f"CRITICAL (semiformalization phase): {e}")
            exit(1)

        # Preparation phase
        _console.rule("[bold blue]Preparation Phase[/bold blue]")
        try:
            with open(ACTIVE_PROMPTS_DIR / "PREPARATION.md", "r") as f:
                PREPARATION_PROMPT = with_library(f.read())

            async for message in query(
                prompt=f"Prepare to formalize the declarations in {project_path}. Chunk by declaration, track dependencies, and create forums.",
                options=ClaudeAgentOptions(
                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                    allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                    agents={**LIBRARY_SUBAGENTS},
                    system_prompt=PREPARATION_PROMPT,
                    mcp_servers=LEAN_MCP_SERVER,
                    permission_mode=PERMISSIONS,
                    continue_conversation=False,
                    max_budget_usd=preparation_budget,
                    disallowed_tools=[],
                    enable_file_checkpointing=True,
                    model="opus",
                    fallback_model="sonnet",
                    env={
                        "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                        "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                        "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                        "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                        "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                    },
                    extra_args={},
                ),
            ):
                _log_agent_message(message)

            logging.info("Preparation phase completed successfully!")
        except Exception as e:
            logging.critical(f"CRITICAL (preparation phase): {e}")
            exit(1)

        iteration = 0
        while True:

            # Formalization phase (always T variant: existing project always present)
            _console.rule("[bold blue]Formalization Phase[/bold blue]")
            try:
                with open(ACTIVE_PROMPTS_DIR / "FORMALIZATION/T.md", "r") as f:
                    FORMALIZATION_PROMPT = with_library(f.read())
                with open(_get_subagents_dir() / "FORMALIZATION/DECLARATIONFORMALIZER/T.md", "r") as f:
                    DECLARATIONFORMALIZER_SUBAGENT = f.read()
                with open(ACTIVE_SUBAGENTS_DIR / "FORMALIZATION/PROOFFORMALIZER/T.md", "r") as f:
                    PROOFFORMALIZER_SUBAGENT = f.read()

                async for message in query(
                    prompt=f"Formalize the declarations in {project_path}.",
                    options=ClaudeAgentOptions(
                        tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                        agents={
                            "declaration-formalizer": AgentDefinition(
                                description="DeclarationFormalizer subagent. Capable of formalizing a declaration or statement of a specific chunk into Lean4.",
                                prompt=DECLARATIONFORMALIZER_SUBAGENT,
                                tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                            ),
                            "proof-formalizer": AgentDefinition(
                                description="ProofFormalizer subagent. Capable of formalizing the proof of a specific chunk into Lean4.",
                                prompt=PROOFFORMALIZER_SUBAGENT,
                                tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                            ),
                            **LIBRARY_SUBAGENTS
                        },
                        system_prompt=FORMALIZATION_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        permission_mode=PERMISSIONS,
                        continue_conversation=False,
                        max_budget_usd=formalization_budget,
                        disallowed_tools=[],
                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env={
                            "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                            "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                            "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                            "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                            "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                        },
                        extra_args={},
                    ),
                ):
                    _log_agent_message(message)

                logging.info("Formalization phase completed successfully!")
            except Exception as e:
                logging.critical(f"CRITICAL (formalization phase): {e}")
                exit(1)

            # Retrospective phase
            _console.rule("[bold blue]Retrospective Phase[/bold blue]")
            try:
                with open(PROMPTS_DIR / "RETROSPECTIVE.md", "r") as f:
                    RETROSPECTIVE_PROMPT = f.read().format(
                        SOURCE_PATH="(no source — proof completion mode)",
                        LIBRARY_DIR=str(_get_library_dir()),
                        PROJECT_NOTES_DIR=str(_get_project_notes_dir()),
                        SUBAGENTS_DIR=str(_get_subagents_dir()),
                        DEFAULT_SUBAGENTS_DIR=str(_get_default_subagents_dir()),
                    )
                async for message in query(
                    prompt=f"Run the retrospective for the unity proof formalization of {project_path}.",
                    options=ClaudeAgentOptions(
                        tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                        agents={**LIBRARY_SUBAGENTS},
                        system_prompt=RETROSPECTIVE_PROMPT,
                        permission_mode=PERMISSIONS,
                        continue_conversation=False,
                        disallowed_tools=[],
                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env={
                            "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                            "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                            "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                            "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                            "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                        },
                        extra_args={},
                    ),
                ):
                    _log_agent_message(message)
                logging.info("Retrospective phase completed successfully!")
            except Exception as e:
                logging.error(f"ERROR (retrospective phase): {e}")

            library_context = _load_library_context()

            # Critic phase (always T variant)
            _console.rule("[bold blue]Critic Phase[/bold blue]")
            try:
                with open(ACTIVE_PROMPTS_DIR / "CRITIC.md", "r") as f:
                    CRITIC_PROMPT = with_library(f.read())
                with open(_get_subagents_dir() / "CRITIC/DECLARATIONFORMALIZER/T.md", "r") as f:
                    DECLARATIONFORMALIZER_SUBAGENT = f.read()
                with open(_get_subagents_dir() / "CRITIC/PROOFFORMALIZER/T.md", "r") as f:
                    PROOFFORMALIZER_SUBAGENT = f.read()

                async for message in query(
                    prompt=f"Critique {project_path} given semiformalization `semiformal/` and specification language `language/`.",
                    options=ClaudeAgentOptions(
                        tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                        agents={
                            "declaration-formalizer": AgentDefinition(
                                description="DeclarationFormalizer subagent. Capable of formalizing a declaration or statement of a specific chunk into Lean4.",
                                prompt=DECLARATIONFORMALIZER_SUBAGENT,
                                tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                            ),
                            "proof-formalizer": AgentDefinition(
                                description="ProofFormalizer subagent. Capable of formalizing the proof of a specific chunk into Lean4.",
                                prompt=PROOFFORMALIZER_SUBAGENT,
                                tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                            ),
                            **LIBRARY_SUBAGENTS
                        },
                        system_prompt=CRITIC_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        permission_mode=PERMISSIONS,
                        continue_conversation=False,
                        max_budget_usd=critic_budget,
                        disallowed_tools=[],
                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env={
                            "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                            "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                            "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                            "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                            "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                        },
                        extra_args={},
                    ),
                ):
                    _log_agent_message(message)
                logging.info("Critic phase completed successfully!")
            except Exception as e:
                logging.critical(f"CRITICAL (critic phase): {e}")
                exit(1)

            # Loop status check
            try:
                report_text = Path("REPORT.md").read_text()
                if "**Status:** COMPLETE" in report_text:
                    logging.info("Critic declared formalization complete.")
                    break
                elif max_critic_iterations is not None and iteration + 1 >= max_critic_iterations:
                    logging.warning(f"Reached maximum iterations ({max_critic_iterations}) — stopping loop.")
                    break
                else:
                    iteration += 1
                    logging.info(f"Critic requested revision — starting iteration {iteration + 1}...")
            except FileNotFoundError:
                logging.warning("No REPORT.md found after critic phase — stopping loop.")
                break

        _console.rule("[bold blue]Summary[/bold blue]")
        try:
            from rich.markdown import Markdown
            _console.print(Markdown(Path("REPORT.md").read_text()))
        except FileNotFoundError:
            logging.warning("No REPORT.md found — critic may not have completed.")
        except Exception as e:
            logging.error(f"ERROR (summarization): {e}")

        logging.info("Unity has completed!")
        return 0

    # ── Path 1 / normal mode ──────────────────────────────────────────────────

    # Generation phase
    
    _console.rule("[bold blue]Generation Phase[/bold blue]")
    try:
        # Load generation phase system prompt and generator subagent prompt
        with open(ACTIVE_PROMPTS_DIR / "GENERATION.md", "r") as f:
            GENERATION_PROMPT = with_library(f.read())
        with open(_get_subagents_dir() / "GENERATION/GENERATOR.md", "r") as f:
            GENERATOR_SUBAGENT = f.read()
        
        async for message in query(
            prompt=f"Generate the specification language for {source}.",
            options=ClaudeAgentOptions(
                tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                agents={
                    "generator": AgentDefinition(
                        description="Generator subagent. Capable of assisting in the design of a semiformal specification language for a given source.",
                        prompt=GENERATOR_SUBAGENT,
                        tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                    ),
                    **LIBRARY_SUBAGENTS
                },
                system_prompt=GENERATION_PROMPT,
                mcp_servers=LEAN_MCP_SERVER,
                permission_mode=PERMISSIONS,
                continue_conversation=False,
                max_budget_usd=generation_budget,
                disallowed_tools=[],
                enable_file_checkpointing=True,
                model="opus",
                fallback_model="sonnet",
                env={
                    "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                    "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                    "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                    "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                    "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                },
                extra_args={},
            ),
        ):
            _log_agent_message(message)
                
        logging.info("Generation phase completed successfully!")

    except Exception as e:
        logging.critical(f"CRITICAL (generation phase): {e}")
        exit(1)
        
    # Semiformalization phase
    
    _console.rule("[bold blue]Semiformalization Phase[/bold blue]")
    if not autofix and not context:
        try:
            # Load semiformalization phase system prompt and semiformalizer subagent prompt
            with open(ACTIVE_PROMPTS_DIR / "SEMIFORMALIZATION/FF.md", "r") as f:
                SEMIFORMALIZATION_PROMPT = with_library(f.read())
            with open(ACTIVE_SUBAGENTS_DIR / "SEMIFORMALIZATION/FF.md", "r") as f:
                SEMIFORMALIZER_SUBAGENT = f.read()
            
            async for message in query(
                prompt=f"Semiformalize {source} as specified by the language.",
                options=ClaudeAgentOptions(
                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                    allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                    agents={
                        "semiformalizer": AgentDefinition(
                            description="Semiformalizer subagent. Capable of producing faithful semiformal translations of a source into the IR specification language located in `language/`.",
                            prompt=SEMIFORMALIZER_SUBAGENT,
                            tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                        ),
                        **LIBRARY_SUBAGENTS
                    },
                    system_prompt=SEMIFORMALIZATION_PROMPT,
                    mcp_servers=LEAN_MCP_SERVER,
                    permission_mode=PERMISSIONS,
                    continue_conversation=False,
                    max_budget_usd=semiformalization_budget,
                    disallowed_tools=[],
                    enable_file_checkpointing=True,
                    model="opus",
                    fallback_model="sonnet",
                    env={
                        "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                        "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                        "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                        "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                        "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                    },
                    extra_args={},
                ),
            ):
                _log_agent_message(message)
                    
            logging.info("Semiformalization phase completed successfully!")
        except Exception as e:
            logging.critical(f"CRITICAL (semiformalization phase): {e}")
            exit(1)
    elif autofix and not context:
        try:
            # Load semiformalization phase system prompt and semiformalizer subagent prompt
            with open(ACTIVE_PROMPTS_DIR / "SEMIFORMALIZATION/TF.md", "r") as f:
                SEMIFORMALIZATION_PROMPT = with_library(f.read())
            with open(ACTIVE_SUBAGENTS_DIR / "SEMIFORMALIZATION/TF.md", "r") as f:
                SEMIFORMALIZER_SUBAGENT = f.read()
            
            async for message in query(
                prompt=f"Semiformalize {source} as specified by the language.",
                options=ClaudeAgentOptions(
                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                    allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                    agents={
                        "semiformalizer": AgentDefinition(
                            description="Semiformalizer subagent. Capable of producing faithful semiformal translations of a source into the IR specification language located in `language/`.",
                            prompt=SEMIFORMALIZER_SUBAGENT,
                            tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                        ),
                        **LIBRARY_SUBAGENTS
                    },
                    system_prompt=SEMIFORMALIZATION_PROMPT,
                    mcp_servers=LEAN_MCP_SERVER,
                    permission_mode=PERMISSIONS,
                    continue_conversation=False,
                    max_budget_usd=semiformalization_budget,
                    disallowed_tools=[],
                    enable_file_checkpointing=True,
                    model="opus",
                    fallback_model="sonnet",
                    env={
                        "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                        "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                        "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                        "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                        "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                    },
                    extra_args={},
                ),
            ):
                _log_agent_message(message)
                    
            logging.info("Semiformalization phase completed successfully!")
        except Exception as e:
            logging.critical(f"CRITICAL (semiformalization phase): {e}")
            exit(1)
    elif autofix and context:
        try:
            # Load semiformalization phase system prompt and semiformalizer subagent prompt
            with open(ACTIVE_PROMPTS_DIR / "SEMIFORMALIZATION/TT.md", "r") as f:
                SEMIFORMALIZATION_PROMPT = with_library(f.read())
            with open(ACTIVE_SUBAGENTS_DIR / "SEMIFORMALIZATION/TT.md", "r") as f:
                SEMIFORMALIZER_SUBAGENT = f.read()
            
            async for message in query(
                prompt=f"Semiformalize {source} as specified by the language. The Lean project is {project_path}.",
                options=ClaudeAgentOptions(
                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                    allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                    agents={
                        "semiformalizer": AgentDefinition(
                            description="Semiformalizer subagent. Capable of producing faithful semiformal translations of a source into the IR specification language located in `language/`.",
                            prompt=SEMIFORMALIZER_SUBAGENT,
                            tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                        ),
                        **LIBRARY_SUBAGENTS
                    },
                    system_prompt=SEMIFORMALIZATION_PROMPT,
                    mcp_servers=LEAN_MCP_SERVER,
                    permission_mode=PERMISSIONS,
                    continue_conversation=False,
                    max_budget_usd=semiformalization_budget,
                    disallowed_tools=[],
                    enable_file_checkpointing=True,
                    model="opus",
                    fallback_model="sonnet",
                    env={
                        "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                        "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                        "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                        "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                        "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                    },
                    extra_args={},
                ),
            ):
                _log_agent_message(message)
                    
            logging.info("Semiformalization phase completed successfully!")
        except Exception as e:
            logging.critical(f"CRITICAL (semiformalization phase): {e}")
            exit(1)
    else:
        logging.critical("CRITICAL (semiformalization phase): cannot have context without autofix enabled")
        exit(1)
        
    iteration = 0
    while True:

        # Exploration phase
    
        if exploration:
            _console.rule("[bold blue]Exploration Phase[/bold blue]")
            if not recurse and not context:
                try:
                    # Load exploration phase system prompt and explorer, semiformalizer, and exploration-generator subagent prompts
                    with open(PROMPTS_DIR / "EXPLORATION/FF.md", "r") as f:
                        EXPLORATION_PROMPT = with_library(f.read())
                    with open(_get_subagents_dir() / "EXPLORATION/EXPLORER/F.md", "r") as f:
                        EXPLORER_SUBAGENT = f.read()
                    with open(_get_subagents_dir() / "EXPLORATION/SEMIFORMALIZER/F.md", "r") as f:
                        SEMIFORMALIZER_SUBAGENT = f.read()
                    with open(_get_subagents_dir() / "EXPLORATION/EXPLORATIONGENERATOR.md", "r") as f:
                        EXPLORATIONGENERATOR_SUBAGENT = f.read()
                
                    async for message in query(
                        prompt=f"Explore `semiformal/` given specification language `language/` and source {source}.",
                        options=ClaudeAgentOptions(
                            tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                            allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                            agents={
                                "explorer": AgentDefinition(
                                    description="Explorer subagent. Capable of searching the web and gathering sources for a specific assumption type.",
                                    prompt=EXPLORER_SUBAGENT,
                                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                                ),
                                "semiformalizer": AgentDefinition(
                                    description="Semiformalizer subagent. Capable of semiformalizing gathered sources for specific assumption types into the existing semiformal translation.",
                                    prompt=SEMIFORMALIZER_SUBAGENT,
                                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                                ),
                                "exploration-generator": AgentDefinition(
                                    description="ExplorationGenerator subagent. Capable of extending the IR specification language to accomodate new sources gathered during exploration.",
                                    prompt=EXPLORATIONGENERATOR_SUBAGENT,
                                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                                ),
                                **LIBRARY_SUBAGENTS
                            },
                            system_prompt=EXPLORATION_PROMPT,
                            mcp_servers=LEAN_MCP_SERVER,
                            permission_mode=PERMISSIONS,
                            continue_conversation=False,
                            max_budget_usd=exploration_budget,
                            disallowed_tools=[],
                            enable_file_checkpointing=True,
                            model="opus",
                            fallback_model="sonnet",
                            env={
                                "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                                "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                                "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                                "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                                "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                                "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                                "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                            },
                            extra_args={},
                        ),
                    ):
                        _log_agent_message(message)
                        
                    logging.info("Exploration phase completed successfully!")
                except Exception as e:
                    logging.critical(f"CRITICAL (exploration phase): {e}")
                    exit(1)
            elif not recurse and context:
                try:
                    # Load exploration phase system prompt and explorer, semiformalizer, and exploration-generator subagent prompts
                    with open(PROMPTS_DIR / "EXPLORATION/FT.md", "r") as f:
                        EXPLORATION_PROMPT = with_library(f.read())
                    with open(_get_subagents_dir() / "EXPLORATION/EXPLORER/T.md", "r") as f:
                        EXPLORER_SUBAGENT = f.read()
                    with open(_get_subagents_dir() / "EXPLORATION/SEMIFORMALIZER/T.md", "r") as f:
                        SEMIFORMALIZER_SUBAGENT = f.read()
                    with open(_get_subagents_dir() / "EXPLORATION/EXPLORATIONGENERATOR.md", "r") as f:
                        EXPLORATIONGENERATOR_SUBAGENT = f.read()
                
                    async for message in query(
                        prompt=f"Explore `semiformal/` given specification language `language/` and source {source}. The Lean project is {project_path}.",
                        options=ClaudeAgentOptions(
                            tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                            allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                            agents={
                                "explorer": AgentDefinition(
                                    description="Explorer subagent. Capable of searching the web and gathering sources for a specific assumption type.",
                                    prompt=EXPLORER_SUBAGENT,
                                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                                ),
                                "semiformalizer": AgentDefinition(
                                    description="Semiformalizer subagent. Capable of semiformalizing gathered sources for specific assumption types into the existing semiformal translation.",
                                    prompt=SEMIFORMALIZER_SUBAGENT,
                                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                                ),
                                "exploration-generator": AgentDefinition(
                                    description="ExplorationGenerator subagent. Capable of extending the IR specification language to accomodate new sources gathered during exploration.",
                                    prompt=EXPLORATIONGENERATOR_SUBAGENT,
                                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                                ),
                                **LIBRARY_SUBAGENTS
                            },
                            system_prompt=EXPLORATION_PROMPT,
                            mcp_servers=LEAN_MCP_SERVER,
                            permission_mode=PERMISSIONS,
                            continue_conversation=False,
                            max_budget_usd=exploration_budget,
                            disallowed_tools=[],
                            enable_file_checkpointing=True,
                            model="opus",
                            fallback_model="sonnet",
                            env={
                                "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                                "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                                "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                                "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                                "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                                "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                                "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                            },
                            extra_args={},
                        ),
                    ):
                        _log_agent_message(message)
                        
                    logging.info("Exploration phase completed successfully!")
                except Exception as e:
                    logging.critical(f"CRITICAL (exploration phase): {e}")
                    exit(1)
            elif recurse and not context:
                try:
                    # Load exploration phase system prompt and explorer, semiformalizer, and exploration-generator subagent prompts
                    with open(PROMPTS_DIR / "EXPLORATION/TF.md", "r") as f:
                        EXPLORATION_PROMPT = with_library(f.read())
                    with open(_get_subagents_dir() / "EXPLORATION/EXPLORER/F.md", "r") as f:
                        EXPLORER_SUBAGENT = f.read()
                    with open(_get_subagents_dir() / "EXPLORATION/SEMIFORMALIZER/F.md", "r") as f:
                        SEMIFORMALIZER_SUBAGENT = f.read()
                    with open(_get_subagents_dir() / "EXPLORATION/EXPLORATIONGENERATOR.md", "r") as f:
                        EXPLORATIONGENERATOR_SUBAGENT = f.read()
                
                    async for message in query(
                        prompt=f"Explore `semiformal/` given specification language `language/` and source {source}.",
                        options=ClaudeAgentOptions(
                            tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                            allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                            agents={
                                "explorer": AgentDefinition(
                                    description="Explorer subagent. Capable of searching the web and gathering sources for a specific assumption type.",
                                    prompt=EXPLORER_SUBAGENT,
                                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                                ),
                                "semiformalizer": AgentDefinition(
                                    description="Semiformalizer subagent. Capable of semiformalizing gathered sources for specific assumption types into the existing semiformal translation.",
                                    prompt=SEMIFORMALIZER_SUBAGENT,
                                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                                ),
                                "exploration-generator": AgentDefinition(
                                    description="ExplorationGenerator subagent. Capable of extending the IR specification language to accomodate new sources gathered during exploration.",
                                    prompt=EXPLORATIONGENERATOR_SUBAGENT,
                                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                                ),
                                **LIBRARY_SUBAGENTS
                            },
                            system_prompt=EXPLORATION_PROMPT,
                            mcp_servers=LEAN_MCP_SERVER,
                            permission_mode=PERMISSIONS,
                            continue_conversation=False,
                            max_budget_usd=exploration_budget,
                            disallowed_tools=[],
                            enable_file_checkpointing=True,
                            model="opus",
                            fallback_model="sonnet",
                            env={
                                "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                                "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                                "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                                "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                                "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                                "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                                "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                            },
                            extra_args={},
                        ),
                    ):
                        _log_agent_message(message)
                        
                    logging.info("Exploration phase completed successfully!")
                except Exception as e:
                    logging.critical(f"CRITICAL (exploration phase): {e}")
                    exit(1)
            elif recurse and context:
                try:
                    # Load exploration phase system prompt and explorer, semiformalizer, and exploration-generator subagent prompts
                    with open(PROMPTS_DIR / "EXPLORATION/TT.md", "r") as f:
                        EXPLORATION_PROMPT = with_library(f.read())
                    with open(_get_subagents_dir() / "EXPLORATION/EXPLORER/T.md", "r") as f:
                        EXPLORER_SUBAGENT = f.read()
                    with open(_get_subagents_dir() / "EXPLORATION/SEMIFORMALIZER/T.md", "r") as f:
                        SEMIFORMALIZER_SUBAGENT = f.read()
                    with open(_get_subagents_dir() / "EXPLORATION/EXPLORATIONGENERATOR.md", "r") as f:
                        EXPLORATIONGENERATOR_SUBAGENT = f.read()
                
                    async for message in query(
                        prompt=f"Explore `semiformal/` given specification language `language/` and source {source}. The Lean project is {project_path}.",
                        options=ClaudeAgentOptions(
                            tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                            allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                            agents={
                                "explorer": AgentDefinition(
                                    description="Explorer subagent. Capable of searching the web and gathering sources for a specific assumption type.",
                                    prompt=EXPLORER_SUBAGENT,
                                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                                ),
                                "semiformalizer": AgentDefinition(
                                    description="Semiformalizer subagent. Capable of semiformalizing gathered sources for specific assumption types into the existing semiformal translation.",
                                    prompt=SEMIFORMALIZER_SUBAGENT,
                                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                                ),
                                "exploration-generator": AgentDefinition(
                                    description="ExplorationGenerator subagent. Capable of extending the IR specification language to accomodate new sources gathered during exploration.",
                                    prompt=EXPLORATIONGENERATOR_SUBAGENT,
                                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                                ),
                                **LIBRARY_SUBAGENTS
                            },
                            system_prompt=EXPLORATION_PROMPT,
                            mcp_servers=LEAN_MCP_SERVER,
                            permission_mode=PERMISSIONS,
                            continue_conversation=False,
                            max_budget_usd=exploration_budget,
                            disallowed_tools=[],
                            enable_file_checkpointing=True,
                            model="opus",
                            fallback_model="sonnet",
                            env={
                                "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                                "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                                "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                                "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                                "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                                "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                                "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                            },
                            extra_args={},
                        ),
                    ):
                        _log_agent_message(message)
                        
                    logging.info("Exploration phase completed successfully!")
                except Exception as e:
                    logging.critical(f"CRITICAL (exploration phase): {e}")
                    exit(1)
            else:
                logging.critical("CRITICAL (exploration phase): reached unreachable code")
                exit(1)
        else:
            logging.info("Exploration phase skipped.")
        
        # Preparation phase
    
        _console.rule("[bold blue]Preparation Phase[/bold blue]")
    
        if not context and iteration == 0:
            try:
                # Load preparation phase system prompt
                with open(PROMPTS_DIR / "PREPARATION/F.md", "r") as f:
                    PREPARATION_PROMPT = with_library(f.read())
            
                async for message in query(
                    prompt=f"Prepare to formalize {source}.",
                    options=ClaudeAgentOptions(
                        tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                        agents={**LIBRARY_SUBAGENTS},
                        system_prompt=PREPARATION_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        permission_mode=PERMISSIONS,
                        continue_conversation=False,
                        max_budget_usd=preparation_budget,
                        disallowed_tools=[],
                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env={
                            "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                            "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                            "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                            "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                            "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                        },
                        extra_args={},
                    ),
                ):
                    _log_agent_message(message)
                    
                logging.info("Preparation phase completed successfully!")
            except Exception as e:
                logging.critical(f"CRITICAL (preparation phase): {e}")
                exit(1)
        elif context or iteration > 0:
            try:
                # Load preparation phase system prompt
                with open(PROMPTS_DIR / "PREPARATION/T.md", "r") as f:
                    PREPARATION_PROMPT = with_library(f.read())
            
                async for message in query(
                    prompt=f"Prepare to formalize {source}. The Lean project is {project_path}.",
                    options=ClaudeAgentOptions(
                        tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                        agents={**LIBRARY_SUBAGENTS},
                        system_prompt=PREPARATION_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        permission_mode=PERMISSIONS,
                        continue_conversation=False,
                        max_budget_usd=preparation_budget,
                        disallowed_tools=[],
                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env={
                            "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                            "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                            "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                            "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                            "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                        },
                        extra_args={},
                    ),
                ):
                    _log_agent_message(message)
                    
                logging.info("Preparation phase completed successfully!")
            except Exception as e:
                logging.critical(f"CRITICAL (preparation phase): {e}")
                exit(1)
        else:
            logging.critical("CRITICAL (preparation phase): reached unreachable code")
            exit(1)
        
        # Formalization phase
    
        _console.rule("[bold blue]Formalization Phase[/bold blue]")
    
        if not context and iteration == 0:
            try:
                # Load formalization phase system prompt and declaration-formalizer and proof-formalizer subagent prompts
                with open(ACTIVE_PROMPTS_DIR / "FORMALIZATION/F.md", "r") as f:
                    FORMALIZATION_PROMPT = with_library(f.read())
                with open(_get_subagents_dir() / "FORMALIZATION/DECLARATIONFORMALIZER/F.md", "r") as f:
                    DECLARATIONFORMALIZER_SUBAGENT = f.read()
                with open(ACTIVE_SUBAGENTS_DIR / "FORMALIZATION/PROOFFORMALIZER/F.md", "r") as f:
                    PROOFFORMALIZER_SUBAGENT = f.read()
            
                async for message in query(
                    prompt=f"Formalize {source} into {project_path}.",
                    options=ClaudeAgentOptions(
                        tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                        agents={
                            "declaration-formalizer": AgentDefinition(
                                description="DeclarationFormalizer subagent. Capable of formalizing a declaration or statement of a specific chunk into Lean4.",
                                prompt=DECLARATIONFORMALIZER_SUBAGENT,
                                tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                            ),
                            "proof-formalizer": AgentDefinition(
                                description="ProofFormalizer subagent. Capable of formalizing the proof of a specific chunk into Lean4.",
                                prompt=PROOFFORMALIZER_SUBAGENT,
                                tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                            ),
                            **LIBRARY_SUBAGENTS
                        },
                        system_prompt=FORMALIZATION_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        permission_mode=PERMISSIONS,
                        continue_conversation=False,
                        max_budget_usd=formalization_budget,
                        disallowed_tools=[],
                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env={
                            "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                            "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                            "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                            "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                            "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                        },
                        extra_args={},
                    ),
                ):
                    _log_agent_message(message)
                    
                logging.info("Formalization phase completed successfully!")
            except Exception as e:
                logging.critical(f"CRITICAL (formalization phase): {e}")
                exit(1)
        elif context or iteration > 0:
            try:
                # Load formalization phase system prompt and declaration-formalizer and proof-formalizer subagent prompts
                with open(ACTIVE_PROMPTS_DIR / "FORMALIZATION/T.md", "r") as f:
                    FORMALIZATION_PROMPT = with_library(f.read())
                with open(_get_subagents_dir() / "FORMALIZATION/DECLARATIONFORMALIZER/T.md", "r") as f:
                    DECLARATIONFORMALIZER_SUBAGENT = f.read()
                with open(ACTIVE_SUBAGENTS_DIR / "FORMALIZATION/PROOFFORMALIZER/T.md", "r") as f:
                    PROOFFORMALIZER_SUBAGENT = f.read()
            
                async for message in query(
                    prompt=f"Formalize {source} into {project_path}.",
                    options=ClaudeAgentOptions(
                        tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                        agents={
                            "declaration-formalizer": AgentDefinition(
                                description="DeclarationFormalizer subagent. Capable of formalizing a declaration or statement of a specific chunk into Lean4.",
                                prompt=DECLARATIONFORMALIZER_SUBAGENT,
                                tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                            ),
                            "proof-formalizer": AgentDefinition(
                                description="ProofFormalizer subagent. Capable of formalizing the proof of a specific chunk into Lean4.",
                                prompt=PROOFFORMALIZER_SUBAGENT,
                                tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                            ),
                            **LIBRARY_SUBAGENTS
                        },
                        system_prompt=FORMALIZATION_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        permission_mode=PERMISSIONS,
                        continue_conversation=False,
                        max_budget_usd=formalization_budget,
                        disallowed_tools=[],
                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env={
                            "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                            "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                            "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                            "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                            "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                        },
                        extra_args={},
                    ),
                ):
                    _log_agent_message(message)
                    
                logging.info("Formalization phase completed successfully!")
            except Exception as e:
                logging.critical(f"CRITICAL (formalization phase): {e}")
                exit(1)
        else:
            logging.critical("CRITICAL (formalization phase): reached unreachable code")
            exit(1)
        
        # Critic phase
    
        _console.rule("[bold blue]Critic Phase[/bold blue]")

        if not context and iteration == 0:
            try:
                # Load critic phase system prompt and declaration-formalizer and proof-formalizer subagent prompts
                with open(ACTIVE_PROMPTS_DIR / "CRITIC.md", "r") as f:
                    CRITIC_PROMPT = with_library(f.read())
                with open(_get_subagents_dir() / "CRITIC/DECLARATIONFORMALIZER/F.md", "r") as f:
                    DECLARATIONFORMALIZER_SUBAGENT = f.read()
                with open(_get_subagents_dir() / "CRITIC/PROOFFORMALIZER/F.md", "r") as f:
                    PROOFFORMALIZER_SUBAGENT = f.read()
            
                async for message in query(
                    prompt=f"Critique {project_path} given source {source}, semiformalization `semiformal/`, and specification language `language/`.",
                    options=ClaudeAgentOptions(
                        tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                        agents={
                            "declaration-formalizer": AgentDefinition(
                                description="DeclarationFormalizer subagent. Capable of formalizing a declaration or statement of a specific chunk into Lean4.",
                                prompt=DECLARATIONFORMALIZER_SUBAGENT,
                                tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                            ),
                            "proof-formalizer": AgentDefinition(
                                description="ProofFormalizer subagent. Capable of formalizing the proof of a specific chunk into Lean4.",
                                prompt=PROOFFORMALIZER_SUBAGENT,
                                tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                            ),
                            **LIBRARY_SUBAGENTS
                        },
                        system_prompt=CRITIC_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        permission_mode=PERMISSIONS,
                        continue_conversation=False,
                        max_budget_usd=critic_budget,
                        disallowed_tools=[],
                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env={
                            "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                            "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                            "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                            "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                            "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                        },
                        extra_args={},
                    ),
                ):
                    _log_agent_message(message)
                    
                logging.info("Critic phase completed successfully!")
            except Exception as e:
                logging.critical(f"CRITICAL (critic phase): {e}")
                exit(1)
        elif context or iteration > 0:
            try:
                # Load critic phase system prompt and declaration-formalizer and proof-formalizer subagent prompts
                with open(ACTIVE_PROMPTS_DIR / "CRITIC.md", "r") as f:
                    CRITIC_PROMPT = with_library(f.read())
                with open(_get_subagents_dir() / "CRITIC/DECLARATIONFORMALIZER/T.md", "r") as f:
                    DECLARATIONFORMALIZER_SUBAGENT = f.read()
                with open(_get_subagents_dir() / "CRITIC/PROOFFORMALIZER/T.md", "r") as f:
                    PROOFFORMALIZER_SUBAGENT = f.read()
            
                async for message in query(
                    prompt=f"Critique {project_path} given source {source}, semiformalization `semiformal/`, and specification language `language/`.",
                    options=ClaudeAgentOptions(
                        tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                        agents={
                            "declaration-formalizer": AgentDefinition(
                                description="DeclarationFormalizer subagent. Capable of formalizing a declaration or statement of a specific chunk into Lean4.",
                                prompt=DECLARATIONFORMALIZER_SUBAGENT,
                                tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                            ),
                            "proof-formalizer": AgentDefinition(
                                description="ProofFormalizer subagent. Capable of formalizing the proof of a specific chunk into Lean4.",
                                prompt=PROOFFORMALIZER_SUBAGENT,
                                tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]
                            ),
                            **LIBRARY_SUBAGENTS
                        },
                        system_prompt=CRITIC_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        permission_mode=PERMISSIONS,
                        continue_conversation=False,
                        max_budget_usd=critic_budget,
                        disallowed_tools=[],
                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env={
                            "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                            "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                            "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                            "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                            "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                        },
                        extra_args={},
                    ),
                ):
                    _log_agent_message(message)
                    
                logging.info("Critic phase completed successfully!")
            except Exception as e:
                logging.critical(f"CRITICAL (critic phase): {e}")
                exit(1)
        else:
            logging.critical("CRITICAL (critic phase): reached unreachable code")
            exit(1)
        
        # Retrospective phase (inside loop — updates .unity/ before next iteration)

        _console.rule("[bold blue]Retrospective Phase[/bold blue]")
        try:
            with open(PROMPTS_DIR / "RETROSPECTIVE.md", "r") as f:
                RETROSPECTIVE_PROMPT = f.read().format(
                    SOURCE_PATH=source,
                    LIBRARY_DIR=str(_get_library_dir()),
                    PROJECT_NOTES_DIR=str(_get_project_notes_dir()),
                    SUBAGENTS_DIR=str(_get_subagents_dir()),
                    DEFAULT_SUBAGENTS_DIR=str(_get_default_subagents_dir()),
                )

            async for message in query(
                prompt=f"Run the retrospective for the unity formalization of {source}.",
                options=ClaudeAgentOptions(
                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                    allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"],
                    agents={**LIBRARY_SUBAGENTS},
                    system_prompt=RETROSPECTIVE_PROMPT,
                    permission_mode=PERMISSIONS,
                    continue_conversation=False,
                    disallowed_tools=[],
                    enable_file_checkpointing=True,
                    model="opus",
                    fallback_model="sonnet",
                    env={
                        "ANTHROPIC_BASE_URL":os.getenv("ANTHROPIC_BASE_URL"),
                        "ANTHROPIC_API_KEY":os.getenv("ANTHROPIC_API_KEY"),
                        "ANTHROPIC_AUTH_TOKEN":os.getenv("ANTHROPIC_AUTH_TOKEN"),
                        "ANTHROPIC_DEFAULT_OPUS_MODEL":os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                        "ANTHROPIC_DEFAULT_SONNET_MODEL":os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL":os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
                    },
                    extra_args={},
                ),
            ):
                _log_agent_message(message)

            logging.info("Retrospective phase completed successfully!")
        except Exception as e:
            logging.error(f"ERROR (retrospective phase): {e}")

        # Reload library context so next iteration picks up .unity/ additions
        library_context = _load_library_context()

        # Critic loop status check
        try:
            report_text = Path("REPORT.md").read_text()
            if "**Status:** COMPLETE" in report_text:
                logging.info("Critic declared formalization complete.")
                break
            elif max_critic_iterations is not None and iteration + 1 >= max_critic_iterations:
                logging.warning(f"Reached maximum iterations ({max_critic_iterations}) — stopping loop.")
                break
            else:
                iteration += 1
                logging.info(f"Critic requested revision — starting iteration {iteration + 1}...")
        except FileNotFoundError:
            logging.warning("No REPORT.md found after critic phase — stopping loop.")
            break

    # Cleanup

    logging.info("Cleaning up...")
    
    try:    
        if not save_spec:
            spec_dir = Path("language")
            subprocess.run(["rm", "-rf", str(spec_dir)], check=True)
        
        if not save_semiformalization:
            semiformal_dir = Path("semiformal")
            subprocess.run(["rm", "-rf", str(semiformal_dir)], check=True)
    except Exception as e:
        logging.error(f"ERROR (clean up): {e}")
        
    logging.info("Clean up completed successfully!")
        
    _console.rule("[bold blue]Summary[/bold blue]")
    try:
        from rich.markdown import Markdown
        _console.print(Markdown(Path("REPORT.md").read_text()))
    except FileNotFoundError:
        logging.warning("No REPORT.md found — critic may not have completed.")
    except Exception as e:
        logging.error(f"ERROR (summarization): {e}")

    logging.info("Unity has completed!")
    return 0
