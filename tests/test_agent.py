import json
from types import SimpleNamespace

import pytest

import agent
from agent_ui import app as ui
from agent_ui.validation import ReportValidationError, validate_report

REPORT = """## Business Model Summary
Company exists [ev_001].
## Competition (Structured)
Unknown.
## Company Quality (Structured)
Unknown.
## Explicit Citation Block
- ev_001 -> https://example.com/company
## Uncertainty and Limitations
Limited data.
"""
EVIDENCE = {
    "evidence": [
        {
            "evidence_id": "ev_001",
            "claim": "Company exists",
            "source": {"url": "https://example.com/company", "kind": "companies_house_api"},
            "verbatim_excerpt": "",
            "structured_fact": {},
            "confidence": 1.0,
        }
    ],
    "uncertainty": [],
}


def state():
    return {"competition": "{}", "quality_signals": "{}", "evidence": json.dumps(EVIDENCE)}


def test_report_validation_accepts_valid_citations():
    validate_report(REPORT, state())


def test_report_validation_accepts_json_fences_without_weakening_citations():
    data = state()
    for key in data:
        data[key] = "```json\n" + data[key] + "\n```"
    validate_report(REPORT, data)
    with pytest.raises(ReportValidationError, match="unknown or uncited"):
        validate_report(REPORT.replace("ev_001", "ev_0001"), data)


@pytest.mark.parametrize(
    "value", ["Here is JSON: {}", "```json\n{}\n```\nExtra text", "```json\n{broken}\n```"]
)
def test_research_json_rejects_prose_and_malformed_content(value):
    data = state()
    data["competition"] = value
    with pytest.raises(ReportValidationError):
        validate_report(REPORT, data)


def test_validation_error_is_actionable_and_does_not_expose_model_content():
    error = ui._http_exception_for_agent_error(ReportValidationError("private model content"))
    assert error.status_code == 502
    assert error.detail["code"] == "invalid_model_report"
    assert "private model content" not in json.dumps(error.detail)


@pytest.mark.asyncio
async def test_favicon_has_no_body_for_no_content_status():
    response = await ui.favicon()
    assert response.status_code == 204
    assert response.body == b""


@pytest.mark.parametrize(
    "report",
    [
        REPORT.replace("ev_001", "ev_999"),
        REPORT.replace("https://example.com/company", "https://invented.example/"),
        REPORT.replace("## Uncertainty and Limitations", "Missing heading"),
    ],
)
def test_invalid_report_rejected(report):
    with pytest.raises(ValueError):
        validate_report(report, state())


def test_malformed_evidence_rejected():
    invalid = state()
    invalid["evidence"] = "Not JSON"
    with pytest.raises(ValueError):
        validate_report(REPORT, invalid)


def test_approved_workflow_skips_entity_selection():
    root = agent.build_root_agent(approved_context=True)
    assert [a.name for a in root.sub_agents] == [
        "ParallelResearch",
        "EvidenceExtractor",
        "UnderwritingSynthesizer",
    ]


def test_mcp_auth_forwarded(monkeypatch):
    monkeypatch.setenv("CH_MCP_AUTH", "private-token")
    toolset = agent.companies_house_toolset(["advanced_company_search"])
    assert toolset._connection_params.headers["Authorization"] == "Bearer private-token"


@pytest.mark.asyncio
async def test_local_model_timeout_does_not_replay_cpu_work(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemma")
    calls = []

    async def timed_out(*args):
        calls.append(True)
        raise TimeoutError("Model still computing")

    monkeypatch.setattr(ui, "run_agent_turn", timed_out)
    with pytest.raises(TimeoutError):
        await ui.run_agent_turn_with_retries({"company_number": "00519500"}, "test")
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("final_author", ["UnderwritingSynthesizer", "EvidenceExtractor"])
async def test_runtime_uses_approved_number_and_requires_synthesizer(monkeypatch, final_author):
    calls, runs = [], []

    async def registry(name, args):
        calls.append((name, args))
        return {
            "company_number": "12345678",
            "profile": {"company_number": "12345678", "company_name": "Real Name"},
        }

    monkeypatch.setattr(ui, "call_registry_tool", registry)

    class FakeRunner:
        def __init__(self, agent, app_name, session_service):
            self.service = session_service
            self.closed = False
            runs.append(self)

        async def run_async(self, user_id, session_id, **kwargs):
            self.sid = session_id
            self.session = await self.service.get_session(
                app_name=ui.APP_NAME, user_id=user_id, session_id=session_id
            )
            assert json.loads(self.session.state["company_context"])["company_name"] == "Real Name"
            # Validation is tested separately; mimic a completed pipeline here.
            yield SimpleNamespace(
                author=final_author,
                partial=False,
                is_final_response=lambda: True,
                content=SimpleNamespace(parts=[SimpleNamespace(text=REPORT)]),
            )

        async def close(self):
            self.closed = True

    monkeypatch.setattr(ui, "Runner", FakeRunner)
    monkeypatch.setattr(ui, "validate_report", lambda *args: None)
    approved = {"company_number": "12345678", "company_name": "Spoofed"}
    if final_author == "UnderwritingSynthesizer":
        assert await ui.run_agent_turn(approved, "Research another company") == REPORT
    else:
        with pytest.raises(RuntimeError, match="synthesizer"):
            await ui.run_agent_turn(approved, "Research another company")
    assert calls == [("underwriting_pack", {"company_number": "12345678"})]
    assert runs[0].closed
    assert (
        await runs[0].service.get_session(
            app_name=ui.APP_NAME, user_id=ui.USER_ID, session_id=runs[0].sid
        )
        is None
    )
