from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent_ui import peer_comparison as peers
from agent_ui.peer_models import PeerComparison, PeerMeasurement
from agent_ui.validation import EvidenceBundle


def accounts(period="2025-03-31", loans="80", deposits="100", scale=1000000):
    figures = {
        key: dict(
            value=value,
            currency="GBP",
            scale=scale,
            period=period,
            scope="company",
            page=2,
            excerpt=f"{key} {value}",
        )
        for key, value in {
            "customer_loans": loans,
            "customer_deposits": deposits,
            "total_equity": "10",
            "total_assets": "200",
        }.items()
    }
    return {
        "filings": [
            {"period": period, "url": "https://example.com/accounts.pdf", "figures": figures}
        ],
        "gaps": [],
    }


def policy():
    return peers.load_peer_runbook("bank")[0]


def measures(number="12345678", **kwargs):
    return peers.measurements(number, accounts(**kwargs), policy())[0]


def test_scale_normalisation_and_percentage_point_difference():
    data = accounts()
    data["filings"][0]["figures"]["customer_loans"].update(value="80000", scale=1000)
    left, _ = peers.measurements("12345678", data, policy())
    right = measures("87654321", loans="70")
    comparisons, _ = peers.compare_measurements(left, right, policy())
    assert comparisons[0].subject.value == 80
    assert comparisons[0].difference == 10
    assert comparisons[0].difference_unit == "percentage_points"


def test_asset_size_gate_uses_comparison_period_and_rejects_conflicts():
    comparison = peers.compare_measurements(measures(), measures("87654321"), policy())[0][0]
    subject, peer = accounts(), accounts()
    assert peers.size_compatibility(subject, peer, comparison, 10) is None
    peer["filings"][0]["figures"]["total_assets"]["value"] = "2001"
    assert "10" in peers.size_compatibility(subject, peer, comparison, 10)
    peer = accounts(period="2024-03-31")
    assert "could not be validated" in peers.size_compatibility(subject, peer, comparison, 10)
    peer = accounts()
    conflicting = deepcopy(peer["filings"][0])
    conflicting["figures"]["total_assets"]["value"] = "201"
    peer["filings"].append(conflicting)
    assert "could not be validated" in peers.size_compatibility(subject, peer, comparison, 10)


@pytest.mark.parametrize("denominator", ["0", "-100"])
def test_invalid_denominators_withheld(denominator):
    values, gaps = peers.measurements("12345678", accounts(deposits=denominator), policy())
    assert "loans_to_deposits" not in {v.metric for v in values}
    assert gaps


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "group"},
        {"period": "2024-03-31"},
        {"currency": "USD"},
        {"value": "NaN"},
        {"excerpt": ""},
    ],
)
def test_invalid_or_incompatible_source_inputs_rejected(change):
    data = accounts()
    data["filings"][0]["figures"]["customer_loans"].update(change)
    values, _ = peers.measurements("12345678", data, policy())
    assert "loans_to_deposits" not in {v.metric for v in values}


def test_period_window_enforced_and_disclosed():
    values, gaps = peers.compare_measurements(
        measures(), measures("87654321", period="2025-12-31"), policy()
    )
    assert not values and any("withheld" in g for g in gaps)
    values, _ = peers.compare_measurements(
        measures(), measures("87654321", period="2025-06-30"), policy()
    )
    assert values[0].period_gap_days == 91


def test_unknown_regulatory_basis_not_assumed_point_in_time():
    data = accounts()
    data["filings"][0]["banking_notes"] = {
        "metrics": [
            dict(
                metric="lcr",
                value="200",
                scale=1,
                unit="%",
                period="2025-03-31",
                scope="company",
                page=3,
                excerpt="Liquidity coverage ratio 200%",
                reporting_basis="unspecified",
            )
        ]
    }
    values, gaps = peers.measurements("12345678", data, policy())
    assert "lcr" not in {v.metric for v in values}
    assert any("basis" in g for g in gaps)


