# app.py
import asyncio
import io
import json
import logging
import os
import secrets
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from xml.sax.saxutils import escape as xml_escape

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Preformatted, SimpleDocTemplate, Spacer
from sqlalchemy.engine import make_url

from .model_config import model_provider
from .registry import call_registry_tool
from .state import StateConflict, StateStore
from .validation import ReportValidationError
from .workflow import generate_report as generate_validated_report
from .workflow import resolve_company

# -----------------------------
# Config
# -----------------------------

# Optional simple protection for a public service:
# set AGENT_UI_TOKEN, then client must send header X-UI-TOKEN: <token>
AGENT_UI_TOKEN = os.getenv("AGENT_UI_TOKEN")

# Debug toggles
DEBUG_ERRORS = os.getenv("DEBUG_ERRORS", "0") == "1"

logger = logging.getLogger("uvicorn.error")


# Shared durable UI state; agent execution sessions are disposable per attempt.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./underwriting.db")
if os.getenv("K_SERVICE") and (
    make_url(DATABASE_URL).get_backend_name() != "postgresql" or not AGENT_UI_TOKEN
):
    raise RuntimeError("Cloud Run requires a shared DATABASE_URL and AGENT_UI_TOKEN.")
REPORT_TIMEOUT_SEC = int(os.getenv("REPORT_TIMEOUT_SEC", "600"))
store = StateStore(DATABASE_URL)
report_tasks: set[asyncio.Task] = set()


# -----------------------------
# Request models
# -----------------------------
class SessionRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class ChatRequest(SessionRequest):
    message: str = Field(max_length=4000)


class ResolveCompanyRequest(SessionRequest):
    query: str = Field(min_length=1, max_length=200)


class ApproveCompanyRequest(SessionRequest):
    company_number: str = Field(pattern=r"^(?:[0-9]{8}|[A-Z]{2}[0-9]{6})$")
    company_name: Optional[str] = Field(default=None, max_length=300)


class GenerateReportRequest(SessionRequest):
    message: Optional[str] = Field(default=None, max_length=4000)
    background: bool = False


def require_token(request: Request):
    if AGENT_UI_TOKEN and not secrets.compare_digest(
        request.headers.get("X-UI-TOKEN", "").encode(), AGENT_UI_TOKEN.encode()
    ):
        raise HTTPException(status_code=401, detail="Missing/invalid X-UI-TOKEN")


async def session_state(sid, request):
    return await asyncio.to_thread(store.read, sid, request.state.owner)


async def save_state(sid, request, old, **changes):
    payload = {k: v for k, v in (old or {}).items() if k != "version"}
    payload.update(changes)
    return await asyncio.to_thread(
        store.write, sid, request.state.owner, (old or {}).get("version"), payload
    )


