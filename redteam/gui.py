"""Launch the bundled CoPyRIT UI with the project's bounded application target."""

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from .client import ApplicationClient, Settings

ROOT = Path(__file__).resolve().parent.parent


def create_app(settings):
    # Import after validation: PyRIT initializes its per-user storage on import.
    from pyrit.backend.main import app, lifespan
    from pyrit.registry import TargetRegistry

    from .pyrit_target import UnderwritingTarget

    @asynccontextmanager
    async def project_lifespan(application):
        async with lifespan(application):
            target = UnderwritingTarget(client=ApplicationClient(settings))
            TargetRegistry.get_registry_singleton().instances.register(
                target,
                name=f"underwriting-{settings.mode}-independent-reports",
                tags={"project": "underwriting", "mode": settings.mode},
            )
            yield

    app.router.lifespan_context = project_lifespan
    return app


def open_when_ready(port):
    import urllib.request

    for _ in range(120):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=1
            ) as response:
                if response.status == 200:
                    webbrowser.open(f"http://127.0.0.1:{port}")
                    return
        except OSError:
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["smoke", "live"], default="smoke")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--company-number", default="12345678")
    parser.add_argument("--max-reports", type=int, default=6)
    parser.add_argument(
        "--timeout",
        type=float,
        default=1860,
        help="Per-request seconds; exceed the backend report deadline",
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    try:
        settings = Settings(
            mode=args.mode,
            base_url=args.base_url,
            company_number=args.company_number,
            token=os.getenv("REDTEAM_UI_TOKEN", ""),
            max_reports=args.max_reports,
            timeout=args.timeout,
        )
    except ValueError as exc:
        parser.error(str(exc))
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    # Explicit configuration prevents accidental loading of model credentials or
    # initializers from a user's global PyRIT configuration.
    directory = ROOT / "redteam" / "results"
    directory.mkdir(exist_ok=True)
    config = directory / "gui-config.json"
    config.write_text(
        json.dumps(
            {
                "memory_db_type": "sqlite",
                "initializers": [],
                "initialization_scripts": [],
                "env_files": [],
                "env_akv_ref": [],
                "allow_custom_initializers": False,
                "custom_initializers_source": None,
                "max_concurrent_scenario_runs": 1,
                "operator": "local-user",
                "operation": f"underwriting-{args.mode}",
            }
        ),
        encoding="utf-8",
    )
    os.environ["PYRIT_CONFIG_FILE"] = str(config)
    print(f"CoPyRIT: http://127.0.0.1:{args.port}")
    print(f"Target: underwriting-{args.mode}-independent-reports")
    print(f"Budget: {args.max_reports} report requests for this server session.")
    print("Each message starts a separate report; conversation history is not sent.")
    if args.mode == "smoke":
        print("OFFLINE SMOKE: synthetic responses only; no model is evaluated.")
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(args.port,), daemon=True).start()
    import uvicorn

    uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
