"""Run bounded project attacks through PyRIT or garak; default is offline smoke."""

import argparse
import asyncio
import io
import json
import os
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .client import ApplicationClient, Settings
from .scoring import assess


async def pyrit_campaign(client, cases):
    from pyrit.executor.attack import PromptSendingAttack
    from pyrit.setup import initialize_pyrit_async

    from .pyrit_target import UnderwritingTarget

    await initialize_pyrit_async(
        memory_db_type="InMemory", load_defaults=False, env_files=[], seed=7
    )
    attack = PromptSendingAttack(
        objective_target=UnderwritingTarget(client=client), max_attempts_on_failure=0
    )
    errors = []
    for case in cases:
        before = len(client.records)
        try:
            result = await attack.execute_async(objective=case["prompt"])
            if len(client.records) == before:
                raise RuntimeError("Framework produced no application request")
            client.records[-1]["framework_outcome"] = str(result.outcome)
        except Exception as exc:
            errors.append({"case_id": case["id"], "error_type": type(exc).__name__})
        for record in client.records[before:]:
            record.update(case_id=case["id"], category=case["category"])
    return errors


def garak_campaign(client, cases):
    from garak import _config
    from garak.generators.function import Single
    from garak.probes.base import Probe

    from .garak_target import configure

    _config.load_base_config()
    _config.run.generations = 1
    _config.system.parallel_attempts = 1
    _config.system.parallel_requests = 1
    configure(client)
    generator = Single(name="redteam.garak_target#generate")
    # The native Probe runs our domain-specific corpus through garak's attempt pipeline.
    probe = Probe()
    probe.parallelisable_attempts = False
    probe.generations = 1
    errors = []
    previous_report = getattr(_config.transient, "reportfile", None)
    with io.StringIO() as reportfile:
        _config.transient.reportfile = reportfile
        try:
            for case in cases:
                before = len(client.records)
                probe.prompts = [case["prompt"]]
                try:
                    attempts = list(probe.probe(generator))
                    if not attempts or len(client.records) == before:
                        raise RuntimeError("Framework produced no application request")
                except Exception as exc:
                    errors.append({"case_id": case["id"], "error_type": type(exc).__name__})
                for record in client.records[before:]:
                    record.update(case_id=case["id"], category=case["category"])
        finally:
            _config.transient.reportfile = previous_report
            configure(None)
    return errors


def summarize(client, errors, *, engine, case_count):
    results = []
    for record in client.records:
        results.append(
            {
                **record,
                **assess(
                    record, client.settings.company_number, smoke=client.settings.mode == "smoke"
                ),
            }
        )
    counts = dict(Counter(row["outcome"] for row in results))
    incomplete = len(results) != case_count or bool(errors) or bool(counts.get("error"))
    try:
        engine_version = version(engine)
    except PackageNotFoundError:
        engine_version = "not-installed"
    return {
        "schema_version": 1,
        "engine": engine,
        "engine_version": engine_version,
        "mode": client.settings.mode,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": client.settings.base_url
        if client.settings.mode == "live"
        else "synthetic-mock-transport",
        "company_number": client.settings.company_number,
        "requested_cases": case_count,
        "report_requests": client.calls,
        "counts": counts,
        "framework_errors": errors,
        "results": results,
        "incomplete": incomplete,
        "exit_code": 2 if incomplete else (1 if counts.get("finding") else 0),
        "interpretation": "Smoke is adapter validation only. Live not_observed is not a safety verdict; manually review every response.",
    }


def main(argv=None):
    # garak emits Unicode status symbols; Windows redirected output may default to cp1252.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=["pyrit", "garak"], required=True)
    parser.add_argument("--mode", choices=["smoke", "live"], default="smoke")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--company-number", default="12345678")
    parser.add_argument("--max-cases", type=int, default=6)
    parser.add_argument(
        "--timeout",
        type=float,
        default=1860,
        help="Per-request seconds; exceed the backend report deadline",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("redteam/results"))
    args = parser.parse_args(argv)
    settings = Settings(
        mode=args.mode,
        base_url=args.base_url,
        company_number=args.company_number,
        token=os.getenv("REDTEAM_UI_TOKEN", ""),
        max_reports=args.max_cases,
        timeout=args.timeout,
    )
    cases = json.loads(Path(__file__).with_name("cases.json").read_text())[0 : args.max_cases]
    client = ApplicationClient(settings)
    try:
        errors = (
            asyncio.run(pyrit_campaign(client, cases))
            if args.engine == "pyrit"
            else garak_campaign(client, cases)
        )
    except Exception as exc:
        errors = [{"case_id": "framework_setup", "error_type": type(exc).__name__}]
    report = summarize(client, errors, engine=args.engine, case_count=len(cases))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / f"{args.engine}-{args.mode}-{uuid.uuid4().hex}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"report": str(path), "counts": report["counts"], "incomplete": report["incomplete"]}
        )
    )
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
