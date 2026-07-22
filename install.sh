#!/bin/sh
# Unity installer — multi-agent autoformalization for Lean 4.
#   curl -fsSL https://raw.githubusercontent.com/sosudo/unity/main/install.sh | sh
set -eu

REPO="https://github.com/sosudo/unity"

say()  { printf '\033[1m[unity]\033[0m %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ask "question" — returns 0 (yes) / 1 (no); auto-yes when not interactive (curl | sh)
ask() {
  if [ -r /dev/tty ]; then
    printf '\033[1m[unity]\033[0m %s [Y/n] ' "$1"
    read -r a < /dev/tty || a=""
    case "$a" in n|N|no|NO) return 1 ;; *) return 0 ;; esac
  fi
  return 0
}

# ── dependency checks ─────────────────────────────────────────────────────────
have git  || { say "git is required — install it; and re-run"; exit 1; }
have curl || { say "curl is required — install it and re-run"; exit 1; }

if ! have uv; then
  if ask "uv (python package manager) is missing — install it from astral.sh?"; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  else
    say "uv is required: https://docs.astral.sh/uv/"; exit 1
  fi
fi
have uv || { say "uv installation failed — install it manually and re-run"; exit 1; }

if ! have lake && ! have elan; then
  if ask "Lean 4 (elan/lake) is missing — install elan (the Lean toolchain manager)?"; then
    curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | sh -s -- -y
    export PATH="$HOME/.elan/bin:$PATH"
    have lake && say "elan installed (Lean toolchains download per-project on first build)"
  else
    say "note: unity needs Lean 4 + lake at run time — https://lean-lang.org/install/"
  fi
fi

# ── unity itself ──────────────────────────────────────────────────────────────
say "installing unity from $REPO"
uv tool install --force --prerelease=allow "git+$REPO"

if ! have unity; then
  say "installed, but 'unity' is not on your PATH yet — run: uv tool update-shell, then restart your shell"
else
  say "installed: unity $(unity version 2>/dev/null || echo '')"
fi

say "get started:"
say "  unity new myproj --math   # new Lean project (or: cd <your project> && unity init)"
say "  cd myproj && unity serve  # the control center: http://localhost:8080"
say "  agents tab -> add your models (presets included), prompt tab -> your goal, then hit run"
