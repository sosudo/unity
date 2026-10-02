"""Credential-safe Bump provider failures, never an automatic model switch.

Only trusted transport errors enter this module, not model prose, tool output or
saved project files. A credit classification lets an authorized controller make
a fresh-run decision; it never changes a live roster, permissions or budgets.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from urllib.parse import urlsplit


class BumpProviderFailure(RuntimeError):
    """An unsuccessful model turn, with deliberately bounded public evidence."""

    def __init__(self, category: str, *, provider: str, status: int | None = None):
        self.category = category
        self.provider = provider
        self.http_status = status
        suffix = f"; HTTP {status}" if status is not None else ""
        super().__init__(f"Bump model backend failed: {category} ({provider}{suffix})")


def is_freeinference(agent) -> bool:
    """Match the configured provider identity, never a substring in error text."""
    try:
        url = urlsplit(agent.base_url or "")
        return (agent.backend == "codex" and url.scheme == "https"
                and url.hostname == "freeinference.org" and url.port in (None, 443)
                and url.path.rstrip("/") == "/v1" and not url.username
                and not url.password and not url.query and not url.fragment)
    except (ValueError, AttributeError):
        return False


def _mapping(value):
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump(mode="json", by_alias=True)
        if isinstance(result, Mapping):
            return result
    return {key: getattr(value, key) for key in (
        "message", "code", "type", "data", "error", "status_code", "http_status_code",
        "httpStatusCode", "codex_error_info", "additional_details",
    ) if hasattr(value, key)}


def provider_failure(agent, error=None, *, category="unsuccessful_turn") -> BumpProviderFailure:
    """Classify a transport failure without retaining raw headers/messages.

    Generic quota or rate-limit errors do not prove spent FreeInference credits.
    Require a precise credit/balance signal from the provider error itself. Even
    this classification grants no authority to purchase, reset or change models.
    """
    messages, codes, statuses = [], set(), set()

    def visit(value, depth=0):
        if depth > 6:
            return
        for key, item in _mapping(value).items():
            if key in {"message", "additional_details", "additionalDetails"} and isinstance(item, str):
                messages.append(item[:16000].lower())
            elif key in {"code", "type"} and isinstance(item, str):
                codes.add(item.lower())
            elif key in {"status_code", "http_status_code", "httpStatusCode"}:
                if isinstance(item, int) and not isinstance(item, bool) and 100 <= item <= 599:
                    statuses.add(item)
            elif key in {"error", "data", "codex_error_info", "codexErrorInfo", "root",
                         "httpConnectionFailed", "responseStreamConnectionFailed",
                         "responseStreamDisconnected", "responseTooManyFailedAttempts"}:
                visit(item, depth + 1)

    visit(error)
    # Some SDK exceptions expose only their bounded message attribute. Do not
    # serialize arbitrary exception reprs, request objects or response headers.
    text = "\n".join(messages)
    status = next(iter(statuses)) if len(statuses) == 1 else None
    provider = "freeinference" if is_freeinference(agent) else "configured_provider"
    exhausted = bool(codes & {
        "insufficient_credits", "credits_exhausted", "credit_exhausted",
        "insufficient_balance", "balance_exhausted", "not_enough_credits",
    }) or bool(re.search(
        r"\b(?:insufficient|exhausted|depleted|no remaining|not enough) (?:api )?credits?\b"
        r"|\bcredits? (?:balance )?(?:is |are |has been )?(?:exhausted|depleted|insufficient)\b"
        r"|\binsufficient (?:account )?balance\b", text))
    if provider == "freeinference" and exhausted:
        category = "freeinference_credit_exhausted"
    elif status in {401, 403}:
        category = "authentication_or_access_denied"
    elif status == 429:
        category = "rate_limited"
    elif status is not None and status >= 500:
        category = "provider_unavailable"
    return BumpProviderFailure(category, provider=provider, status=status)
