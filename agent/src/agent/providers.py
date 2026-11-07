"""LangChain chat model for the 9Router gateway.

9Router is a self-hosted, OpenAI-compatible gateway. It fronts several
upstream providers and routes between them, either by exact model id or
through a named "combo" (an ordered fallback chain). From LangChain's point of
view it is just an OpenAI-compatible chat completion endpoint, so one
`ChatOpenAI` instance covers every model and every combo.

Configuration comes from the environment:

    NINEROUTER_BASE_URL  gateway endpoint, default http://localhost:20128/v1
    NINEROUTER_API_KEY   gateway Bearer key
    NINEROUTER_MODEL     model id or combo name, default "mamazon"

The model is sent exactly as configured — 9Router resolves the combo itself.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import config

logger = logging.getLogger(__name__)


def build_chat_model(provider: str | None = None, **overrides: Any) -> Any:
    """Return a LangChain chat model pointed at the 9Router gateway.

    Raises RuntimeError when the gateway is not configured, so the caller can
    fall back to the deterministic answer instead of failing.
    """
    selected = (provider or config.AI_PROVIDER).lower()
    if selected not in {"9router", "ninerouter", "9r"}:
        raise RuntimeError(f"Unsupported AI provider for the agent: {selected}")

    if not config.NINEROUTER_API_KEY:
        raise RuntimeError(
            "NINEROUTER_API_KEY is empty — set the 9Router gateway key"
        )
    if not config.NINEROUTER_MODEL:
        raise RuntimeError(
            "NINEROUTER_MODEL is empty — set a model id or a 9Router combo name"
        )

    from langchain_openai import ChatOpenAI
    from ..logging_config import AIChatLoggingCallbackHandler

    kwargs: dict[str, Any] = {
        "base_url": config.effective_ninerouter_base_url(),
        "api_key": config.NINEROUTER_API_KEY,
        "model": config.NINEROUTER_MODEL,
        "timeout": config.NINEROUTER_TIMEOUT,
        "max_tokens": config.NINEROUTER_MAX_TOKENS,
        "temperature": 0.2,
        "default_headers": _headers(),
        "max_retries": 1,
    }
    if config.NINEROUTER_REASONING_EFFORT:
        # Reasoning models need the effort set or they spend the whole token
        # budget thinking and return an empty completion.
        kwargs["reasoning_effort"] = config.NINEROUTER_REASONING_EFFORT
    kwargs.update(overrides)

    callbacks = list(kwargs.get("callbacks") or [])
    if AIChatLoggingCallbackHandler is not None and not any(
        isinstance(cb, AIChatLoggingCallbackHandler) for cb in callbacks
    ):
        callbacks.append(AIChatLoggingCallbackHandler())
    kwargs["callbacks"] = callbacks

    return ChatOpenAI(**kwargs)


def _headers() -> dict[str, str]:
    """Request headers sent to the gateway."""
    headers = {"User-Agent": config.NINEROUTER_USER_AGENT}
    if config.NINEROUTER_SITE_URL:
        headers["HTTP-Referer"] = config.NINEROUTER_SITE_URL
    if config.NINEROUTER_APP_NAME:
        headers["X-9Router-Title"] = config.NINEROUTER_APP_NAME
    return headers


def provider_label(provider: str | None = None) -> str:
    """Human-readable "provider:model" label for audit logs and status."""
    selected = (provider or config.AI_PROVIDER).lower()
    return f"{selected}:{config.NINEROUTER_MODEL}"


def supports_native_tools(provider: str | None = None) -> bool:
    """True when the backend speaks the OpenAI tool-call protocol.

    9Router is OpenAI-compatible, so tool calls work natively.
    """
    selected = (provider or config.AI_PROVIDER).lower()
    return selected in {"9router", "ninerouter", "9r"}


def list_models(base_url: str | None = None, api_key: str | None = None) -> list[dict]:
    """List the models and combos the gateway exposes.

    Returns one dict per entry with `id`, `owned_by` ("combo" for a named
    fallback chain) and the gateway's capability flags.
    """
    import json
    import urllib.request

    base = (base_url or config.effective_ninerouter_base_url()).rstrip("/")
    key = api_key if api_key is not None else config.NINEROUTER_API_KEY
    headers = {"Accept": "application/json", "User-Agent": config.NINEROUTER_USER_AGENT}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(f"{base}/models", headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    entries: list[dict] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        capabilities = item.get("capabilities") or {}
        entries.append(
            {
                "id": str(item["id"]),
                "owned_by": str(item.get("owned_by") or ""),
                "combo": item.get("owned_by") == "combo",
                "vision": bool(capabilities.get("vision")),
                "tools": bool(capabilities.get("tools")),
                "context_length": item.get("context_length"),
            }
        )
    return entries
