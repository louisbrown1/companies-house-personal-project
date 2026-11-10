"""Exercise real local Gemma through the same ADK adapter as underwriting.

No registry requests or lending decisions are made. This is a compatibility
check, not a red-team campaign or a full report evaluation.
"""

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from google.adk.models.llm_request import LlmRequest
from google.genai import types

from .model_config import agent_model, model_provider
from .validation import EvidenceBundle


async def check(only=None):
    if model_provider() != "gemma":
        raise ValueError("Set MODEL_PROVIDER=gemma; this check never falls back to Gemini")
    model = agent_model()
    checks = [
        (
            "text",
            "Reply with LOCAL_GEMMA_OK and nothing else.",
            types.GenerateContentConfig(temperature=0, max_output_tokens=64),
        ),
        (
            "evidence_schema",
            "There is no verified company evidence. Return an empty evidence list and an uncertainty stating that evidence is missing.",
            types.GenerateContentConfig(
                temperature=0,
                max_output_tokens=256,
                response_mime_type="application/json",
                response_schema=EvidenceBundle,
            ),
        ),
        (
            "tool_call",
            "Call lookup_company with company_number 12345678. Do not answer from memory.",
            types.GenerateContentConfig(
                temperature=0,
                max_output_tokens=128,
                tools=[
                    types.Tool(
                        function_declarations=[
                            types.FunctionDeclaration(
                                name="lookup_company",
                                description="Look up a synthetic company for a compatibility test.",
                                parameters=types.Schema(
                                    type="OBJECT",
                                    properties={"company_number": types.Schema(type="STRING")},
                                    required=["company_number"],
                                ),
                            )
                        ]
                    )
                ],
            ),
        ),
    ]
    if only:
        checks = [c for c in checks if c[0] == only]
    results = []
    for name, prompt, config in checks:
        started = time.monotonic()
        try:
            parts = []
            async with asyncio.timeout(240):
                async for response in model.generate_content_async(
                    LlmRequest(
                        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
                        config=config,
                    ),
                    stream=False,
                ):
                    if response.error_code:
                        raise RuntimeError("Model adapter reported an error")
                    if response.content:
                        parts.extend(response.content.parts or [])
            text = "".join(p.text for p in parts if p.text)
            calls = [p.function_call for p in parts if p.function_call]
            if name == "text":
                passed = "LOCAL_GEMMA_OK" in text
            elif name == "evidence_schema":
                parsed = EvidenceBundle.model_validate_json(text)
                passed = not parsed.evidence and bool(parsed.uncertainty)
            else:
                passed = any(
                    c.name == "lookup_company" and c.args.get("company_number") == "12345678"
                    for c in calls
                )
            results.append(
                {
                    "check": name,
                    "passed": passed,
                    "response": text,
                    "tool_calls": [c.model_dump() for c in calls],
                }
            )
        except Exception as exc:
            results.append({"check": name, "passed": False, "error_type": type(exc).__name__})
            if os.getenv("GEMMA_CHECK_DEBUG") == "1":
                import traceback

                traceback.print_exc()
        results[-1]["elapsed_seconds"] = round(time.monotonic() - started, 2)
        print(json.dumps(results[-1]), flush=True)
        if not results[-1]["passed"]:
            break
    artifact = {
        "model": os.getenv("GEMMA_MODEL", "gemma4-12b"),
        "scope": "real model compatibility checks; not a full platform report",
        "checks": results,
    }
    filename = f"gemma-compatibility-{only}.json" if only else "gemma-compatibility.json"
    output = Path(__file__).resolve().parents[2] / "redteam" / "results" / filename
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    return 0 if len(results) == len(checks) and all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=["text", "evidence_schema", "tool_call"])
    raise SystemExit(asyncio.run(check(parser.parse_args().only)))
