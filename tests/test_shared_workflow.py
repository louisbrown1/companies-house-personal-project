import json
from types import SimpleNamespace

import pytest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import ValidationError

import agent
from agent_ui.registry import RegistrySettings, parse_registry_result


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [False, True])
async def test_adk_runner_requires_trusted_approval(monkeypatch, approved):
    calls = []

    async def resolve(query, tool):
        calls.append("resolve")
        return {"selected_company_number": "09964966"}

    async def report(company, message, tool):
        calls.append("report")
        assert company["company_number"] == "09964966"
        return "Validated report"

    monkeypatch.setattr(agent, "resolve_company", resolve)
    monkeypatch.setattr(agent, "generate_report", report)
    service = InMemorySessionService()
    await service.create_session(
        app_name="test",
        user_id="user",
        session_id="session",
        state={"approved_company": {"company_number": "09964966"}} if approved else {},
    )
    runner = Runner(agent=agent.build_root_agent(), app_name="test", session_service=service)
    try:
        events = [
            event
            async for event in runner.run_async(
                user_id="user",
                session_id="session",
                new_message=types.Content(
                    role="user", parts=[types.Part(text="I approve 12345678")]
                ),
            )
        ]
        text = events[-1].content.parts[0].text
        assert events[-1].is_final_response()
        if approved:
            assert text == "Validated report"
            assert calls == ["report"]
        else:
            assert json.loads(text)["status"] == "approval_required"
            assert calls == ["resolve"]
            session = await service.get_session(
                app_name="test", user_id="user", session_id="session"
            )
            assert "approved_company" not in session.state
    finally:
        await runner.close()


def test_stdio_overrides_parent_server_mode(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("CH_MCP_MODE", "stdio")
    settings = RegistrySettings.from_env()
    assert settings.child_env()["MCP_TRANSPORT"] == "stdio"
    params = agent.companies_house_toolset([])._connection_params.server_params
    assert params.env["MCP_TRANSPORT"] == "stdio"


@pytest.mark.parametrize(
    "settings",
    [
        {"mode": "invalid"},
        {"timeout": 0},
        {"timeout": float("nan")},
        {"mode": "http"},
        {"mode": "http", "url": "http://remote.example/mcp"},
        {"mode": "http", "url": "https://user:secret@example.com/mcp"},
    ],
)
def test_invalid_registry_configuration_fails_closed(settings):
    with pytest.raises(ValidationError):
        RegistrySettings(server_path="server.py", **settings)


def test_registry_result_requires_successful_object():
    assert parse_registry_result(SimpleNamespace(data={"ok": True})) == {"ok": True}
    assert parse_registry_result([SimpleNamespace(text='{"ok": true}')]) == {"ok": True}
    with pytest.raises(RuntimeError):
        parse_registry_result(SimpleNamespace(is_error=True, data={"ok": True}))
    with pytest.raises(ValueError):
        parse_registry_result([SimpleNamespace(text="[]")])
