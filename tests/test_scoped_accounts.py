import copy
import json
from pathlib import Path

from agent_ui.financial_health import add_financial_evidence
from agent_ui.local_report import build_evidence, render_report
from agent_ui.scoped_accounts import extract_scoped_figures


def document():
    return json.loads(
        (Path(__file__).parent / "fixtures/chetwood_statements_2025.json").read_text()
    )


def extract(doc, scope="company", period="2025-03-31", name="CHETWOOD FINANCIAL LIMITED"):
    return extract_scoped_figures(doc, period, name, scope)[0]


def test_real_bank_columns_keep_scope_period_sign_and_units():
    doc = document()
    company, group = extract(doc), extract(doc, "group")
    assert company["profit_before_tax"]["value"] == "2760"
    assert group["profit_after_tax"]["value"] == "-4702"
    assert company["cash"]["value"] == "357292"
    assert group["cash"]["value"] == "387550"
    assert company["operating_cash_flow"]["value"] == "19517"
    assert group["operating_cash_flow"]["value"] == "43038"
    assert company["total_assets"]["scale"] == 1000
    assert company["total_assets"]["value"] == "4529104"
    assert group["total_assets"]["value"] == "4532547"
    prior = extract(doc, "company", "2024-03-31")
    assert prior["profit_before_tax"]["value"] == "-24798"
    assert prior["operating_cash_flow"]["value"] == "177086"
    assert "net_interest_income" not in extract(doc, "group", "2024-03-31")  # low OCR confidence


def test_missing_scope_or_wrong_identity_date_are_not_inferred():
    doc = document()
    assert not extract(doc, name="DIFFERENT LIMITED")
    assert not extract(doc, period="2025-03-30")
    cash = copy.deepcopy(doc["pages"][-1])
    cash["boxes"] = [b for b in cash["boxes"] if b["text"] not in ("Group", "Company")]
    assert not extract({"pages": [cash]})


def test_prior_filing_high_resolution_retains_partial_results_without_guessing():
    doc = json.loads((Path(__file__).parent / "fixtures/chetwood_statements_2024.json").read_text())
    company = extract(doc, "company", "2024-03-31")
    group = extract(doc, "group", "2024-03-31")
    assert company["customer_deposits"]["value"] == "2856737"
    assert company["total_assets"]["value"] == "3068593"
    assert group["net_interest_income"]["value"] == "44316"
    assert group["profit_before_tax"]["value"] == "-8861"
    assert "cash" not in company  # ambiguous OCR remains unavailable


def test_conflicting_cash_withheld_within_scope_only():
    doc = document()
    for box in doc["pages"][-1]["boxes"]:
        if box["text"] == "357,292":
            box["text"] = "357,293"
    assert "cash" not in extract(doc)
    assert extract(doc, "group")["cash"]["value"] == "387550"


def test_bank_report_cites_separate_group_loss_company_profit():
    doc = document()
    filings = []
    for period in ["2025-03-31", "2024-03-31"]:
        filings.append(
            {
                "period": period,
                "filed": "2025-12-20",
                "description": "group",
                "banking": True,
                "url": "https://example.com/accounts",
                "figures": extract(doc, "company", period),
                "group_figures": extract(doc, "group", period),
            }
        )
    filings[0].update(
        comparatives=filings[1]["figures"], group_comparatives=filings[1]["group_figures"]
    )
    bundle, groups = build_evidence(
        {"company_number": "09964966", "profile": {"company_name": "CHETWOOD FINANCIAL LIMITED"}},
        None,
        {},
    )
    add_financial_evidence(bundle, groups, {"filings": filings, "gaps": []})
    report = render_report(bundle, groups, [])
    summary = report.split("## Business Model Summary")[0]
    assert "Company: 2025-03-31: profit before tax GBP 2.760m" in summary
    assert "Group: 2025-03-31: profit after tax GBP -4.702m" in summary
    assert "Company: 2025-03-31: cash GBP 357.292m" in summary
    assert "not a regulatory liquidity or capital ratio" in summary
    assert "capital adequacy" in summary
    assert "current ratio" not in report
    by_id = {e.evidence_id: e for e in bundle.evidence}
    for eid in groups["financial_observations"]:
        f = by_id[eid].structured_fact["financial_observation"]
        scopes = {next(iter(by_id[i].structured_fact.values())).get("scope") for i in f["inputs"]}
        assert len(scopes) == 1
