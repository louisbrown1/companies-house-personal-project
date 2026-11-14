import asyncio
import json
from pathlib import Path

import httpx
import pytest

from agent_ui.pillar3 import extract_pillar3, fetch_public, publisher_matches


def source():
    return [
        (Path(__file__).parent / "fixtures/allica_pillar3_2025.txt").read_text(encoding="utf-8")
    ]


def test_pillar3_ratios_keep_literal_values_dates_and_averaging_windows():
    metrics = extract_pillar3(source(), "ALLICA BANK LIMITED", "2025-12-31")
    found = {(m["metric"], m["period"]): m for m in metrics}
    assert len(found) == 8
    assert found["nsfr", "2025-12-31"]["value"] == "138.8"
    assert found["nsfr", "2025-12-31"]["averaging_window"] == "4-quarter"
    assert found["lcr", "2025-12-31"]["value"] == "220.8"
    assert found["lcr", "2025-12-31"]["averaging_window"] == "12-month"
    assert found["cet1", "2025-12-31"]["value"] == "13.4"
    assert all(m["scope"] == "group" for m in metrics)


@pytest.mark.parametrize(
    "name,period", [("OTHER BANK LIMITED", "2025-12-31"), ("ALLICA BANK LIMITED", "2024-12-31")]
)
def test_wrong_issuer_or_period_is_withheld(name, period):
    assert not extract_pillar3(source(), name, period)


def test_missing_average_basis_is_not_guessed():
    pages = [
        source()[0]
        .replace("12-month average", "unspecified method")
        .replace("4-quarter average", "unspecified method")
    ]
    assert {m["metric"] for m in extract_pillar3(pages, "ALLICA BANK LIMITED", "2025-12-31")} == {
        "cet1",
        "total_capital_ratio",
    }


def test_conflicting_disclosure_rows_are_withheld():
    pages = [source()[0] + "\n20 NSFR ratio (%) 999.0 % 999.0 %"]
    assert not any(
        m["metric"] == "nsfr" for m in extract_pillar3(pages, "ALLICA BANK LIMITED", "2025-12-31")
    )


def test_publisher_domain_is_only_a_candidate_filter():
    assert publisher_matches("https://www.allica.bank/investor-relations", "ALLICA BANK LIMITED")
    assert not publisher_matches("https://example.com/allica.bank", "ALLICA BANK LIMITED")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/a.pdf",
        "https://user:pass@example.com/a.pdf",
        "https://elsewhere.com/a.pdf",
    ],
)
def test_disclosure_fetch_rejects_unsafe_origins(url):
    async def run():
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError):
                await fetch_public(client, url, "example.com")

    asyncio.run(run())


def test_disclosure_fetch_rejects_private_dns(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))]
    )

    async def run():
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match="Non-public"):
                await fetch_public(client, "https://example.com/a.pdf", "example.com")

    asyncio.run(run())


def test_supplementary_evidence_has_its_own_source_and_basis():
    from agent_ui.banking_notes import add_banking_note_evidence
    from agent_ui.validation import EvidenceBundle

    metrics = extract_pillar3(source(), "ALLICA BANK LIMITED", "2025-12-31")
    b = EvidenceBundle(evidence=[], uncertainty=[])
    g = {}
    add_banking_note_evidence(
        b,
        g,
        [
            {
                "period": "2025-12-31",
                "url": "https://example.com/pillar3.pdf",
                "source_kind": "other",
                "banking_notes": {"metrics": metrics, "gaps": [], "disclosures": []},
            }
        ],
    )
    assert all(e.source.kind == "other" for e in b.evidence)
    observations = [
        e.structured_fact["financial_observation"]["observation"]
        for e in b.evidence
        if e.claim == "financial_observation"
    ]
    assert any("4-quarter average" in s and "138.8%" in s for s in observations)
    assert any("12-month average" in s and "220.8%" in s for s in observations)


def test_discovery_follows_descriptive_links_and_rejects_old_period(monkeypatch):
    from agent_ui import pillar3

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    queries = []

    class SearchClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            queries.append(kwargs["json"]["query"])
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={"results": [{"url": "https://www.allica.bank/old-pillar3-2024.pdf"}]},
            )

    async def fetch(client, url, host):
        assert host == "www.allica.bank"
        if url.endswith("old-pillar3-2024.pdf"):
            return b"old", url
        if url.endswith("/download.pdf"):
            return b"current", url
        return (
            b'ALLICA BANK LIMITED company number 07706156 <a href="/download.pdf">Pillar III current disclosures</a>',
            url,
        )

    monkeypatch.setattr(pillar3.httpx, "AsyncClient", lambda **kwargs: SearchClient())
    monkeypatch.setattr(pillar3, "fetch_public", fetch)
    monkeypatch.setattr(
        pillar3,
        "pdf_pages",
        lambda data: source() if data == b"current" else [source()[0].replace("2025", "2023")],
    )
    filings, gap = asyncio.run(
        pillar3.collect_pillar3("ALLICA BANK LIMITED", "07706156", "2025-12-31")
    )
    assert gap is None and filings[0]["url"].endswith("download.pdf")
    assert len(queries) == 2
    assert filings[0]["banking_notes"]["metrics"][0]["issuer_company_number"] == "07706156"


