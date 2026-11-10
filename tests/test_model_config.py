import json
from types import SimpleNamespace

import pytest

from agent_ui.model_config import agent_model, model_provider, unavailable_web_research


def test_default_models(monkeypatch):
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    assert agent_model() == "gemini-2.5-flash"
    assert agent_model(synthesis=True) == "gemini-2.5-pro"


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


def test_no_search_records_gap_without_model_call():
    context = SimpleNamespace(state={})
    result = unavailable_web_research(context)
    evidence = json.loads(context.state["quality_signals"])
    assert evidence["review_sources"] == []
    assert evidence["uncertainty"]
    assert result.parts[0].text == context.state["quality_signals"]


def test_gemma_pipeline_has_no_gemini_models_or_search(monkeypatch):
    pytest.importorskip("litellm")
    from google.adk.agents.llm_agent import LlmAgent

    from agent import build_root_agent

    monkeypatch.setenv("MODEL_PROVIDER", "gemma")
    monkeypatch.setenv("GEMMA_API_BASE", "http://127.0.0.1:9379/v1")
    monkeypatch.setenv("GEMMA_MODEL", "gemma4-12b")
    root = build_root_agent(approved_context=True)

    def visit(agent):
        if isinstance(agent, LlmAgent):
            assert agent.model.model == "openai/gemma4-12b"
            if agent.name == "ReviewHarvester":
                assert agent.tools == []
                assert agent.before_agent_callback is unavailable_web_research
        for child in agent.sub_agents:
            visit(child)

    visit(root)
