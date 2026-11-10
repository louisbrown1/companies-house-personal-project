import asyncio
import json

import pytest
from test_table_interpretation import document

from agent_ui import table_interpretation as tables
from agent_ui.document_memory import DocumentMemory, active_memory


def memory():
    return DocumentMemory(
        pdf_sha256="synthetic", company_name="Example Limited", period="2026-06-30"
    )


def test_memory_is_bounded_and_source_linked():
    m = memory()
    for i in range(100):
        m.add(page=1, source_id=f"r{i}", label="Interest income", status="source_context")
    assert len(m.entries) == 64 and m.entries[0].source_id == "r36"
    t = tables.build_tables(document(), ["2026-06-30"])[0]
    assert len(m.context(t, t["rows"])) < 4200
    assert "synthetic" in m.context(t, t["rows"])


@pytest.mark.parametrize(
    "change", [{"company_name": "Other Limited"}, {"period": "2025-06-30"}, {"verified": False}]
)
def test_memory_cannot_cross_identity_or_period(change):
    with pytest.raises(ValueError):
        memory().check("synthetic", {**document()["identity"], **change})


def test_memory_cannot_cross_pdf():
    with pytest.raises(ValueError):
        memory().check("another-pdf", document()["identity"])


def test_context_is_reset_even_when_extraction_fails(monkeypatch):
    async def fail(*args):
        assert active_memory.get() is not None
        raise RuntimeError("test failure")

    monkeypatch.setattr(tables, "_interpret_document", fail)

    async def run():
        with pytest.raises(RuntimeError):
            await tables.interpret_document(document(), ["2026-06-30"], memory())
        assert active_memory.get() is None

    asyncio.run(run())
    assert active_memory.get() is None


def test_conflict_removes_previously_validated_memory():
    m = memory()
    m.add(
        page=1,
        source_id="cell",
        label="Revenue",
        status="source_validated",
        meaning="revenue; group; 2026-06-30; GBP scale 1000",
    )
    t = tables.build_tables(document(), ["2026-06-30"])[0]
    m.remember(t, {}, {"conflicts": [["group", "2026-06-30", "revenue"]]})
    assert not any(e.status == "source_validated" for e in m.entries)
    assert any(e.status == "rejected" and "Conflicting revenue" in e.meaning for e in m.entries)


def test_model_sees_memory_but_cannot_bypass_source_validation(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from agent_ui import model_config

    prompts = []

    class Model:
        async def generate_content_async(self, request, stream=False):
            prompts.append(request.contents[0].parts[0].text)
            # Memory cannot authorize a row outside the offered source schema.
            yield SimpleNamespace(
                error_code=None,
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            text=json.dumps(
                                {
                                    "columns": ["c0"],
                                    "rows": [{"row_id": "invented", "metric": "revenue"}],
                                }
                            )
                        )
                    ]
                ),
            )

    monkeypatch.setattr(model_config, "agent_model", lambda: Model())
    monkeypatch.setattr(tables.accounts, "CACHE", tmp_path)
    m = memory()
    facts, audit = asyncio.run(tables.interpret_document(document(), ["2026-06-30"], m))
    assert not facts and audit["memory"]["enabled"] and audit["memory"]["reads"] > 0
    assert all("TEMPORARY DOCUMENT MEMORY" in p for p in prompts)
    assert not list(tmp_path.glob("*table-plan.json")) and active_memory.get() is None
    assert "entries" not in audit["tables"][0]


def test_concurrent_documents_have_isolated_context(monkeypatch):
    seen = []

    async def inspect(doc, periods):
        current = active_memory.get()
        await asyncio.sleep(0)
        assert active_memory.get() is current
        seen.append(current)
        return {}, {}

    monkeypatch.setattr(tables, "_interpret_document", inspect)
    first, second = memory(), memory()

    async def run():
        await asyncio.gather(
            tables.interpret_document(document(), [], first),
            tables.interpret_document(document(), [], second),
        )

    asyncio.run(run())
    assert len(seen) == 2 and seen[0] is not seen[1] and active_memory.get() is None