def test_different_regulatory_averaging_windows_are_not_comparable():
    readings = []
    for number, window in [("12345678", "12 months"), ("87654321", "3 months")]:
        data = accounts()
        data["filings"][0]["banking_notes"] = {
            "metrics": [
                dict(
                    metric="lcr",
                    value="200",
                    scale=1,
                    unit="%",
                    period="2025-03-31",
                    scope="company",
                    page=3,
                    excerpt="Liquidity coverage ratio 200%",
                    reporting_basis="average",
                    averaging_window=window,
                )
            ]
        }
        readings.append(peers.measurements(number, data, policy())[0])
    comparisons, gaps = peers.compare_measurements(*readings, policy())
    assert "lcr" not in {c.subject.metric for c in comparisons}
    assert any("lcr" in g for g in gaps)


def test_schema_rejects_fabricated_arithmetic_and_mixed_source_scope():
    comparisons, _ = peers.compare_measurements(measures(), measures("87654321"), policy())
    raw = comparisons[0].model_dump(mode="json")
    raw["difference"] = "99"
    with pytest.raises(ValidationError):
        PeerComparison.model_validate(raw)
    raw = measures()[0].model_dump(mode="json")
    raw["sources"][0]["scope"] = "group"
    with pytest.raises(ValidationError):
        PeerMeasurement.model_validate(raw)


def test_conflicting_duplicate_filings_withheld():
    data = accounts()
    data["filings"].extend(accounts(loans="99")["filings"])
    values, gaps = peers.measurements("12345678", data, policy())
    assert "loans_to_deposits" not in {v.metric for v in values}
    assert any("conflicting" in g for g in gaps)


def test_historical_peer_period_selected_as_whole_not_per_metric():
    data = accounts(period="2026-03-31")
    prior = accounts(loans="50")["filings"][0]
    del prior["figures"]["total_equity"]
    data["filings"].append(prior)
    values, _ = peers.measurements("87654321", data, policy(), date(2025, 3, 31))
    assert [(v.metric, v.value) for v in values] == [("loans_to_deposits", Decimal(50))]


@pytest.mark.parametrize("sector", ["bank", "retail", "trading"])
def test_packaged_runbooks_are_valid_and_bounded(sector):
    p, guidance, digest = peers.load_peer_runbook(sector)
    assert p.max_peers == 2 and p.max_candidates == 20 and guidance and len(digest) == 64


@pytest.mark.asyncio
async def test_discovery_identity_validation_self_exclusion_and_timeout_preservation(monkeypatch):
    monkeypatch.setattr(
        peers,
        "discover_web_peers",
        stub_discovery(["12345678", "11111111", "22222222", "33333333"]),
    )
    calls = []
    pack = {"company_number": "12345678", "profile": {"sic_codes": ["64191"]}}

    async def registry(name, args):
        calls.append((name, args))
        if name == "advanced_company_search":
            assert "location" not in args
            return {
                "items": [
                    {"company_number": n, "company_name": "Example bank"}
                    for n in ["12345678", "11111111", "22222222", "33333333"]
                ]
            }
        n = args["company_number"]
        return {
            "company_number": "99999999" if n == "11111111" else n,
            "company_name": "Example bank",
            "company_status": "active",
            "sic_codes": ["64191"],
            "accounts": {"last_accounts": {"made_up_to": "2025-03-31"}},
        }

    async def collect(number, *args, **kwargs):
        assert number != "11111111"
        if number == "33333333":
            raise TimeoutError()
        return accounts(loans="70")

    monkeypatch.setattr(peers, "collect_accounts", collect)
    report, _ = await peers.collect_peer_comparison(pack, accounts(), registry, "bank")
    assert report.status == "partial"
    assert [p.company_number for p in report.peers] == ["22222222"]
    assert report.comparisons[0].difference == 10
    assert any("budget" in g for g in report.gaps)
    bundle, groups = EvidenceBundle(evidence=[], uncertainty=[]), {}
    peers.add_peer_evidence(bundle, groups, report)
    assert bundle.evidence[0].typed_fact.difference == 10
    assert "Financial peer comparison" in "\n".join(peers.render_peers(groups))


