import json
from decimal import Decimal

import httpx
import pytest

from agent_ui import accounts
from agent_ui.financial_health import add_financial_evidence
from agent_ui.validation import EvidenceBundle


def filing(period, filed, transaction="abc"):
    return {
        "category": "accounts",
        "date": filed,
        "description_values": {"made_up_date": period},
        "transaction_id": transaction,
        "description": "accounts-with-accounts-type-full",
        "links": {
            "document_metadata": "https://document-api.company-information.service.gov.uk/document/abc"
        },
    }


def test_latest_distinct_periods_select_amendment_not_duplicate_year():
    old = filing("2024-03-30", "2024-08-01")
    initial = filing("2025-03-29", "2025-08-01")
    amended = filing("2025-03-29", "2026-09-01")
    latest = filing("2026-03-28", "2026-08-01")
    assert accounts.latest_periods(
        [old, initial, amended, latest, filing("bad", "2026-01-01")]
    ) == [latest, amended]


@pytest.mark.parametrize(
    "url",
    [
        "http://document-api.company-information.service.gov.uk/document/a",
        "https://document-api.company-information.service.gov.uk.evil.test/document/a",
        "https://user:password@document-api.company-information.service.gov.uk/document/a",
        "https://127.0.0.1/document/a",
        "https://example.test/document/a",
    ],
)
def test_document_url_rejects_untrusted_targets(url):
    with pytest.raises(ValueError):
        accounts.safe_document_url(url, redirect=True)


@pytest.mark.parametrize(
    "text,expected", [("(1,714)", "-1714"), ("1,040", "1040"), ("-", "0"), ("3.25", "3.25")]
)
def test_number_sign_and_scale(text, expected):
    assert accounts.number(text) == Decimal(expected)


def box(text, x, y, score=1):
    return {
        "text": text,
        "score": score,
        "box": [[x, y], [x + 40, y], [x + 40, y + 20], [x, y + 20]],
    }


def document():
    boxes = [
        box("Example Limited", 140, 80),
        box("Statement of Comprehensive Income", 140, 180),
        box("28 March", 800, 260),
        box("29 March", 980, 260),
        box("2026", 815, 285),
        box("2025", 985, 285),
        box("£'m", 815, 310),
        box("£'m", 985, 310),
        box("Revenue", 140, 355),
        box("2", 650, 355),
        box("4,624", 815, 355),
        box("4,489", 985, 355),
        box("Operating profit", 140, 400),
        box("327", 815, 400),
        box("492", 985, 400),
    ]
    return {"pages": [{"page": 56, "boxes": boxes}]}


def test_table_preserves_current_and_comparative_columns():
    current, gaps = accounts.extract_figures(document(), "2026-03-28", "EXAMPLE LIMITED")
    prior, _ = accounts.extract_figures(document(), "2025-03-29", "EXAMPLE LIMITED", column=1)
    assert not any("withheld" in gap for gap in gaps)
    assert current["revenue"]["value"] == "4624"
    assert prior["revenue"]["value"] == "4489"
    assert current["revenue"]["scale"] == 1000000
    assert current["revenue"]["currency"] == "GBP"
    assert current["revenue"]["page"] == 56


def test_scope_date_units_and_low_confidence_fail_closed():
    assert not accounts.extract_figures(document(), "2026-03-27", "EXAMPLE LIMITED")[0]
    assert not accounts.extract_figures(document(), "2026-03-28", "DIFFERENT LIMITED")[0]
    group = document()
    group["pages"][0]["boxes"][1]["text"] = "Consolidated Statement of Comprehensive Income"
    assert not accounts.extract_figures(group, "2026-03-28", "EXAMPLE LIMITED")[0]
    unclear = document()
    unclear["pages"][0]["boxes"][6]["text"] = "?"
    assert not accounts.extract_figures(unclear, "2026-03-28", "EXAMPLE LIMITED")[0]
    low = document()
    low["pages"][0]["boxes"][10]["score"] = 0.8
    assert "revenue" not in accounts.extract_figures(low, "2026-03-28", "EXAMPLE LIMITED")[0]


def test_apostrophe_noise_does_not_discard_a_label():
    doc = document()
    doc["pages"][0]["boxes"][8]["text"] = "T'otal assets"
    figures, _ = accounts.extract_figures(doc, "2026-03-28", "EXAMPLE LIMITED")
    assert figures["total_assets"]["value"] == "4624"


@pytest.mark.asyncio
async def test_download_does_not_forward_key_to_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "CACHE", tmp_path)
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "test-key")
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.host.startswith("document-api"):
            assert request.headers.get("authorization")
            if request.url.path.endswith("/content"):
                return httpx.Response(
                    302,
                    headers={"location": "https://s3.eu-west-2.amazonaws.com/test/document.pdf"},
                )
            return httpx.Response(200, json={"resources": {"application/pdf": {}}})
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"%PDF-test")

    client = httpx.AsyncClient
    monkeypatch.setattr(
        accounts.httpx,
        "AsyncClient",
        lambda **kw: client(transport=httpx.MockTransport(handle), **kw),
    )
    path = await accounts.download_document(
        "https://document-api.company-information.service.gov.uk/document/abc"
    )
    assert path.read_bytes() == b"%PDF-test"
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_missing_accounts_explicit_not_fabricated():
    async def registry(*args):
        return {"items": []}

    result = await accounts.collect_accounts("12345678", "Example", registry)
    assert result["filings"] == []
    assert any("Fewer than two" in gap for gap in result["gaps"])


def test_growth_requires_reconciled_comparative():
    def entry(period, revenue):
        return {
            "period": period,
            "filed": period,
            "description": "full",
            "url": "https://example.com/accounts",
            "figures": {
                "revenue": {
                    "value": str(revenue),
                    "scale": 1000000,
                    "currency": "GBP",
                    "scope": "company",
                    "page": 1,
                    "excerpt": str(revenue),
                }
            },
        }

    new, old = entry("2026-03-28", 4624), entry("2025-03-29", 4489)
    new["comparatives"] = {"revenue": dict(old["figures"]["revenue"])}
    bundle, groups = EvidenceBundle(evidence=[], uncertainty=[]), {}
    add_financial_evidence(bundle, groups, {"filings": [new, old], "gaps": []})
    assert "increased 3.01%" in json.dumps(bundle.model_dump(mode="json"))
    new["comparatives"]["revenue"]["value"] = "4000"
    bundle, groups = EvidenceBundle(evidence=[], uncertainty=[]), {}
    add_financial_evidence(bundle, groups, {"filings": [new, old], "gaps": []})
    assert not groups["financial_observations"]
    assert any("growth calculation withheld" in gap for gap in bundle.uncertainty)
