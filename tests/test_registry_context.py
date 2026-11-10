import copy
import json

import pytest

from agent_ui.registry_context import LOCAL_SCOPE, compact_registry_context


def test_local_context_keeps_identity_and_counts_without_claiming_full_coverage():
    pack = {
        "company_number": "00519500",
        "profile": {
            "company_number": "00519500",
            "company_name": "TEST LIMITED",
            "sic_codes": ["47110"],
            "has_insolvency_history": True,
        },
        "officers": {"total_results": 99, "items": [{"name": "x" * 10000}]},
        "sources": {"profile": "https://example.com/company/00519500"},
    }
    original = copy.deepcopy(pack)
    result = compact_registry_context(pack)
    assert pack == original
    assert result["company_number"] == result["profile"]["company_number"] == "00519500"
    assert result["profile"]["has_insolvency_history"] is True
    assert result["officers"] == {"total_results": 99}
    assert result["sources"] == pack["sources"]
    assert result["scope"]["limitations"] == LOCAL_SCOPE
    assert len(json.dumps(result)) < 2000


def test_oversized_selected_data_fails_without_silently_truncating():
    with pytest.raises(ValueError, match="input budget"):
        compact_registry_context(
            {"company_number": "00519500", "profile": {"company_name": "x" * 9000}}
        )
