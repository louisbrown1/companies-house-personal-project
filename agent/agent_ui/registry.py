"""Shared registry transport configuration and bounded tool calls."""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from pydantic import BaseModel, ConfigDict, Field, model_validator


class RegistrySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    mode: Literal["stdio", "http"] = "stdio"
    url: str | None = None
    auth: str | None = Field(default=None, repr=False)
    timeout: float = Field(default=120, gt=0)
    sse_timeout: float = Field(default=180, gt=0)
    server_path: str

    @model_validator(mode="after")
    def validate_http(self):
        if self.mode == "http":
            u = urlsplit(self.url or "")
            if (
                u.scheme not in ("http", "https")
                or not u.hostname
                or u.username
                or u.password
                or u.fragment
                or u.query
            ):
                raise ValueError(
                    "HTTP MCP requires a URL without embedded credentials, query or fragment"
                )
            if u.scheme == "http" and u.hostname not in ("127.0.0.1", "localhost", "::1"):
                raise ValueError("Remote MCP requires HTTPS")
        return self

    @classmethod
    def from_env(cls):
        return cls(
            mode=os.getenv("CH_MCP_MODE", "stdio").strip().lower(),
            url=os.getenv("CH_MCP_URL"),
            auth=os.getenv("CH_MCP_AUTH") or None,
            timeout=os.getenv("CH_MCP_TIMEOUT_SEC", "120"),
            sse_timeout=os.getenv("CH_MCP_SSE_READ_TIMEOUT_SEC", "180"),
            server_path=os.getenv(
                "CH_MCP_SERVER_PATH",
                str(Path(__file__).resolve().parents[2] / "mcp_server/ch_mcp_server.py"),
            ),
        )

    def child_env(self):
        return {**os.environ, "MCP_TRANSPORT": "stdio"}


def parse_registry_result(result):
    if getattr(result, "is_error", False):
        raise RuntimeError("Registry tool failed")
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    content = result if isinstance(result, (list, tuple)) else getattr(result, "content", [])
    if content and getattr(content[0], "text", None):
        data = json.loads(content[0].text)
        if isinstance(data, dict):
            return data
    raise ValueError("Registry response must be a JSON object")


async def call_registry_tool(name, arguments):
    settings = RegistrySettings.from_env()
    if settings.mode == "stdio":
        transport = StdioTransport(
            command=sys.executable,
            args=[settings.server_path],
            env=settings.child_env(),
            keep_alive=False,
        )
    else:
        transport = StreamableHttpTransport(
            settings.url, auth=settings.auth, sse_read_timeout=settings.sse_timeout
        )
    async with (
        asyncio.timeout(settings.timeout),
        Client(transport, timeout=settings.timeout) as client,
    ):
        return parse_registry_result(await client.call_tool(name, arguments))
