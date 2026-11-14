"""ADK entry point for the same validated workflow used by the web application.

An approved company must come from trusted session state, never model-generated
resolution or a user prompt. Without approval, only registry candidates are returned.
"""

import json
import sys
from functools import lru_cache

from google.adk.agents.base_agent import BaseAgent
from google.adk.events import Event
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StdioConnectionParams,
    StreamableHTTPConnectionParams,
)
from google.genai import types
from mcp import StdioServerParameters

from agent_ui.registry import RegistrySettings, call_registry_tool
from agent_ui.workflow import generate_report, resolve_company


def companies_house_toolset(tool_filter):
    """Compatibility helper for external ADK callers; built only on demand."""
    settings = RegistrySettings.from_env()
    if settings.mode == "stdio":
        connection = StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable, args=[settings.server_path], env=settings.child_env()
            ),
            timeout=settings.timeout,
        )
    else:
        connection = StreamableHTTPConnectionParams(
            url=settings.url,
            timeout=settings.timeout,
            sse_read_timeout=settings.sse_timeout,
            headers={"Authorization": f"Bearer {settings.auth}"} if settings.auth else None,
        )
    return McpToolset(connection_params=connection, tool_filter=tool_filter)


class UnderwritingAgent(BaseAgent):
    approved_context: bool = False

    async def _run_async_impl(self, ctx):
        message = (
            "".join(p.text for p in (ctx.user_content.parts or []) if p.text)
            if ctx.user_content
            else ""
        )
        approved = ctx.session.state.get("approved_company")
        if approved is None:
            if self.approved_context:
                raise ValueError("Approved company missing from trusted session state")
            resolution = await resolve_company(message, call_registry_tool)
            response = json.dumps({"status": "approval_required", "resolution": resolution})
        else:
            response = await generate_report(approved, message, call_registry_tool)
        yield Event(
            author=self.name, content=types.Content(role="model", parts=[types.Part(text=response)])
        )


def build_root_agent(*, approved_context=False):
    return UnderwritingAgent(name="UnderwritingPipeline", approved_context=approved_context)


@lru_cache(maxsize=1)
def _compatibility_root():
    return build_root_agent()


def __getattr__(name):
    if name == "root_agent":
        return _compatibility_root()
    raise AttributeError(name)