@pytest.mark.asyncio
async def test_no_sector_evidence_does_not_trigger_peer_downloads():
    async def registry(*args):
        pytest.fail("No discovery should occur without subject sector evidence")

    report, _ = await peers.collect_peer_comparison(
        {"company_number": "12345678"}, {"filings": []}, registry, "bank"
    )
    assert report.status == "unavailable"


@pytest.mark.asyncio
async def test_peer_stage_respects_remaining_report_deadline():
    async def registry(*args):
        pytest.fail("Peer discovery must not start after its deadline")

    report, _ = await peers.collect_peer_comparison(
        {"company_number": "12345678", "profile": {"sic_codes": ["64191"]}},
        accounts(),
        registry,
        "bank",
        deadline=0,
    )
    assert not report.comparisons
    assert any("remaining report time" in gap for gap in report.gaps)


@pytest.mark.asyncio
async def test_explicit_peers_still_require_registry_and_sector_validation(monkeypatch):
    monkeypatch.setenv("PEER_COMPANY_NUMBERS", "12345678,87654321")

    async def registry(name, args):
        assert name == "company_profile"
        assert args["company_number"] == "87654321"
        return dict(
            company_number="87654321",
            company_name="Peer bank",
            company_status="active",
            sic_codes=["64191"],
            accounts={"last_accounts": {"made_up_to": "2025-03-31"}},
        )

    async def collect(*args, **kwargs):
        return accounts(loans="70")

    monkeypatch.setattr(peers, "collect_accounts", collect)
    report, _ = await peers.collect_peer_comparison(
        {"company_number": "12345678", "profile": {"sic_codes": ["64191"]}},
        accounts(),
        registry,
        "bank",
    )
    assert report.selection_mode == "explicit"
    assert report.candidates_considered == 1
    assert report.comparisons[0].difference == 10


@pytest.mark.parametrize("numbers", [["invalid"], ["11111111", "22222222", "33333333"]])
def test_invalid_explicit_peer_configuration_rejected(numbers):
    from agent_ui.peer_models import PeerSelection

    with pytest.raises(ValidationError):
        PeerSelection(company_numbers=numbers)


@pytest.mark.asyncio
@pytest.mark.parametrize("include_notes", [True, False])
@pytest.mark.parametrize("sector", ["bank", "trading"])
async def test_accounts_can_skip_note_search_without_skipping_identity_or_figures(
    monkeypatch, tmp_path, include_notes, sector
):
    from agent_ui import accounts as extractor
    from agent_ui import (
        banking_notes,
        document_context,
        hybrid_accounts,
        page_selection,
        scoped_accounts,
        table_interpretation,
    )

    monkeypatch.setenv("DOCUMENT_MEMORY_ENABLED", "0")
    calls = []
    document = {"sha256": "test", "pages": [], "page_count": 1}
    figures = {
        name: {}
        for name in [
            "profit_before_tax",
            "profit_after_tax",
            "total_assets",
            "interest_income",
            "customer_deposits",
            "revenue",
            "operating_profit",
        ]
    }

    async def registry(*args):
        return {
            "items": [
                dict(
                    category="accounts",
                    transaction_id="abc",
                    date="2025-04-01",
                    description="full",
                    description_values={"made_up_date": "2025-03-31"},
                    links={"document_metadata": "https://example.com/document"},
                )
            ]
        }

    async def download(*args):
        return tmp_path / "source.pdf"

    def bind(doc, *args):
        calls.append("identity")
        return {**doc, "identity": {"verified": True}}

    async def enrich(*args):
        calls.append("validated_mapping")

    async def select(*args, **kwargs):
        calls.append("notes")
        return document, {"selected_pages": []}

    monkeypatch.setattr(extractor, "download_document", download)
    monkeypatch.setattr(extractor, "read_statement_pages", lambda *args: document)
    monkeypatch.setattr(document_context, "bind_document", bind)
    monkeypatch.setattr(table_interpretation, "prepare_context", lambda path, doc: doc)
    monkeypatch.setattr(hybrid_accounts, "enrich_filing", enrich)
    monkeypatch.setattr(scoped_accounts, "extract_scoped_figures", lambda *args: (figures, []))
    monkeypatch.setattr(extractor, "extract_figures", lambda *args: (figures, []))
    monkeypatch.setattr(page_selection, "select_note_pages", select)
    monkeypatch.setattr(
        banking_notes, "extract_banking_notes", lambda *args: {"metrics": [], "retry_pages": []}
    )
    result = await extractor.collect_accounts(
        "12345678", "Example", registry, sector, include_notes=include_notes
    )
    assert calls[:2] == ["identity", "validated_mapping"]
    assert ("notes" in calls) is include_notes
    assert result["filings"][0]["figures"] == figures


