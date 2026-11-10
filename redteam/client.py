"""Bounded adapter for the real resolve -> approve -> generate API flow."""

import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx


class TargetError(RuntimeError):
    """Infrastructure/setup errors must never become a successful defence."""


@dataclass(frozen=True)
class Settings:
    mode: str = "smoke"
    base_url: str = "http://127.0.0.1:8080"
    company_number: str = "12345678"
    token: str = field(default="", repr=False)
    max_reports: int = 6
    timeout: float = 1860

    def __post_init__(self):
        if self.mode not in {"smoke", "live"}:
            raise ValueError("mode must be smoke or live")
        if not 1 <= self.max_reports <= 50 or not 1 <= self.timeout <= 7200:
            raise ValueError("Use 1..50 reports and a 1..7200 second timeout")
        if not re.fullmatch(r"(?:[0-9]{8}|[A-Z]{2}[0-9]{6})", self.company_number):
            raise ValueError("An eight-character company number is required")
        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError(
                "Target must be an HTTP(S) origin without credentials or query parameters"
            )
        if self.mode == "live":
            if os.getenv("REDTEAM_ALLOW_LIVE") != "1":
                raise ValueError("Set REDTEAM_ALLOW_LIVE=1 for model-backed requests")
            allowed = os.getenv("REDTEAM_ALLOWED_ORIGIN", "").rstrip("/")
            if self.base_url.rstrip("/") != allowed:
                raise ValueError("Target must exactly match REDTEAM_ALLOWED_ORIGIN")
            if url.hostname not in {"localhost", "127.0.0.1", "::1"} and url.scheme != "https":
                raise ValueError("Remote targets require HTTPS")


class ApplicationClient:
    def __init__(self, settings: Settings, *, transport=None):
        self.settings = settings
        if settings.mode == "smoke" and transport is None:
            transport = httpx.MockTransport(smoke_response)
        self.transport = transport
        self.calls = 0
        self.lock = threading.Lock()
        self.records = []

    def _post(self, client, path, payload):
        try:
            response = client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise TargetError(f"Transport failure at {path}: {type(exc).__name__}") from None
        if response.status_code != 200:
            # Do not copy arbitrary upstream error text or credentials into logs.
            raise TargetError(f"HTTP {response.status_code} at {path}")
        try:
            result = response.json()
        except ValueError:
            raise TargetError(f"Non-JSON response at {path}") from None
        if not isinstance(result, dict):
            raise TargetError(f"Invalid response shape at {path}")
        return result

    def generate(self, prompt: str) -> str:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
            raise TargetError("Prompt outside the application's 1..4000 character limit; not sent")
        with self.lock:
            if self.calls >= self.settings.max_reports:
                raise TargetError("Report request budget exhausted")
            self.calls += 1
        sid = "redteam-" + uuid.uuid4().hex
        headers = {"X-UI-TOKEN": self.settings.token} if self.settings.token else {}
        record = {"prompt": prompt, "status": "error", "response": None}
        self.records.append(record)
        # A new cookie jar and random application session for every attack.
        with httpx.Client(
            base_url=self.settings.base_url,
            headers=headers,
            timeout=self.settings.timeout,
            transport=self.transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            try:
                resolved = self._post(
                    client,
                    "/api/resolve-company",
                    {"session_id": sid, "query": self.settings.company_number},
                )
                candidates = resolved.get("resolution", {}).get("candidates", [])
                if not any(
                    c.get("company_number") == self.settings.company_number for c in candidates
                ):
                    raise TargetError("Configured company was not returned by resolution")
                approved = self._post(
                    client,
                    "/api/approve-company",
                    {"session_id": sid, "company_number": self.settings.company_number},
                )
                if (
                    approved.get("approved_company", {}).get("company_number")
                    != self.settings.company_number
                ):
                    raise TargetError("Approval response does not match configured company")
                result = self._post(
                    client, "/api/generate-report", {"session_id": sid, "message": prompt}
                )
                response = result.get("response")
                if not isinstance(response, str) or not response.strip():
                    raise TargetError("No report text returned")
                record.update(
                    status="completed",
                    response=response,
                    returned_company=result.get("approved_company", {}).get("company_number"),
                )
                return response
            except TargetError as exc:
                record["error"] = str(exc)
                raise
            finally:
                try:
                    self._post(client, "/api/reset-session", {"session_id": sid})
                except TargetError as exc:
                    record["cleanup_error"] = str(exc)


def smoke_response(request):
    """Synthetic adapter check, not an evaluation of model behaviour."""
    import json

    payload = json.loads(request.content)
    path = request.url.path
    if path.endswith("resolve-company"):
        return httpx.Response(
            200,
            json={
                "resolution": {
                    "candidates": [
                        {"company_number": payload["query"], "title": "Synthetic Company"}
                    ]
                }
            },
            headers={"set-cookie": "uw_owner=synthetic; Path=/"},
        )
    if path.endswith("approve-company"):
        return httpx.Response(
            200, json={"approved_company": {"company_number": payload["company_number"]}}
        )
    if path.endswith("generate-report"):
        return httpx.Response(
            200,
            json={
                "response": "Synthetic report. No real model was evaluated.",
                "approved_company": {"company_number": "12345678"},
            },
        )
    return httpx.Response(200, json={"reset": True})
