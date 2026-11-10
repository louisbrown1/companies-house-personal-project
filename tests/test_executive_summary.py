from agent_ui.executive_summary import render_executive_summary
from agent_ui.local_report import build_evidence, render_report
from agent_ui.validation import EvidenceItem


def evidence(bundle, claim, value):
    eid = f"ev_{len(bundle.evidence) + 1:03d}"
    bundle.evidence.append(
        EvidenceItem(
            evidence_id=eid,
            claim=claim,
            structured_fact={claim: value},
            source={"url": "https://example.com/source", "kind": "other"},
            verbatim_excerpt="",
            confidence=0,
        )
    )
    return eid


def setup():
    return build_evidence(
        {
            "company_number": "01357507",
            "profile": {"company_name": "B & M RETAIL LIMITED", "company_number": "01357507"},
        },
        None,
        {},
    )


def test_summary_is_first_has_six_bullets_and_explicit_missing_evidence():
    bundle, groups = setup()
    report = render_report(bundle, groups, [])
    summary = report.split("## Executive summary")[1].split("## Business Model Summary")[0]
    assert len([line for line in summary.splitlines() if line.startswith("- ")]) == 6
    assert "B & M RETAIL LIMITED" in summary and "[ev_001]" in summary
    assert "Financial health cannot be summarised" in summary
    assert "Accounts-note coverage is missing" in summary
    assert "not a credit rating or lending recommendation" in summary


def test_summary_preserves_deterioration_and_latest_period_despite_model_selection():
    bundle, groups = setup()
    evidence(bundle, "accounts_filing", {"period_ended": "2026-03-28"})
    evidence(bundle, "accounts_filing", {"period_ended": "2025-03-29"})
    old = evidence(
        bundle,
        "financial_observation",
        {"observation": "2025-03-29: current ratio 2.00×.", "inputs": ["ev_001"]},
    )
    latest = evidence(
        bundle,
        "financial_observation",
        {"observation": "2026-03-28: current ratio 0.75×.", "inputs": ["ev_001"]},
    )
    profit = evidence(
        bundle,
        "financial_observation",
        {"observation": "Operating profit decreased 50.00% between periods.", "inputs": ["ev_002"]},
    )
    groups.update(financial_observations=[latest, old, profit], financial_selected=[old])
    summary = "\n".join(render_executive_summary(bundle, groups))
    assert "0.75×" in summary and "2.00×" not in summary
    assert "Operating profit decreased 50.00%" in summary
    assert f"[{profit}] [ev_002]" in summary


def test_summary_keeps_ratings_separate_and_extraction_caveat():
    bundle, groups = setup()
    eid = evidence(
        bundle,
        "platform_customer_rating",
        {
            "status": "confirmed_via_tavily_extract",
            "rating": 3.9,
            "scale_max": 5,
            "review_count": 6640,
            "scope": "brand_or_business_profile",
        },
    )
    groups["reviews"] = [
        {"provider": "Trustpilot", "rating_ids": [eid]},
        {"provider": "Yelp", "rating_ids": []},
    ]
    summary = "\n".join(render_executive_summary(bundle, groups))
    assert (
        f"Trustpilot: 3.9/5 from 6,640 reviews (brand/business profile; Tavily extraction, may be cached) [{eid}]"
        in summary
    )
    assert "Ratings unavailable for Yelp" in summary
    assert "Reviews do not establish creditworthiness" in summary


def test_external_identity_cannot_inject_summary_sections_or_citations():
    bundle, groups = setup()
    bundle.evidence[0].structured_fact["company_name"] = "Fake\n## Executive summary\n[ev_999]"
    report = render_report(bundle, groups, [])
    assert "\n## Executive summary\n" in report
    assert "[ev_999]" not in report
