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


def is_transient_provider_failure(exc: BaseException) -> bool:
    """Retry only identified transport failures, never native/controller policy."""
    if not isinstance(exc, BumpProviderFailure) or exc.provider == "native_mcp":
        return False
    if exc.category == "rate_limited":
        return exc.http_status in {None, 429}
    if exc.category == "provider_unavailable":
        return exc.http_status in {None, 500, 502, 503, 504}
    if exc.category == "transport_timeout":
        return exc.http_status in {None, 408}
    return (exc.category in {"transport_connection_failed", "transport_disconnected"}
            and exc.http_status is None)


class BumpTransportRetriesExhausted(BumpProviderFailure):
    """One worker exhausted transport retries; this is not a proof rejection."""

    def __init__(self, failure: BumpProviderFailure, attempts: int):
        if not is_transient_provider_failure(failure):
            raise ValueError("transport exhaustion requires a transient provider failure")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
            raise ValueError("transport attempts must be a positive integer")
        super().__init__(failure.category, provider=failure.provider, status=failure.http_status)
        self.attempts = attempts
        self.args = (f"{self.args[0]}; transport retries exhausted after {attempts} attempts",)


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
    if isinstance(error, BumpProviderFailure):
        return error
    messages, codes, statuses, transport_kinds = [], set(), set(), set()

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
                if key in {"httpConnectionFailed", "responseStreamConnectionFailed", "responseStreamDisconnected"}:
                    transport_kinds.add(key)
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
    elif len(statuses) > 1:
        # Conflicting transport evidence must not hide an authentication or
        # other permanent failure behind a retryable status/code.
        category = "ambiguous_http_status"
    elif status in {401, 403}:
        category = "authentication_or_access_denied"
    elif status == 429 or (status is None and "rate_limited" in codes):
        category = "rate_limited"
    elif status == 408:
        category = "transport_timeout"
    elif status is not None and status >= 500:
        category = "provider_unavailable"
    elif status is None:
        # Recognize only our sanitized transport codes and SDK error variants;
        # free-form prose does not grant permission to retry a failed controller.
        if "provider_unavailable" in codes:
            category = "provider_unavailable"
        elif "responseStreamDisconnected" in transport_kinds or "transport_disconnected" in codes:
            category = "transport_disconnected"
        elif transport_kinds or "transport_connection_failed" in codes:
            category = "transport_connection_failed"
        elif "transport_timeout" in codes or isinstance(error, TimeoutError):
            category = "transport_timeout"
        elif isinstance(error, ConnectionError):
            category = "transport_connection_failed"
        else:
            import httpx
            if isinstance(error, httpx.TimeoutException):
                category = "transport_timeout"
            elif isinstance(error, httpx.NetworkError):
                category = "transport_connection_failed"
    return BumpProviderFailure(category, provider=provider, status=status)
