import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import ch_mcp_server as registry
from agent_ui import app as ui
from agent_ui.state import StateConflict, StateStore

SID = "test-session-12345678"
A = {"company_number": "12345678", "title": "Company A"}
B = {"company_number": "87654321", "title": "Company B"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = StateStore("sqlite:///" + str(tmp_path / "state.db"))
    monkeypatch.setattr(ui, "store", store)
    monkeypatch.setattr(ui, "AGENT_UI_TOKEN", None)

    async def resolve(query):
        return {"candidates": [A, B]}

    monkeypatch.setattr(ui, "resolve_company_via_mcp", resolve)
    yield store
    store.engine.dispose()


def approve(client, company=A):
    assert (
        client.post(
            "/api/resolve-company", json={"session_id": SID, "query": "company"}
        ).status_code
        == 200
    )
    return client.post(
        "/api/approve-company",
        json={
            "session_id": SID,
            "company_number": company["company_number"],
            "company_name": "Spoofed",
        },
    )


def test_approval_requires_resolution_and_ignores_client_name(setup):
    with TestClient(ui.app) as client:
        assert (
            client.post(
                "/api/approve-company",
                json={"session_id": SID, "company_number": A["company_number"]},
            ).status_code
            == 400
        )
        result = approve(client)
        assert result.json()["approved_company"]["company_name"] == "Company A"


def test_sessions_belong_to_browser(setup):
    with TestClient(ui.app) as first, TestClient(ui.app) as other:
        approve(first)
        assert other.post("/api/reset-session", json={"session_id": SID}).status_code == 409
        assert other.get("/api/report-pdf", params={"session_id": SID}).status_code == 409
        assert other.get("/api/report-status", params={"session_id": SID}).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_background_report_returns_immediately_and_persists_outcome(setup, monkeypatch, fail):
    request = SimpleNamespace(state=SimpleNamespace(owner="owner"), headers={})
    await ui.save_state(
        SID,
        request,
        None,
        approved={"company_number": A["company_number"], "company_name": A["title"]},
    )
    finish = asyncio.Event()

    async def run(*args):
        await finish.wait()
        if fail:
            raise TimeoutError()
        return "completed after submission disconnected"

    monkeypatch.setattr(ui, "run_agent_turn_with_retries", run)
    response = await ui.generate_report(
        ui.GenerateReportRequest(session_id=SID, background=True), request
    )
    assert response.status_code == 202
    assert (await ui.report_status(request, SID))["status"] == "running"
    with pytest.raises(ui.HTTPException) as exc:
        await ui.generate_report(ui.GenerateReportRequest(session_id=SID, background=True), request)
    assert exc.value.status_code == 409
    tasks = list(ui.report_tasks)
    finish.set()
    await asyncio.gather(*tasks)
    status = await ui.report_status(request, SID)
    assert status["status"] == ("failed" if fail else "completed")
    if fail:
        assert "time limit" in status["error"]
    else:
        assert status["report"]["response"] == "completed after submission disconnected"


@pytest.mark.asyncio
async def test_background_report_cannot_restore_reset_session(setup, monkeypatch):
    request = SimpleNamespace(state=SimpleNamespace(owner="owner"), headers={})
    await ui.save_state(
        SID,
        request,
        None,
        approved={"company_number": A["company_number"], "company_name": A["title"]},
    )
    finish = asyncio.Event()

    async def run(*args):
        await finish.wait()
        return "stale report"

    monkeypatch.setattr(ui, "run_agent_turn_with_retries", run)
    await ui.generate_report(ui.GenerateReportRequest(session_id=SID, background=True), request)
    tasks = list(ui.report_tasks)
    await ui.reset_session(ui.SessionRequest(session_id=SID), request)
    finish.set()
    await asyncio.gather(*tasks)
    assert (await ui.report_status(request, SID))["status"] == "idle"


def test_token_and_pdf(setup, monkeypatch):
    monkeypatch.setattr(ui, "AGENT_UI_TOKEN", "test-token")

    async def run(*args):
        return "Report body"

    monkeypatch.setattr(ui, "run_agent_turn_with_retries", run)
    with TestClient(ui.app) as client:
        assert (
            client.post("/api/resolve-company", json={"session_id": SID, "query": "A"}).status_code
            == 401
        )
        client.headers["X-UI-TOKEN"] = "test-token"
        approve(client)
        assert client.post("/api/generate-report", json={"session_id": SID}).status_code == 200
        pdf = client.get("/api/report-pdf", params={"session_id": SID})
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
        client.post(
            "/api/approve-company", json={"session_id": SID, "company_number": B["company_number"]}
        )
        assert client.get("/api/report-pdf", params={"session_id": SID}).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "reset", "resolve"])
async def test_in_flight_report_cannot_overwrite_changed_session(setup, monkeypatch, action):
    request = SimpleNamespace(state=SimpleNamespace(owner="owner"), headers={})
    await ui.save_state(
        SID,
        request,
        None,
        candidates=[A, B],
        approved={"company_number": A["company_number"], "company_name": A["title"]},
    )
    started, finish = asyncio.Event(), asyncio.Event()

    async def run(*args):
        started.set()
        await finish.wait()
        return "old report"

    monkeypatch.setattr(ui, "run_agent_turn_with_retries", run)
    task = asyncio.create_task(
        ui.generate_report(ui.GenerateReportRequest(session_id=SID), request)
    )
    await started.wait()
    with pytest.raises(ui.HTTPException) as exc:
        await ui.generate_report(ui.GenerateReportRequest(session_id=SID), request)
    assert exc.value.status_code == 409
    if action == "approve":
        await ui.approve_company_endpoint(
            ui.ApproveCompanyRequest(session_id=SID, company_number=B["company_number"]), request
        )
    elif action == "reset":
        await ui.reset_session(ui.SessionRequest(session_id=SID), request)
    else:
        await ui.resolve_company_endpoint(
            ui.ResolveCompanyRequest(session_id=SID, query="B"), request
        )
    finish.set()
    with pytest.raises(StateConflict):
        await task
    assert not (await ui.session_state(SID, request)).get("report")


