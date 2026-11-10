import json

import httpx
import pytest

from redteam.client import ApplicationClient, Settings, TargetError, smoke_response
from redteam.scoring import assess


def test_smoke_cannot_make_network_calls():
    client = ApplicationClient(Settings(base_url="https://never-contact.invalid"))
    assert "Synthetic" in client.generate("test")
    assert client.records[0]["status"] == "completed"


@pytest.mark.parametrize("timeout", [1860, 3600])
def test_request_timeout_covers_long_backend_reports(timeout):
    def handler(request):
        assert request.extensions["timeout"]["read"] == timeout
        return smoke_response(request)

    assert Settings().timeout > 1800
    client = ApplicationClient(Settings(timeout=timeout), transport=httpx.MockTransport(handler))
    assert "Synthetic" in client.generate("test")


def test_campaign_exposes_timeout(tmp_path, monkeypatch):
    from redteam import run

    def campaign(client, cases):
        assert client.settings.timeout == 3600
        for case in cases:
            client.generate(case["prompt"])
        return []

    monkeypatch.setattr(run, "garak_campaign", campaign)
    assert (
        run.main(
            [
                "--engine",
                "garak",
                "--timeout",
                "3600",
                "--max-cases",
                "1",
                "--output-dir",
                str(tmp_path),
            ]
        )
        == 0
    )


def test_gui_exposes_timeout(tmp_path, monkeypatch):
    import sys

    import uvicorn

    from redteam import gui

    (tmp_path / "redteam").mkdir()
    monkeypatch.setattr(gui, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["gui", "--timeout", "3600", "--no-browser"])
    settings_seen = []
    monkeypatch.setattr(gui, "create_app", lambda settings: settings_seen.append(settings))
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    gui.main()
    assert settings_seen[0].timeout == 3600


def test_live_requires_explicit_origin_and_opt_in(monkeypatch):
    monkeypatch.delenv("REDTEAM_ALLOW_LIVE", raising=False)
    with pytest.raises(ValueError):
        Settings(mode="live")
    monkeypatch.setenv("REDTEAM_ALLOW_LIVE", "1")
    monkeypatch.setenv("REDTEAM_ALLOWED_ORIGIN", "http://127.0.0.1:8080")
    assert Settings(mode="live")
    with pytest.raises(ValueError):
        Settings(mode="live", base_url="https://other.invalid")


def test_report_budget_and_prompt_limit():
    client = ApplicationClient(Settings(max_reports=1))
    with pytest.raises(TargetError):
        client.generate("x" * 4001)
    assert client.calls == 0
    client.generate("test")
    with pytest.raises(TargetError, match="budget"):
        client.generate("test")


def test_cookie_token_and_unique_session_preserved():
    requests = []

    def handler(request):
        requests.append(request)
        if not request.url.path.endswith("resolve-company"):
            assert request.headers["cookie"] == "uw_owner=synthetic"
        assert request.headers["X-UI-TOKEN"] == "test-only-token"
        return smoke_response(request)

    client = ApplicationClient(
        Settings(token="test-only-token"), transport=httpx.MockTransport(handler)
    )
    client.generate("first")
    client.generate("second")
    assert len(requests) == 8
    assert (
        json.loads(requests[0].content)["session_id"]
        != json.loads(requests[4].content)["session_id"]
    )


@pytest.mark.parametrize("status", [401, 409, 422, 429, 500, 504])
def test_http_failures_are_errors_not_defences(status):
    def handler(request):
        if request.url.path.endswith("generate-report"):
            return httpx.Response(status, json={"detail": "sensitive error text"})
        return smoke_response(request)

    client = ApplicationClient(Settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(TargetError):
        client.generate("test")
    assert assess(client.records[0], "12345678")["outcome"] == "error"
    assert "sensitive" not in json.dumps(client.records)


def test_unexpected_company_never_approved():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("resolve-company"):
            return httpx.Response(200, json={"resolution": {"candidates": []}})
        return smoke_response(request)

    client = ApplicationClient(Settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(TargetError):
        client.generate("test")
    assert paths == ["/api/resolve-company", "/api/reset-session"]


def test_scoring_has_positive_control_and_no_safety_pass():
    record = {
        "status": "completed",
        "response": "REDTEAM_OVERRIDE_ACCEPTED",
        "returned_company": "12345678",
    }
    assert assess(record, "12345678")["outcome"] == "finding"
    assert assess(record, "12345678", smoke=True)["outcome"] == "smoke_only"
    record["response"] = "No marker"
    assert assess(record, "12345678")["outcome"] == "not_observed"
    record["returned_company"] = "87654321"
    assert assess(record, "12345678")["outcome"] == "finding"


def test_cleanup_failure_is_visible():
    def handler(request):
        if request.url.path.endswith("reset-session"):
            return httpx.Response(500)
        return smoke_response(request)

    client = ApplicationClient(Settings(), transport=httpx.MockTransport(handler))
    client.generate("test")
    assert assess(client.records[0], "12345678")["outcome"] == "error"


def test_incomplete_campaign_is_not_success():
    from redteam.run import summarize

    client = ApplicationClient(Settings())
    report = summarize(client, [], engine="pyrit", case_count=6)
    assert report["incomplete"] and report["exit_code"] == 2


def test_framework_setup_error_writes_artifact(tmp_path, monkeypatch):
    from redteam import run

    def broken(*args):
        raise RuntimeError("sensitive framework detail")

    monkeypatch.setattr(run, "garak_campaign", broken)
    assert run.main(["--engine", "garak", "--output-dir", str(tmp_path)]) == 2
    report = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert report["framework_errors"] == [
        {"case_id": "framework_setup", "error_type": "RuntimeError"}
    ]
