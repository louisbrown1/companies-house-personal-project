import pytest

from agent_ui.model_config import agent_model, model_provider


def test_default_models(monkeypatch):
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    assert agent_model().model == "gemini-2.5-flash"
    assert agent_model(synthesis=True).model == "gemini-2.5-pro"


def test_invalid_provider_fails_closed(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemmma")
    with pytest.raises(ValueError):
        model_provider()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com/v1",
        "http://user:password@localhost/v1",
        "http://localhost/v1?key=x",
        "http://localhost",
    ],
)
def test_unsafe_gemma_endpoint_rejected(monkeypatch, endpoint):
    monkeypatch.setenv("MODEL_PROVIDER", "gemma")
    monkeypatch.setenv("GEMMA_API_BASE", endpoint)
    with pytest.raises(ValueError):
        agent_model()


def test_gemma_model_uses_local_adapter(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemma")
    monkeypatch.setenv("GEMMA_API_BASE", "http://127.0.0.1:9379/v1")
    monkeypatch.setenv("GEMMA_MODEL", "gemma4-e2b")
    assert agent_model().model == "openai/gemma4-e2b"
