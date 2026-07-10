#!/bin/sh
# Unity installer — multi-agent autoformalization for Lean 4.
#   curl -fsSL https://raw.githubusercontent.com/sosudo/unity/main/install.sh | sh
set -eu

REPO="https://github.com/sosudo/unity"

say() { printf '\033[1m[unity]\033[0m %s\n' "$1"; }

# 1. uv (the installer/runtime for the tool)
if ! command -v uv >/dev/null 2>&1; then
  say "uv not found — installing it first (astral.sh)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # pick up uv from its default install locations for the rest of this script
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || { say "uv installation failed — install it manually from https://docs.astral.sh/uv/ and re-run"; exit 1; }

# 2. unity itself
say "installing unity from $REPO"
uv tool install --force "git+$REPO"

# 3. PATH sanity
if ! command -v unity >/dev/null 2>&1; then
  say "installed, but 'unity' is not on your PATH yet — run: uv tool update-shell, then restart your shell"
else
  say "installed: $(unity version 2>/dev/null || echo unity)"
fi

say "next steps:"
say "  cd <your Lean project> && unity init   # set up .unity/ (roster, prompt, sources)"
say "  unity help                             # commands: prove, solve, formalize, create, ..."
say "  unity serve                            # web control center at http://localhost:8080"
