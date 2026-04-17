"""Interactive setup command for generating .env configuration."""

import os
from pathlib import Path


def prompt_with_default(prompt: str, default: str = "", secret: bool = False) -> str:
    """Prompt user for input with optional default value."""
    if default:
        display = f"{prompt} [{default}]: "
    else:
        display = f"{prompt}: "
    
    if secret:
        import getpass
        value = getpass.getpass(display)
    else:
        value = input(display)
    
    return value.strip() if value.strip() else default


def prompt_bool(prompt: str, default: bool = False) -> bool:
    """Prompt user for yes/no input."""
    default_str = "Y/n" if default else "y/N"
    value = input(f"{prompt} [{default_str}]: ").strip().lower()
    
    if not value:
        return default
    return value in ("y", "yes", "true", "1")


def run_setup(output_path: str = ".env"):
    """Run interactive setup to generate .env file."""
    print("\n╔══════════════════════════════════════════════════════════════╗")
    print("║                    Unity Agent Setup                         ║")
    print("╚══════════════════════════════════════════════════════════════╝\n")
    
    env_vars = {}
    
    # Primary Tier Configuration
    print("─── Primary Tier (default model) ───\n")

    env_vars["PRIMARY_BASE_URL"] = prompt_with_default(
        "Primary Base URL (leave empty for SDK default)",
        os.getenv("PRIMARY_BASE_URL", "https://api.anthropic.com")
    )

    env_vars["PRIMARY_API_KEY"] = prompt_with_default(
        "Primary API Key",
        os.getenv("PRIMARY_API_KEY", ""),
        secret=True
    )

    env_vars["PRIMARY_AUTH_TOKEN"] = prompt_with_default(
        "Primary Auth Token (optional)",
        os.getenv("PRIMARY_AUTH_TOKEN", ""),
        secret=True
    )

    env_vars["PRIMARY_MODEL"] = prompt_with_default(
        "Primary Model",
        os.getenv("PRIMARY_MODEL", "claude-sonnet-4-6")
    )

    # Secondary Tier Configuration
    print("\n─── Secondary Tier (escalation model) ───\n")

    env_vars["SECONDARY_BASE_URL"] = prompt_with_default(
        "Secondary Base URL (leave empty for SDK default)",
        os.getenv("SECONDARY_BASE_URL", "https://api.anthropic.com")
    )

    env_vars["SECONDARY_API_KEY"] = prompt_with_default(
        "Secondary API Key",
        os.getenv("SECONDARY_API_KEY", ""),
        secret=True
    )

    env_vars["SECONDARY_AUTH_TOKEN"] = prompt_with_default(
        "Secondary Auth Token (optional)",
        os.getenv("SECONDARY_AUTH_TOKEN", ""),
        secret=True
    )

    env_vars["SECONDARY_MODEL"] = prompt_with_default(
        "Secondary Model",
        os.getenv("SECONDARY_MODEL", "claude-opus-4-7")
    )
    
    # Pipeline Flags
    print("\n─── Pipeline Configuration ───\n")
    
    env_vars["AUTOFIX"] = "true" if prompt_bool("Enable autofix mode", True) else ""
    env_vars["EXPLORATION"] = "true" if prompt_bool("Enable exploration phase", True) else ""
    env_vars["RECURSE"] = "true" if prompt_bool("Enable recursive exploration", False) else ""
    env_vars["NO_BYPASS"] = "true" if prompt_bool("Require permission for edits (no bypass)", False) else ""
    env_vars["SILENT"] = "true" if prompt_bool("Silent mode (redirect output to files)", False) else ""
    env_vars["RECORDING"] = "true" if prompt_bool("Enable recording", True) else ""
    env_vars["SAVE_SPECIFICATION"] = "true" if prompt_bool("Save specification to file", True) else ""
    env_vars["SAVE_SEMIFORMALIZATION"] = "true" if prompt_bool("Save semiformalization to file", True) else ""
    
    # Budget Configuration
    print("\n─── Budget Configuration (USD) ───\n")
    
    env_vars["GENERATION_BUDGET"] = prompt_with_default("Generation phase budget", "5.0")
    env_vars["SEMIFORMALIZATION_BUDGET"] = prompt_with_default("Semiformalization phase budget", "5.0")
    env_vars["EXPLORATION_BUDGET"] = prompt_with_default("Exploration phase budget", "10.0")
    env_vars["SOURCE_SCAN_BUDGET"] = prompt_with_default("Source scan phase budget", "5.0")
    env_vars["PREPARATION_BUDGET"] = prompt_with_default("Preparation phase budget", "5.0")
    env_vars["FORMALIZATION_BUDGET"] = prompt_with_default("Formalization phase budget", "15.0")
    env_vars["CRITIC_BUDGET"] = prompt_with_default("Critic phase budget", "5.0")
    env_vars["VALIDATION_BUDGET"] = prompt_with_default("Validation phase budget", "5.0")
    
    # Iteration / Retry Limits
    print("\n─── Iteration / Retry Limits ───\n")

    env_vars["MAX_CRITIC_ITERATIONS"] = prompt_with_default("Max critic iterations (blank = unlimited)", "")
    env_vars["MAX_VALIDATION_ITERATIONS"] = prompt_with_default("Max validation iterations (blank = unlimited)", "")
    env_vars["RESOLVER_MAX_RETRIES"] = prompt_with_default("Resolver max retries (blank = default)", "")

    # Server Configuration
    print("\n─── Server Configuration ───\n")

    env_vars["FORUM_PORT"] = prompt_with_default("Forum port", os.getenv("FORUM_PORT", "6367"))

    # Experimental Features
    print("\n─── Experimental Features ───\n")
    
    env_vars["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "true" if prompt_bool(
        "Enable experimental agent teams", False
    ) else ""
    
    # Write .env file
    output_file = Path(output_path)
    
    with open(output_file, "w") as f:
        f.write("# Unity Agent Configuration\n")
        f.write("# Generated by `unity setup`\n\n")
        
        f.write("# Primary Tier\n")
        for key in ["PRIMARY_BASE_URL", "PRIMARY_API_KEY", "PRIMARY_AUTH_TOKEN", "PRIMARY_MODEL"]:
            if env_vars.get(key):
                f.write(f"{key}={env_vars[key]}\n")
            else:
                f.write(f"# {key}=\n")

        f.write("\n# Secondary Tier (escalation)\n")
        for key in ["SECONDARY_BASE_URL", "SECONDARY_API_KEY", "SECONDARY_AUTH_TOKEN", "SECONDARY_MODEL"]:
            if env_vars.get(key):
                f.write(f"{key}={env_vars[key]}\n")
            else:
                f.write(f"# {key}=\n")
        
        f.write("\n# Pipeline Flags\n")
        for key in ["AUTOFIX", "EXPLORATION", "RECURSE", "NO_BYPASS", "SILENT", "RECORDING", "SAVE_SPECIFICATION", "SAVE_SEMIFORMALIZATION"]:
            if env_vars.get(key):
                f.write(f"{key}={env_vars[key]}\n")
            else:
                f.write(f"# {key}=\n")

        f.write("\n# Budget Configuration (USD)\n")
        for key in ["GENERATION_BUDGET", "SEMIFORMALIZATION_BUDGET", "EXPLORATION_BUDGET",
                    "SOURCE_SCAN_BUDGET", "PREPARATION_BUDGET", "FORMALIZATION_BUDGET",
                    "CRITIC_BUDGET", "VALIDATION_BUDGET"]:
            f.write(f"{key}={env_vars[key]}\n")

        f.write("\n# Iteration / Retry Limits\n")
        for key in ["MAX_CRITIC_ITERATIONS", "MAX_VALIDATION_ITERATIONS", "RESOLVER_MAX_RETRIES"]:
            if env_vars.get(key):
                f.write(f"{key}={env_vars[key]}\n")
            else:
                f.write(f"# {key}=\n")

        f.write("\n# Server Configuration\n")
        f.write(f"FORUM_PORT={env_vars['FORUM_PORT']}\n")

        f.write("\n# Experimental Features\n")
        if env_vars.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"):
            f.write(f"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS={env_vars['CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS']}\n")
        else:
            f.write("# CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=\n")
    
    print(f"\n✓ Configuration saved to {output_file.absolute()}")
    print("\nYou can now run the pipeline with:")
    print("  unity --source <source_file> --project <project_dir>\n")