@pytest.mark.asyncio
async def test_historical_benchmark_is_explicit_and_dormant_peer_excluded(monkeypatch):
    monkeypatch.setattr(peers, "discover_web_peers", stub_discovery(["11111111", "22222222"]))
    pack = {"company_number": "12345678", "profile": {"sic_codes": ["64191"]}}
    data = accounts(period="2026-06-30")
    data["filings"].extend(accounts(period="2025-06-30")["filings"])

    async def registry(name, args):
        if name == "advanced_company_search":
            return {
                "items": [
                    {"company_number": n, "company_name": "Test bank"}
                    for n in ["11111111", "22222222"]
                ]
            }
        n = args["company_number"]
        return {
            "company_number": n,
            "company_name": "Test bank",
            "company_status": "active",
            "sic_codes": ["64191"],
            "accounts": {
                "last_accounts": {
                    "made_up_to": "2025-03-31",
                    "type": "dormant" if n == "11111111" else "full",
                }
            },
        }

    async def collect(number, *args, **kwargs):
        assert number == "22222222"
        return accounts()

    monkeypatch.setattr(peers, "collect_accounts", collect)
    report, _ = await peers.collect_peer_comparison(pack, data, registry, "bank")
    assert report.comparisons
    assert all(c.subject.period == date(2025, 6, 30) for c in report.comparisons)
    assert any("historical subject period" in g for g in report.gaps)


@pytest.mark.asyncio
async def test_peer_filing_is_selected_before_ocr_and_metadata_preserved():
    history = {
        "items": [
            dict(
                category="accounts",
                date=p,
                description_values={"made_up_date": p},
                links={"document_metadata": f"https://example.com/{p}"},
            )
            for p in ["2026-03-31", "2025-03-31"]
        ]
    }

    async def registry(*args):
        return history

    call = peers.benchmark_registry(registry, "87654321", {date(2025, 3, 31)}, 93)
    result = await call("filing_history", {"company_number": "87654321"})
    assert result["items"] == [history["items"][1]]
    assert len(history["items"]) == 2
    # Other calls pass through unchanged.
    assert await call("company_profile", {"company_number": "87654321"}) is history


@pytest.mark.asyncio
async def test_incompatible_filing_dates_are_explained_without_relaxing_window():
    async def registry(*args):
        return {
            "items": [
                {
                    "category": "accounts",
                    "date": "2026-04-01",
                    "description_values": {"made_up_date": "2025-12-31"},
                    "links": {"document_metadata": "https://example.com/document"},
                }
            ]
        }

    audit = {}
    wrapped = peers.benchmark_registry(
        registry, "87654321", {date(2025, 6, 30), date(2026, 6, 30)}, 93, audit
    )
    result = await wrapped("filing_history", {"company_number": "87654321"})
    assert result["items"] == []
    assert audit == {"available_periods": ["2025-12-31"], "selected_periods": []}