def _try_parse_json(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except Exception:
        return None


def _json_to_pretty_text(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _build_report_pdf_bytes(report_record: dict[str, Any]) -> bytes:
    """
    Build a PDF from a stored report record:
    {
      "session_id": "...",
      "approved_company": {"company_number": "...", "company_name": "..."},
      "response": "<agent final text>",
      "generated_at": "ISO timestamp"
    }
    """
    buf = io.BytesIO()

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title="Underwriting Intelligence Report",
        author="Underwriting Agent",
    )

    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    heading_style = styles["Heading2"]
    meta_style = ParagraphStyle(
        "Meta",
        parent=styles["Normal"],
        fontSize=9,
        leading=12,
        textColor="#555555",
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "Body",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
        spaceAfter=8,
    )
    mono_style = ParagraphStyle(
        "MonoWrap",
        parent=styles["Code"],
        fontName="Courier",
        fontSize=8.5,
        leading=10.5,
        spaceAfter=6,
    )

    story = []

    approved = report_record.get("approved_company") or {}
    company_name = approved.get("company_name") or "Unknown company"
    company_number = approved.get("company_number") or "Unknown number"
    generated_at = report_record.get("generated_at") or datetime.now(timezone.utc).isoformat()
    response_text = report_record.get("response") or ""

    story.append(Paragraph("Underwriting Intelligence Report", title_style))
    story.append(Spacer(1, 4))
    story.append(Paragraph(f"<b>Approved company:</b> {xml_escape(str(company_name))}", body_style))
    story.append(
        Paragraph(f"<b>Companies House number:</b> {xml_escape(str(company_number))}", body_style)
    )
    story.append(
        Paragraph(f"<b>Generated at (UTC):</b> {xml_escape(str(generated_at))}", meta_style)
    )
    story.append(Spacer(1, 6))
    story.append(Paragraph("Report", heading_style))
    story.append(Spacer(1, 4))

    parsed = _try_parse_json(str(response_text))

    if parsed is not None:
        pretty = _json_to_pretty_text(parsed)
        chunk_size = 3000
        for i in range(0, len(pretty), chunk_size):
            chunk = pretty[i : i + chunk_size]
            story.append(Preformatted(chunk, mono_style))
            story.append(Spacer(1, 4))
    else:
        paragraphs = [p.strip() for p in str(response_text).split("\n\n") if p.strip()]
        if not paragraphs:
            paragraphs = ["(No report content available)"]

        for p in paragraphs:
            html = xml_escape(p).replace("\n", "<br/>")
            story.append(Paragraph(html, body_style))

    doc.build(story)
    pdf_bytes = buf.getvalue()
    buf.close()
    return pdf_bytes


def _flatten_exception_messages(exc: BaseException) -> list[str]:
    """Unwrap Python 3.11 ExceptionGroup / TaskGroup errors into readable causes."""
    msgs: list[str] = []
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            msgs.extend(_flatten_exception_messages(sub))
    else:
        msgs.append(f"{type(exc).__name__}: {exc}")
    return msgs


def _is_vertex_429(exc: Exception) -> bool:
    s = " ".join(_flatten_exception_messages(exc))
    return "429" in s and ("RESOURCE_EXHAUSTED" in s or "Resource exhausted" in s)


def _is_timeout_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    s = " ".join(_flatten_exception_messages(exc)).lower()
    timeout_markers = [
        "timeout",
        "timed out",
        "upstream request timeout",
        "readtimeout",
        "connecttimeout",
        "deadlineexceeded",
        "deadline exceeded",
    ]
    return any(m in s for m in timeout_markers)


def _is_transient_retryable(exc: Exception) -> bool:
    return _is_vertex_429(exc) or _is_timeout_error(exc)


def _http_exception_for_agent_error(exc: Exception) -> HTTPException:
    causes = _flatten_exception_messages(exc)
    logger.exception("Agent generation failed")
    if isinstance(exc, ReportValidationError):
        return HTTPException(
            status_code=502,
            detail={
                "code": "invalid_model_report",
                "error": "The model output failed evidence or report-format validation. No report was published.",
                "message": "The model may have returned malformed data or invalid citations. Retrying may not resolve this; the model workflow needs review.",
            },
        )
    detail: Any = {"error": "Report generation failed. Please retry."}
    if DEBUG_ERRORS:
        detail.update(causes=causes, traceback=traceback.format_exc())

    if _is_vertex_429(exc):
        return HTTPException(
            status_code=429,
            detail={
                **detail,
                "message": "Vertex AI capacity/quota temporarily exhausted. Please retry in a moment.",
            },
        )
    if _is_timeout_error(exc):
        return HTTPException(
            status_code=504,
            detail={
                **detail,
                "message": "Upstream timeout while generating the report. Please retry.",
            },
        )
    return HTTPException(status_code=500, detail=detail)


async def resolve_company_via_mcp(query: str) -> dict:
    return await resolve_company(query, call_registry_tool)


async def run_agent_turn(approved: dict, user_text: str) -> str:
    return await generate_validated_report(approved, user_text, call_registry_tool)


async def run_agent_turn_with_retries(approved: dict, user_text: str) -> str:
    async with asyncio.timeout(REPORT_TIMEOUT_SEC):
        # A timed-out local request can still be computing in the model server.
        # Replaying it automatically would duplicate expensive CPU inference.
        delays = [0] if model_provider() == "gemma" else [0, 1, 2]
        for attempt, delay in enumerate(delays):
            if delay:
                await asyncio.sleep(delay)
            try:
                return await run_agent_turn(approved, user_text)
            except Exception as exc:
                if attempt == len(delays) - 1 or not _is_transient_retryable(exc):
                    raise
                logger.warning("Retrying transient report failure, attempt %s", attempt + 1)


def _approved_prompt(
    company_number: str, company_name: Optional[str], extra_instruction: Optional[str] = None
) -> str:
    name = company_name or "Approved company"
    prompt = (
        "Generate an underwriting intelligence report for the APPROVED company ONLY.\n"
        f"Approved legal entity: {name}\n"
        f"Companies House number (authoritative): {company_number}\n\n"
        "Treat the company number as authoritative for entity identity.\n"
        "Include:\n"
        "1) Business model summary\n"
        "2) Structured competition view (sector + geography)\n"
        "3) Structured quality view (customer reviews / trade press) with citations and uncertainty if sparse.\n"
    )
    if extra_instruction and extra_instruction.strip():
        prompt += f"\nAdditional user instructions:\n{extra_instruction.strip()}\n"
    return prompt


# -----------------------------
# FastAPI app
# -----------------------------
app = FastAPI()


@app.exception_handler(StateConflict)
async def state_conflict_handler(request, exc):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.middleware("http")
async def browser_owner(request: Request, call_next):
    owner = request.cookies.get("uw_owner")
    fresh = not owner or len(owner) != 64
    request.state.owner = secrets.token_hex(32) if fresh else owner
    response = await call_next(request)
    response.set_cookie(
        "uw_owner",
        request.state.owner,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https" or bool(os.getenv("K_SERVICE")),
        max_age=86400,
    )
    return response


@app.get("/", response_class=HTMLResponse)
async def index():
    return Path(__file__).with_name("index.html").read_text(encoding="utf-8")


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.post("/api/reset-session")
async def reset_session(req: SessionRequest, request: Request):
    require_token(request)
    old = await session_state(req.session_id, request)
    await save_state(
        req.session_id,
        request,
        old,
        candidates=[],
        approved=None,
        report=None,
        running=None,
        report_error=None,
    )
    return {"session_id": req.session_id, "reset": True}


@app.post("/api/resolve-company")
async def resolve_company_endpoint(req: ResolveCompanyRequest, request: Request):
    require_token(request)
    old = await session_state(req.session_id, request)
    searching = await save_state(
        req.session_id,
        request,
        old,
        candidates=[],
        approved=None,
        report=None,
        running=None,
        report_error=None,
    )
    try:
        result = await resolve_company_via_mcp(req.query)
    except Exception as exc:
        raise _http_exception_for_agent_error(exc)
    candidates = result.get("candidates", [])
    if not isinstance(candidates, list) or any(
        not isinstance(c, dict) or not c.get("company_number") or not c.get("title")
        for c in candidates
    ):
        raise HTTPException(status_code=502, detail="Registry returned invalid candidates")
    await save_state(req.session_id, request, searching, candidates=candidates)
    return {"session_id": req.session_id, "resolution": result}


@app.post("/api/approve-company")
async def approve_company_endpoint(req: ApproveCompanyRequest, request: Request):
    require_token(request)
    old = await session_state(req.session_id, request)
    match = next(
        (
            c
            for c in (old or {}).get("candidates", [])
            if c.get("company_number") == req.company_number
        ),
        None,
    )
    if match is None:
        raise HTTPException(
            status_code=400, detail="Resolve a company first and select a current candidate."
        )
    approved = {"company_number": match["company_number"], "company_name": match["title"]}
    await save_state(
        req.session_id,
        request,
        old,
        approved=approved,
        report=None,
        running=None,
        report_error=None,
    )
    return {"session_id": req.session_id, "approved_company": approved}


@app.post("/api/generate-report")
async def generate_report(req: GenerateReportRequest, request: Request):
    require_token(request)
    old = await session_state(req.session_id, request)
    approved = (old or {}).get("approved")
    if not approved:
        raise HTTPException(
            status_code=400,
            detail="No approved company for this session. Resolve and approve a company first.",
        )
    if old.get("running") and old["running"] > time.time():
        raise HTTPException(
            status_code=409, detail="A report is already being generated for this session."
        )
    running = await save_state(
        req.session_id,
        request,
        old,
        running=time.time() + REPORT_TIMEOUT_SEC + 30,
        report=None,
        report_error=None,
    )
    prompt = _approved_prompt(approved["company_number"], approved["company_name"], req.message)
    if req.background:
        task = asyncio.create_task(
            _background_report(req.session_id, request, running, approved, prompt)
        )
        report_tasks.add(task)
        task.add_done_callback(report_tasks.discard)
        return JSONResponse({"status": "running"}, status_code=202)
    return await _complete_report(req.session_id, request, running, approved, prompt)


async def _background_report(sid, request, running, approved, prompt):
    try:
        await _complete_report(sid, request, running, approved, prompt)
    except (HTTPException, StateConflict):
        pass  # Failure is persisted; stale jobs cannot overwrite a changed session.


async def _complete_report(sid, request, running, approved, prompt):
    try:
        response = await run_agent_turn_with_retries(approved, prompt)
        record = {
            "session_id": sid,
            "approved_company": approved,
            "response": response,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        await save_state(sid, request, running, running=None, report=record, report_error=None)
        return record
    except StateConflict:
        raise
    except BaseException as exc:
        try:
            error = (
                "Report generation reached its time limit while processing sources. No report was published."
                if isinstance(exc, TimeoutError)
                else "Report generation stopped before completion. No report was published."
            )
            await save_state(sid, request, running, running=None, report_error=error)
        except StateConflict:
            pass
        if not isinstance(exc, Exception):
            raise
        raise _http_exception_for_agent_error(exc)


@app.get("/api/report-status")
async def report_status(
    request: Request,
    session_id: str = Query(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
):
    require_token(request)
    state = await session_state(session_id, request) or {}
    common = {"approved_company": state.get("approved")}
    if state.get("running"):
        if state["running"] > time.time():
            return {**common, "status": "running"}
        return {
            **common,
            "status": "failed",
            "error": "Report generation expired or the server stopped. No report was published.",
        }
    if state.get("report"):
        return {**common, "status": "completed", "report": state["report"]}
    if state.get("report_error"):
        return {**common, "status": "failed", "error": state["report_error"]}
    return {**common, "status": "idle"}


@app.get("/api/report-pdf")
async def report_pdf(session_id: str, request: Request):
    require_token(request)
    record = ((await session_state(session_id, request)) or {}).get("report")
    if not record:
        raise HTTPException(
            status_code=404,
            detail="No generated report found for this session. Generate a report first.",
        )
    try:
        pdf_bytes = await asyncio.to_thread(_build_report_pdf_bytes, record)
    except Exception as exc:
        raise _http_exception_for_agent_error(exc)
    # ASCII header avoids failures on legal names containing non-Latin characters.
    filename = f"underwriting_report_{record['approved_company']['company_number']}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    return await generate_report(
        GenerateReportRequest(session_id=req.session_id, message=req.message), request
    )