def test_state_survives_new_store_and_rejects_stale_writes(tmp_path):
    url = "sqlite:///" + str(tmp_path / "shared.db")
    first, second = StateStore(url), StateStore(url)
    state = first.write(SID, "owner", None, {"approved": A})
    assert second.read(SID, "owner")["approved"] == A
    second.write(SID, "owner", state["version"], {"approved": B})
    with pytest.raises(StateConflict):
        first.write(SID, "owner", state["version"], {"approved": A})
    first.engine.dispose()
    second.engine.dispose()


def status_error(status):
    response = httpx.Response(status, request=httpx.Request("GET", "https://example.com"))
    return httpx.HTTPStatusError("failure", request=response.request, response=response)


@pytest.mark.parametrize(
    "status,expected", [(401, False), (404, False), (429, True), (500, True), (503, True)]
)
def test_retry_statuses(status, expected):
    assert registry._retryable(status_error(status)) is expected


def test_network_failures_retry():
    assert registry._retryable(httpx.ReadTimeout("timeout"))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 404, 429, 503])
async def test_insolvency_only_404_means_absent(monkeypatch, status):
    async def data(*args, **kwargs):
        return {}

    for name in (
        "_company_profile_impl",
        "_registered_office_address_impl",
        "_list_officers_impl",
        "_psc_impl",
        "_charges_impl",
        "_filing_history_impl",
    ):
        monkeypatch.setattr(registry, name, data)

    async def insolvency(*args):
        raise status_error(status)

    monkeypatch.setattr(registry, "_insolvency_impl", insolvency)
    if status == 404:
        result = await registry.underwriting_pack.fn("12345678")
        assert result["insolvency"]["status"] == "not_found"
    else:
        with pytest.raises(httpx.HTTPStatusError):
            await registry.underwriting_pack.fn("12345678")


@pytest.mark.asyncio
async def test_number_resolution_uses_legal_name(monkeypatch):
    async def profile(*args):
        return {"company_name": "Real Company", "company_status": "active"}

    monkeypatch.setattr(registry, "_company_profile_impl", profile)
    result = await registry.resolve_company.fn("12345678")
    assert result["candidates"][0]["title"] == "Real Company"


def test_bad_inputs_rejected(setup):
    with TestClient(ui.app) as client:
        assert (
            client.post("/api/resolve-company", json={"session_id": SID, "query": "  "}).status_code
            == 422
        )
        assert (
            client.post(
                "/api/approve-company", json={"session_id": SID, "company_number": "../../other"}
            ).status_code
            == 422
        )


@pytest.mark.asyncio
async def test_late_search_cannot_replace_newer_search(setup, monkeypatch):
    request = SimpleNamespace(state=SimpleNamespace(owner="owner"), headers={})
    started, finish = asyncio.Event(), asyncio.Event()

    async def resolve(query):
        if query == "old":
            started.set()
            await finish.wait()
            return {"candidates": [A]}
        return {"candidates": [B]}

    monkeypatch.setattr(ui, "resolve_company_via_mcp", resolve)
    task = asyncio.create_task(
        ui.resolve_company_endpoint(ui.ResolveCompanyRequest(session_id=SID, query="old"), request)
    )
    await started.wait()
    await ui.resolve_company_endpoint(
        ui.ResolveCompanyRequest(session_id=SID, query="new"), request
    )
    finish.set()
    with pytest.raises(StateConflict):
        await task
    assert (await ui.session_state(SID, request))["candidates"] == [B]


@pytest.mark.asyncio
async def test_failed_generation_releases_reservation_without_leaking_details(setup, monkeypatch):
    request = SimpleNamespace(state=SimpleNamespace(owner="owner"), headers={})
    await ui.save_state(
        SID,
        request,
        None,
        candidates=[A],
        approved={"company_number": A["company_number"], "company_name": A["title"]},
    )

    async def run(*args):
        raise RuntimeError("sensitive upstream detail")

    monkeypatch.setattr(ui, "run_agent_turn_with_retries", run)
    monkeypatch.setattr(ui, "DEBUG_ERRORS", False)
    with pytest.raises(ui.HTTPException) as exc:
        await ui.generate_report(ui.GenerateReportRequest(session_id=SID), request)
    assert "sensitive" not in str(exc.value.detail)
    assert not (await ui.session_state(SID, request))["running"]


def test_deadline_is_reported_as_timeout():
    assert ui._http_exception_for_agent_error(TimeoutError()).status_code == 504


def test_session_expiry(tmp_path, monkeypatch):
    store = StateStore("sqlite:///" + str(tmp_path / "expiry.db"), ttl=1)
    store.write(SID, "owner", None, {"approved": A})
    from agent_ui import state as state_module

    now = state_module.time.time()
    monkeypatch.setattr(state_module.time, "time", lambda: now + 2)
    assert store.read(SID, "owner") is None
    store.engine.dispose()