def test_parent_group_disclosures_are_not_relabelled_as_the_bank():
    pages = [source()[0].replace("Allica Bank Limited", "Allica Group Limited")]
    assert not extract_pillar3(pages, "ALLICA BANK LIMITED", "2025-12-31")


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True])
async def test_browser_discovery_still_requires_publisher_identity_and_pdf_validation(
    monkeypatch, external
):
    from agent_ui import browser_research, pillar3

    monkeypatch.setenv("BROWSER_RESEARCH_ENABLED", "1")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    async def discover(*args, **kwargs):
        return browser_research.BrowserDiscovery(
            status="completed",
            pages=[
                browser_research.BrowserPage(
                    url="https://www.allica.bank/legal",
                    text="ALLICA BANK LIMITED company number 07706156",
                    retrieved_at="2026-11-13T00:00:00Z",
                )
            ],
            links=[] if external else ["https://www.allica.bank/pillar3.pdf"],
            documents=[
                {
                    "url": "https://cdn.example.com/pillar3.pdf",
                    "publisher_page": "https://www.allica.bank/legal",
                    "label": "Pillar 3",
                }
            ]
            if external
            else [],
        )

    async def fetch(client, url, host):
        if url.endswith("pillar3.pdf"):
            return b"validated-document", url
        raise httpx.ConnectError("HTML unavailable; browser evidence retained")

    monkeypatch.setattr(browser_research, "discover", discover)
    monkeypatch.setattr(pillar3, "fetch_public", fetch)
    monkeypatch.setattr(pillar3, "pdf_pages", lambda body: source())
    filings, gap = await pillar3.collect_pillar3("ALLICA BANK LIMITED", "07706156", "2025-12-31")
    assert gap is None and filings[0]["publisher_page"] == "https://www.allica.bank/legal"
    assert filings[0]["banking_notes"]["metrics"][0]["issuer_company_number"] == "07706156"
    filings, gap = await pillar3.collect_pillar3("ALLICA BANK LIMITED", "99999999", "2025-12-31")
    assert not filings and gap


def km1_source(bank):
    return json.loads(
        (Path(__file__).parent / f"fixtures/{bank}_pillar3_pages.json").read_text(encoding="utf-8")
    )


def test_dual_scope_km1_selects_bank_columns_and_bound_footnotes():
    metrics = extract_pillar3(km1_source("aldermore"), "ALDERMORE BANK PLC", "2026-06-30")
    assert len(metrics) == 12
    current = {m["metric"]: m for m in metrics if m["period"] == "2026-06-30"}
    assert {key: m["value"] for key, m in current.items()} == {
        "cet1": "17.7",
        "total_capital_ratio": "22.0",
        "lcr": "164.6",
        "nsfr": "125.4",
    }
    assert all(m["scope"] == "company" and m["page"] == 6 for m in metrics)
    assert current["lcr"]["averaging_window"] == "12-month"
    assert current["nsfr"]["averaging_window"] == "4-quarter"


def test_single_year_km1_continuation_keeps_unknown_basis():
    metrics = extract_pillar3(km1_source("chetwood"), "CHETWOOD FINANCIAL LIMITED", "2025-03-31")
    assert {m["metric"]: m["value"] for m in metrics} == {
        "cet1": "15.2",
        "total_capital_ratio": "15.2",
        "lcr": "194",
        "nsfr": "145",
    }
    assert all(m["scope"] == "bank (regulatory perimeter unspecified)" for m in metrics)
    liquidity = [m for m in metrics if m["metric"] in {"lcr", "nsfr"}]
    assert all(
        m["reporting_basis"] == "unspecified" and m["averaging_window"] is None for m in liquidity
    )


@pytest.mark.parametrize(
    "old,new",
    [
        ("Group Bank", "Bank Group"),
        ("30-\r\nJun-26", "29-\r\nJun-26"),
        ("Aldermore Bank PLC’s", "Another Bank PLC’s"),
    ],
)
def test_ambiguous_km1_scope_or_dates_are_withheld(old, new):
    pages = km1_source("aldermore")
    assert any(old in p for p in pages)
    assert not extract_pillar3(
        [p.replace(old, new) for p in pages], "ALDERMORE BANK PLC", "2026-06-30"
    )


