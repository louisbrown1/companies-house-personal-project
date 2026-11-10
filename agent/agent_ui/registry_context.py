"""Deterministic, explicitly limited registry context for local model tests."""

import json

LOCAL_SCOPE = (
    "Local Gemma test coverage is limited to selected Companies House profile fields "
    "and API collection counts. Individual officer, PSC, charge, insolvency-case and "
    "filing records were not supplied to the general highlight model. Accounts document extraction and web search coverage are reported "
    "separately. This is not a complete underwriting assessment."
)


def compact_registry_context(pack):
    def pick(data, names):
        return {key: data[key] for key in names if key in data}

    profile = pick(
        pack["profile"],
        [
            "company_number",
            "company_name",
            "company_status",
            "type",
            "date_of_creation",
            "date_of_cessation",
            "sic_codes",
            "jurisdiction",
            "has_charges",
            "has_insolvency_history",
            "undeliverable_registered_office_address",
        ],
    )
    profile["registered_office_address"] = pick(
        pack["profile"].get("registered_office_address", {}),
        ["locality", "region", "country", "postal_code"],
    )
    accounts = pack["profile"].get("accounts", {})
    profile["accounts"] = pick(accounts, ["overdue", "next_due", "next_made_up_to"])
    profile["accounts"]["last_accounts"] = pick(
        accounts.get("last_accounts", {}), ["made_up_to", "type"]
    )
    profile["confirmation_statement"] = pick(
        pack["profile"].get("confirmation_statement", {}),
        ["last_made_up_to", "next_due", "overdue"],
    )
    result = {
        "company_number": pack["company_number"],
        "profile": profile,
        "sources": pack.get("sources", {}),
        "scope": {"limitations": LOCAL_SCOPE},
    }
    for key in ("officers", "psc", "charges", "filing_history_recent"):
        data = pack.get(key, {})
        result[key] = pick(
            data,
            [
                "total_count",
                "active_count",
                "resigned_count",
                "satisfied_count",
                "part_satisfied_count",
                "unfiltered_count",
                "total_results",
            ],
        )
    # Fail explicitly instead of silently chopping identities or source URLs.
    if len(json.dumps(result)) > 8000:
        raise ValueError("Selected registry context exceeds the local test input budget")
    return result
