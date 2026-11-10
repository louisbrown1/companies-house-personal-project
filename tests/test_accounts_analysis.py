from agent_ui.financial_health import add_financial_evidence
from agent_ui.validation import EvidenceBundle


def filing(period, values):
    return {
        "period": period,
        "filed": period,
        "description": "full",
        "url": "https://example.com/accounts",
        "figures": {
            key: {
                "value": str(value),
                "scale": 1000000,
                "currency": "GBP",
                "scope": "company",
                "period": period,
                "page": 1,
                "excerpt": key,
            }
            for key, value in values.items()
        },
    }


def analyse(new, old=None):
    bundle, groups = EvidenceBundle(evidence=[], uncertainty=[]), {}
    add_financial_evidence(bundle, groups, {"filings": [new] + ([old] if old else []), "gaps": []})
    observations = [
        e.structured_fact["financial_observation"]["observation"]
        for e in bundle.evidence
        if e.claim == "financial_observation"
    ]
    return "\n".join(observations), bundle


def pair():
    new = filing(
        "2026-03-28",
        {
            "revenue": 4624,
            "cost_of_sales": -2966,
            "gross_profit": 1658,
            "administrative_expenses": -1331,
            "operating_profit": 327,
        },
    )
    old = filing(
        "2025-03-29",
        {
            "revenue": 4489,
            "cost_of_sales": -2813,
            "gross_profit": 1676,
            "administrative_expenses": -1184,
            "operating_profit": 492,
        },
    )
    new["comparatives"] = {k: dict(v) for k, v in old["figures"].items()}
    return new, old


def test_profit_bridge_reconciles_without_inventing_business_cause():
    text, _ = analyse(*pair())
    assert "cost-of-sales impact -153.00" in text
    assert "gross-profit movement -18.00" in text
    assert "administrative-expense impact -147.00" in text
    assert "total operating-profit movement -165.00" in text
    assert "does not establish their underlying business causes" in text


def test_inconsistent_bridge_is_withheld():
    new, old = pair()
    new["figures"]["operating_profit"]["value"] = "328"
    text, bundle = analyse(new, old)
    assert "Operating profit bridge (" not in text
    assert any("arithmetic does not reconcile" in gap for gap in bundle.uncertainty)


def test_restated_cost_line_withholds_bridge():
    new, old = pair()
    new["comparatives"]["cost_of_sales"]["value"] = "-2800"
    text, bundle = analyse(new, old)
    assert "Operating profit bridge (" not in text
    assert any("comparative figures do not reconcile" in gap for gap in bundle.uncertainty)


def test_lease_and_group_flows_are_not_double_counted_or_called_debt_cover():
    new = filing(
        "2026-03-28",
        {
            "current_liabilities_leases": -172,
            "non_current_liabilities_leases": -997,
            "lease_principal_paid": -153,
            "lease_interest_paid": -56,
            "operating_cash_flow": 628,
            "intragroup_investing_cash_flow": 74,
            "intragroup_financing_cash_flow": -360,
            "dividends_paid": -75,
        },
    )
    text, _ = analyse(new)
    assert "lease liabilities total 1,169.00" in text
    assert "combined 209.00" in text
    assert "combined -286.00" in text
    assert "Dividends paid 75.00, shown separately" in text
    assert "not a debt-service coverage ratio" in text
    assert "not undiscounted future payments" in text


def test_missing_transfers_are_not_treated_as_zero():
    text, _ = analyse(filing("2026-03-28", {"dividends_paid": 75}))
    assert "combined" not in text
    assert "does not identify the recipient" in text
