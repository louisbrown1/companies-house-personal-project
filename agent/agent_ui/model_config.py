"""Explicit model selection for Gemini or a self-hosted Gemma endpoint."""

import os
from urllib.parse import urlsplit


def model_provider():
    provider = os.getenv("MODEL_PROVIDER", "gemini").lower().strip()
    if provider not in {"gemini", "gemma"}:
        raise ValueError("MODEL_PROVIDER must be gemini or gemma")
    return provider


def agent_model(*, synthesis=False):
    if model_provider() == "gemini":
        from google.adk.models.google_llm import Gemini

        return Gemini(model="gemini-2.5-pro" if synthesis else "gemini-2.5-flash")
    base = os.getenv("GEMMA_API_BASE", "http://127.0.0.1:9379/v1").rstrip("/")
    url = urlsplit(base)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path != "/v1"
    ):
        raise ValueError(
            "GEMMA_API_BASE must be an HTTP(S) endpoint ending in /v1 without credentials"
        )
    if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Use HTTPS for remote Gemma, or a localhost SSH tunnel")
    model = os.getenv("GEMMA_MODEL", "gemma4-12b").strip()
    if not model:
        raise ValueError("GEMMA_MODEL must not be empty")
    # Local inference does not need LiteLLM's remote pricing catalogue.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    from google.adk.models.lite_llm import LiteLlm

    return LiteLlm(
        model=f"openai/{model}",
        api_base=base,
        api_key=os.getenv("GEMMA_API_KEY") or "local-not-required",
        # ADK executes MCP tools and sends ordinary function schemas.
        # LiteLLM 1.108.2 otherwise imports its unused MCP 2.x gateway,
        # which conflicts with this application's MCP 1.x stack.
        _skip_mcp_handler=True,
        reasoning_effort="none",
        timeout=600,
        num_retries=0,
    )