def test_km1_column_count_and_unknown_footnotes():
    pages = [
        p.replace("13.6 14.7 14.9 17.7 18.3 18.4", "13.6 14.7 14.9 17.7 18.3").replace(
            "12 month average", "unspecified calculation"
        )
        for p in km1_source("aldermore")
    ]
    metrics = extract_pillar3(pages, "ALDERMORE BANK PLC", "2026-06-30")
    assert not any(m["metric"] == "cet1" for m in metrics)
    assert all(m["reporting_basis"] == "unspecified" for m in metrics if m["metric"] == "lcr")


def test_km1_rules_are_not_company_specific():
    pages = [
        p.replace("Chetwood Financial Limited", "Example Finance Limited")
        for p in km1_source("chetwood")
    ]
    assert len(extract_pillar3(pages, "EXAMPLE FINANCE LIMITED", "2025-03-31")) == 4
    assert not extract_pillar3(pages, "EXAMPLE FINANCE LIMITED", "2024-03-31")
    assert not extract_pillar3(pages, "ANOTHER FINANCE LIMITED", "2025-03-31")


def test_hydrated_disclosure_links_are_read_without_executing_scripts():
    from agent_ui.pillar3 import page_links

    payload = json.dumps({"url": "/documents/pillar-three-disclosures.pdf"})
    markup = "<script>self.__next_f.push([1," + json.dumps(payload) + "])</script>"
    assert ("https://example.com/documents/pillar-three-disclosures.pdf", "") in page_links(
        markup, "https://example.com/"
    )


def test_publisher_crn_zero_padding_requires_company_label():
    from agent_ui.pillar3 import publisher_identity

    assert publisher_identity(
        "Example Bank PLC Company No. 947662.", "EXAMPLE BANK PLC", "00947662"
    )
    assert not publisher_identity(
        "Example Bank PLC FCA registration number 947662.", "EXAMPLE BANK PLC", "00947662"
    )
    assert not publisher_identity(
        "Example Bank PLC Company No. 947663.", "EXAMPLE BANK PLC", "00947662"
    )
    assert not publisher_identity(
        "Other Bank PLC Company No. 947662.", "EXAMPLE BANK PLC", "00947662"
    )


@pytest.mark.parametrize(
    "jurisdiction,number",
    [("England and Wales", "09446231"), ("Scotland", "SC123456"), ("Northern Ireland", "NI123456")],
)
def test_registered_company_sentence_binds_exact_legal_name(jurisdiction, number):
    from agent_ui.pillar3 import publisher_identity

    text = f"Example Bank Limited is a company registered in {jurisdiction} (No.{number})."
    assert publisher_identity(text, "EXAMPLE BANK LIMITED", number)
    assert not publisher_identity(text, "OTHER BANK LIMITED", number)
    assert not publisher_identity(text, "EXAMPLE BANK LIMITED", "99999999")


@pytest.mark.parametrize(
    "text",
    [
        "Example Bank Limited is regulated by the FCA (No.09446231).",
        "Example Bank Limited is a company registered in England and Wales. FCA No.09446231.",
        "Example Bank Limited. Other Bank Limited is a company registered in England and Wales (No.09446231).",
        "<script>Example Bank Limited is a company registered in England and Wales (No.09446231).</script>",
    ],
)
def test_registration_sentence_cannot_bind_regulator_or_other_company(text):
    from agent_ui.pillar3 import publisher_identity

    assert not publisher_identity(text, "EXAMPLE BANK LIMITED", "09446231")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,mime,size,declared,ok",
    [
        ("/report.pdf", "application/octet-stream", 20, None, True),
        ("/download", "application/pdf", 32, None, True),
        ("/report.pdf", "application/pdf", 33, None, False),
        ("/report.pdf", "application/pdf", 1, "33", False),
        ("/report.pdf", "application/pdf", 33, "1", False),
        ("/page", "text/html", 11, None, False),
    ],
)
async def test_pdf_and_html_downloads_have_separate_streamed_limits(
    monkeypatch, path, mime, size, declared, ok
):
    import socket

    from agent_ui import pillar3

    monkeypatch.setattr(pillar3, "MAX_BYTES", 10)
    monkeypatch.setattr(pillar3, "MAX_PDF_BYTES", 32)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443))])
    consumed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(0, size, 5):
                consumed.append(i)
                yield b"x" * min(5, size - i)

    headers = {"content-type": mime}
    if declared is not None:
        headers["content-length"] = declared
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers=headers, stream=Stream())
        )
    ) as client:
        if ok:
            body, _ = await pillar3.fetch_public(
                client, "https://example.com" + path, "example.com"
            )
            assert len(body) == size
        else:
            with pytest.raises(ValueError, match="size budget"):
                await pillar3.fetch_public(client, "https://example.com" + path, "example.com")
    if declared == "33":
        assert consumed == []
