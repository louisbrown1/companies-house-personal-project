import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "mcp_server"))
_temp = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = "sqlite:///" + str(Path(_temp.name) / "test.db")
os.environ["CH_MCP_MODE"] = "http"
os.environ["CH_MCP_URL"] = "http://localhost:8081/mcp"
os.environ.pop("K_SERVICE", None)
# Unit tests never launch a live browser because of a developer's local .env.
os.environ["BROWSER_RESEARCH_ENABLED"] = "0"


def pytest_sessionfinish(session, exitstatus):
    from agent_ui import app

    app.store.engine.dispose()
    _temp.cleanup()
