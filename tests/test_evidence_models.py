import copy
import json

import pytest
from pydantic import ValidationError

from agent_ui.evidence_models import EvidenceGap, FinancialFact, resolve_metric_gaps
from agent_ui.validation import EvidenceItem


def metric(**changes):
    return dict(
        metric="lcr",
        value="194",
        scale=1,
        unit="%",
        period="2025-03-31",
        scope="group",
        page=6,
        reporting_basis="unspecified",
        averaging_window=None,
        excerpt="LCR 194%",
        **changes,
    )


def evidence(claim, payload):
    return dict(
        evidence_id="ev_001",
        claim=claim,
        source={"url": "https://example.com/accounts", "kind": "other"},
        verbatim_excerpt="",
        structured_fact={claim: payload},
        confidence=0.8,
    )


def fact():
    return dict(
        value="193329",
        scale=1000,
        currency="GBP",
        period="2025-03-31",
        scope="group",
        page=82,
        excerpt="Total capital 193,329",
        source_box=[[1, 2]],
        source_text="193,329",
    )


def test_typed_fact_preserves_wire_format_and_source_provenance():
    payload = evidence("total_equity", fact())
    item = EvidenceItem.model_validate(payload)
    assert isinstance(item.typed_fact, FinancialFact)
    assert json.loads(item.model_dump_json()) == payload


@pytest.mark.parametrize(
    "field,value",
    [
        ("value", "NaN"),
        ("value", "Infinity"),
        ("scale", 0),
        ("scale", True),
        ("page", 0),
        ("period", "2025-02-30"),
        ("currency", "USD"),
    ],
)
def test_invalid_financial_fact_rejected(field, value):
    payload = fact()
    payload[field] = value
    with pytest.raises(ValidationError):
        EvidenceItem.model_validate(evidence("total_equity", payload))


def test_missing_fields_cannot_bypass_known_metric_validation():
    with pytest.raises(ValidationError):
        EvidenceItem.model_validate(evidence("revenue", {"value": "4"}))
    payload = evidence("banking_regulatory_metric", metric())
    payload["structured_fact"] = {"something_else": {}}
    with pytest.raises(ValidationError):
        EvidenceItem.model_validate(payload)


def test_generic_claim_cannot_bypass_validation():
    payload = evidence("Company exists", {})
    payload["structured_fact"] = {}
    with pytest.raises(ValidationError):
        EvidenceItem.model_validate(payload)


@pytest.mark.parametrize(
    "change", [{"unit": "GBP"}, {"scale": 100}, {"averaging_window": "12-month"}]
)
def test_regulatory_units_and_basis_are_consistent(change):
    payload = metric()
    payload.update(change)
    with pytest.raises(ValidationError):
        EvidenceItem.model_validate(evidence("banking_regulatory_metric", payload))


def test_review_rating_and_identity_fields_are_validated():
    rating = dict(
        provider="Trustpilot",
        rating=4,
        scale_max=5,
        review_count=10,
        scope="brand",
        entity_match="name only",
    )
    EvidenceItem.model_validate(evidence("platform_customer_rating", rating))
    rating["rating"] = 6
    with pytest.raises(ValidationError):
        EvidenceItem.model_validate(evidence("platform_customer_rating", rating))


@pytest.mark.parametrize(
    "status", ["aggregate_unavailable", "invented_status", "confirmed_on_profile"]
)
def test_rating_failure_status_cannot_bypass_numeric_validation(status):
    payload = dict(
        provider="REVIEWS.io",
        status=status,
        url="https://www.reviews.io/company-reviews/store/example",
        checked_at="2026-11-13",
        reason="Unavailable",
        rating=99,
        scale_max=5,
        review_count=-10,
    )
    with pytest.raises(ValidationError):
        EvidenceItem.model_validate(evidence("platform_customer_rating", payload))


def test_recovery_uses_fields_not_gap_wording_and_retains_audit():
    gap = EvidenceGap(
        code="missing_metric",
        metric="lcr",
        period="2025-03-31",
        scope="group",
        message="A completely rewritten description",
    )
    notes = {
        "gaps": [gap.message, "Other limitation"],
        "gap_records": [gap.model_dump(mode="json")],
    }
    resolve_metric_gaps(notes, [metric()])
    assert notes["gaps"] == ["Other limitation"]
    assert notes["gap_records"][0]["status"] == "resolved"


@pytest.mark.parametrize(
    "change", [{"period": "2024-03-31"}, {"scope": "company"}, {"metric": "nsfr"}]
)
def test_wrong_period_scope_or_metric_does_not_resolve_gap(change):
    gap = EvidenceGap(
        code="missing_metric",
        metric="lcr",
        period="2025-03-31",
        scope="group",
        message="Unavailable",
    )
    notes = {"gaps": [gap.message], "gap_records": [gap.model_dump(mode="json")]}
    before = copy.deepcopy(notes)
    value = metric()
    value.update(change)
    resolve_metric_gaps(notes, [value])
    assert notes == before


def test_conflicts_and_legacy_untyped_gaps_are_never_silently_cleared():
    gap = EvidenceGap(
        code="conflicting_metric", metric="lcr", period="2025-03-31", message="Conflict"
    )
    notes = {
        "gaps": ["Conflict", "Liquidity coverage ratio (LCR): no reliable legacy value"],
        "gap_records": [gap.model_dump(mode="json")],
    }
    resolve_metric_gaps(notes, [metric()])
    assert len(notes["gaps"]) == 2
    assert notes["gap_records"][0]["status"] == "open"


def test_conflicting_recovered_measurements_do_not_clear_missing_gap():
    gap = EvidenceGap(code="missing_metric", metric="lcr", period="2025-03-31", message="Missing")
    notes = {"gaps": [gap.message], "gap_records": [gap.model_dump(mode="json")]}
    other = metric()
    other["value"] = "200"
    resolve_metric_gaps(notes, [metric(), other])
    assert notes["gap_records"][0]["status"] == "open"


def test_disclosure_requires_source_context():
    with pytest.raises(ValidationError):
        EvidenceItem.model_validate(
            evidence("banking_note_disclosure", {"excerpt": "A claim without scope or period"})
        )


def test_gap_rejects_missing_machine_readable_context():
    with pytest.raises(ValidationError):
        EvidenceGap(code="missing_metric", message="Unknown")
    with pytest.raises(ValidationError):
        EvidenceGap(code="uncertain_transcription", message="Unreadable")


@pytest.mark.parametrize("claim", ["review_summary", "platform_customer_rating"])
def test_rating_cannot_bypass_validation_under_an_alias(claim):
    with pytest.raises(ValidationError):
        EvidenceItem.model_validate(evidence(claim, {"rating": 99, "review_count": -10}))


def test_extra_payload_cannot_hide_under_valid_claim():
    payload = evidence("company_number", "09964966")
    payload["structured_fact"]["review_summary"] = {"rating": 99}
    with pytest.raises(ValidationError):
        EvidenceItem.model_validate(payload)