@pytest.mark.asyncio
async def test_date_rejection_does_not_consume_extraction_attempt(monkeypatch):
    monkeypatch.delenv("PEER_COMPANY_NUMBERS", raising=False)
    monkeypatch.setattr(peers, "discover_web_peers", stub_discovery(["11111111", "22222222"]))
    p, guidance, digest = peers.load_peer_runbook("bank")
    monkeypatch.setattr(
        peers,
        "load_peer_runbook",
        lambda sector: (p.model_copy(update={"max_attempts": 1}), guidance, digest),
    )

    async def registry(name, args):
        number = args["company_number"]
        period = "2025-12-31" if number == "11111111" else "2025-03-31"
        if name == "filing_history":
            return {
                "items": [
                    {
                        "category": "accounts",
                        "date": "2026-04-01",
                        "description_values": {"made_up_date": period},
                        "links": {"document_metadata": "https://example.com/document"},
                    }
                ]
            }
        return {
            "company_number": number,
            "company_name": "Example Bank",
            "company_status": "active",
            "sic_codes": ["64191"],
            "accounts": {"last_accounts": {"made_up_to": "2025-03-31"}},
        }

    downloads = []

    async def collect(number, name, wrapped, *args, **kwargs):
        history = await wrapped("filing_history", {"company_number": number})
        if not history["items"]:
            return {"filings": [], "gaps": []}
        downloads.append(number)
        return accounts(period="2025-03-31")

    monkeypatch.setattr(peers, "collect_accounts", collect)
    report, _ = await peers.collect_peer_comparison(
        {"company_number": "12345678", "profile": {"sic_codes": ["64191"]}},
        accounts(period="2025-06-30"),
        registry,
        "bank",
    )
    assert downloads == ["22222222"]
    assert report.comparisons
    assert "No filed accounts within 93 days" in report.discovery.candidates[0].reasons[-1]


def test_retail_current_ratio_uses_times_not_percentage_points():
    data = accounts()
    figures = data["filings"][0]["figures"]
    for key, value in [("revenue", "300"), ("current_assets", "60"), ("current_liabilities", "30")]:
        figures[key] = {**figures["total_assets"], "value": value}
    p, _, _ = peers.load_peer_runbook("retail")
    assert peers.sector_accounts_verified(data, "retail")
    a, _ = peers.measurements("12345678", data, p)
    b, _ = peers.measurements("87654321", data, p)
    comparisons, _ = peers.compare_measurements(a, b, p)
    current = next(c for c in comparisons if c.subject.metric == "current_ratio")
    assert current.subject.value == 2
    assert current.difference_unit == "times"


def test_peer_evidence_survives_full_report_citation_validation():
    from agent_ui.local_report import build_evidence, render_report
    from agent_ui.peer_models import PeerIdentity, PeerReport

    bundle, groups = build_evidence(
        {
            "company_number": "12345678",
            "profile": {"company_name": "Subject", "company_number": "12345678"},
        },
        None,
        {},
    )
    comparisons, _ = peers.compare_measurements(measures(), measures("87654321"), policy())
    result = PeerReport(
        sector="bank",
        runbook_sha256="test",
        status="partial",
        comparisons=comparisons,
        peers=[
            PeerIdentity(
                company_number="87654321",
                company_name="Peer",
                sic_codes=["64191"],
                registry_url="https://example.com/registry",
            )
        ],
    )
    peers.add_peer_evidence(bundle, groups, result)
    rendered = render_report(bundle, groups, [])
    assert "Financial peer comparison" in rendered
    assert "subject minus peer" in rendered
    for eid in groups["peer_comparisons"]:
        assert f"- {eid} -> https://example.com/accounts.pdf" in rendered


def test_average_windows_and_self_comparisons_rejected():
    a = measures()[0]
    with pytest.raises(ValidationError, match="itself"):
        PeerComparison(
            subject=a, peer=a, difference=0, difference_unit="percentage_points", period_gap_days=0
        )
    # Different scopes never match, even if numbers happen to coincide.
    raw = deepcopy(measures("87654321")[0].model_dump())
    raw["scope"] = "group"
    for s in raw["sources"]:
        s["scope"] = "group"
    values, _ = peers.compare_measurements([a], [PeerMeasurement.model_validate(raw)], policy())
    assert not values


def stub_discovery(numbers):
    from agent_ui.peer_discovery_models import CandidateDecision, DiscoveryAudit

    async def discover(*args, **kwargs):
        return DiscoveryAudit(
            status="partial",
            candidates=[
                CandidateDecision(
                    name="Example bank",
                    company_number=n,
                    status="shortlisted",
                    reasons=["Source-backed profile checked by discovery boundary"],
                )
                for n in numbers
            ],
        )

    return discover
