"""Seed private Claude sessions without modifying the user's credentials.

Prefer explicit authentication; otherwise reuse only an unexpired access token.
The macOS helper disables Keychain UI without changing the controller's policy.
Storage format/service names match Claude Code 2.1.105. No refresh or ACL bypass.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import pwd
import stat
import subprocess
import sys
import time
import unicodedata
from pathlib import Path


class ClaudeAuthUnavailable(RuntimeError):
    """Authentication cannot be reused without interaction or shared writes."""


_AUTH_KEYS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
_GUIDANCE = (
    "Cannot reuse a current Claude subscription login without interaction. "
    "Refresh your login in Claude Code, or run `claude setup-token` yourself and "
    "configure CLAUDE_CODE_OAUTH_TOKEN for unattended runs. "
    "No shared credentials were changed."
)


def _keychain_service(environ: dict[str, str]) -> str:
    suffix = "-custom-oauth" if environ.get("CLAUDE_CODE_CUSTOM_OAUTH_URL") else ""
    service = f"Claude Code{suffix}-credentials"
    configured = environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        # Match the CLI's exact NFC-normalized configured string; resolving a
        # path or expanding '~' would select a different Keychain item.
        normalized = unicodedata.normalize("NFC", configured)
        service += "-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return service


def _access_token(payload: bytes | str, *, now: float | None = None) -> str:
    try:
        value = json.loads(payload)
        oauth = value["claudeAiOauth"]
        token = oauth["accessToken"]
        expires = oauth["expiresAt"]
        if not isinstance(token, str) or not token.strip() or any(c in token for c in "\x00\r\n"):
            raise ValueError
        if (type(expires) not in (int, float) or not math.isfinite(expires)
                or expires <= (time.time() if now is None else now) * 1000):
            raise ClaudeAuthUnavailable("Stored Claude subscription token is expired or has no valid expiry. " + _GUIDANCE)
        return token
    except ClaudeAuthUnavailable:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise ClaudeAuthUnavailable("Stored Claude subscription credential has an unsupported shape. " + _GUIDANCE) from None


def _read_plaintext(config_dir: Path) -> bytes | None:
    try:
        fd = os.open(config_dir / ".credentials.json", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return None
            data = handle.read(1024 * 1024 + 1)
            return data if len(data) <= 1024 * 1024 else None
    except OSError:
        return None


def _keychain_read_without_ui(service: str, account: str) -> bytes | None:
    """Only call inside the dedicated helper: UI policy is process-global."""
    framework = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    set_ui = framework.SecKeychainSetUserInteractionAllowed
    set_ui.argtypes = [ctypes.c_ubyte]
    set_ui.restype = ctypes.c_int32
    if set_ui(0) != 0:
        return None
    find = framework.SecKeychainFindGenericPassword
    find.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
                     ctypes.c_uint32, ctypes.c_char_p,
                     ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    find.restype = ctypes.c_int32
    free = framework.SecKeychainItemFreeContent
    free.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    free.restype = ctypes.c_int32
    service_bytes, account_bytes = service.encode("utf-8"), account.encode("utf-8")
    length, data = ctypes.c_uint32(), ctypes.c_void_p()
    status = find(None, len(service_bytes), service_bytes, len(account_bytes), account_bytes,
                  ctypes.byref(length), ctypes.byref(data), None)
    if status != 0 or not data.value:
        return None
    try:
        if length.value > 1024 * 1024:
            return None
        return ctypes.string_at(data, length.value)
    finally:
        free(None, data)


def _read_keychain(environ: dict[str, str]) -> str | None:
    # No token enters argv, a file, logs, or the controller's stdout. The only
    # output is a private captured pipe from a no-UI, bounded helper process.
    account = environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-m", "unity.autoformalize_claude_auth",
             "--keychain-read", _keychain_service(environ), account],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            close_fds=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0 and len(result.stdout) <= 1024 * 1024:
        return result.stdout.decode("utf-8") or None  # Already validated by the helper.
    return None


def _stored_access_token(environ: dict[str, str]) -> str:
    # This newer override is not present in the pinned CLI we audited; avoid
    # guessing another installed version's credential-store derivation.
    if environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR") is not None:
        raise ClaudeAuthUnavailable("Independent Claude credential-store overrides need explicit environment authentication. " + _GUIDANCE)
    if sys.platform not in {"darwin", "linux"}:
        raise ClaudeAuthUnavailable(_GUIDANCE)
    if sys.platform == "darwin" and (token := _read_keychain(environ)):
        return token
    configured = environ.get("CLAUDE_CONFIG_DIR")
    config_dir = Path(configured) if configured else Path.home() / ".claude"
    if config_dir.is_absolute():
        payload = _read_plaintext(config_dir)
        if payload:
            return _access_token(payload)
    raise ClaudeAuthUnavailable(_GUIDANCE)


def seed_claude_session(directory: Path, agent_env: dict[str, str]) -> dict[str, str]:
    """Return child-only env overrides using DIRECTORY as its private config.

    Resolve source authentication before setting CLAUDE_CONFIG_DIR. The caller
    must include directory in the child's private writable sandbox roots and
    must never log this returned dictionary.
    """
    directory = Path(directory)
    if not directory.is_absolute():
        raise ValueError("private Claude runtime directory must be absolute")
    source = {**os.environ, **agent_env}
    result = dict(agent_env)
    # A roster's FI/API credentials must not accidentally pick up the parent's
    # unrelated Claude subscription token when the SDK merges child env.
    explicit = {key: agent_env[key] for key in _AUTH_KEYS if agent_env.get(key, "").strip()}
    if explicit:
        result.update({key: "" for key in _AUTH_KEYS if key not in explicit})
    else:
        explicit = {key: source[key] for key in _AUTH_KEYS if source.get(key, "").strip()}
    if explicit:
        result.update(explicit)
    else:
        result["CLAUDE_CODE_OAUTH_TOKEN"] = _stored_access_token(source)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("tmp", "debug"):
        (directory / name).mkdir(mode=0o700, exist_ok=True)
    result.update({
        "CLAUDE_CONFIG_DIR": str(directory),
        "CLAUDE_CODE_TMPDIR": str(directory / "tmp"),
        "TMPDIR": str(directory / "tmp"),
        "TMP": str(directory / "tmp"),
        "TEMP": str(directory / "tmp"),
        "CLAUDE_CODE_DEBUG_LOGS_DIR": str(directory / "debug"),
        "DISABLE_AUTOUPDATER": "1",
    })
    return result


def _main(argv: list[str] | None = None) -> int:
    """Private parent/child IPC; never display credential data in a terminal."""
    args = sys.argv[1:] if argv is None else argv
    if (sys.platform != "darwin" or len(args) != 3 or args[0] != "--keychain-read"
            or sys.stdout.isatty()):
        return 1
    try:
        payload = _keychain_read_without_ui(args[1], args[2])
        if payload is None:
            return 1
        # Send only the validated access token; do not expose refresh tokens or
        # unrelated MCP secrets from the same Keychain JSON object.
        sys.stdout.write(_access_token(payload))
        return 0
    except (OSError, ValueError, AttributeError, ClaudeAuthUnavailable):
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
