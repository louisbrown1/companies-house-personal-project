"""Concise summary of existing evidence, without generating new factual claims."""

import json


def render_executive_summary(bundle, groups):
    by_id = {item.evidence_id: item for item in bundle.evidence}

    def safe(value):
        # External strings must not create Markdown lines or evidence identifiers.
        return json.dumps(value, ensure_ascii=True).replace("ev_", "ev\\u005f")

    def cite_observation(eid):
        fact = by_id[eid].structured_fact["financial_observation"]
        return (
            fact["observation"]
            + " "
            + " ".join(f"[{source}]" for source in dict.fromkeys([eid, *fact["inputs"]]))
        )

    observations = [eid for eid in groups.get("financial_observations", []) if eid in by_id]
    selected = [eid for eid in groups.get("financial_selected", []) if eid in observations]
    filings = [
        item.structured_fact["accounts_filing"]["period_ended"]
        for item in bundle.evidence
        if item.claim == "accounts_filing"
    ]
    latest = max(filings, default="")

    def choose(terms, prefer_model=False):
        pool = list(dict.fromkeys((selected if prefer_model else []) + observations))
        for eid in pool:
            fact = by_id[eid].structured_fact["financial_observation"]
            text = fact["observation"]
            # Never promote an older single-period observation over the latest filing.
            if fact.get("period") and fact["period"] != latest:
                continue
            if any(
                text.removeprefix("Company: ").removeprefix("Group: ").startswith(period)
                for period in filings
                if period != latest
            ):
                continue
            if any(term in text.lower() for term in terms):
                return cite_observation(eid)
        return None

    identity = []
    for item in bundle.evidence:
        if item.claim in ("company_name", "company_number", "company_status", "sic_codes"):
            identity.append(
                f"{item.claim.replace('_', ' ')}: {safe(item.structured_fact[item.claim])} [{item.evidence_id}]"
            )

    performance = [
        choose(["revenue increased", "revenue decreased"]),
        choose(
            [
                "operating profit decreased",
                "operating profit increased",
                "profit after tax decreased",
                "profit after tax increased",
            ],
            True,
        ),
        choose(["management-adjusted operating profit", "operating profit bridge"]),
    ]
    liquidity = [
        choose(["current ratio", "operating cash flow was"], True),
        choose(["money-market deposit", "cash increased", "cash decreased"]),
    ]
    commitments = [
        choose(["lease maturity schedule", "recognised lease liabilities"]),
        choose(["net intragroup investing cash flow", "group non-current funding receivable"]),
        choose(["guarantees of group"]),
    ]
    if groups.get("banking"):

        def bank(metric, scope):
            for eid in observations:
                fact = by_id[eid].structured_fact["financial_observation"]
                if (
                    fact.get("metric") == metric
                    and fact.get("scope") == scope
                    and fact.get("period") == latest
                ):
                    return cite_observation(eid)
            return None

        performance = [
            bank("profit_before_tax", "company"),
            bank("profit_after_tax", "group") or bank("profit_after_tax", "company"),
            bank("net_interest_income", "group") or bank("net_interest_income", "company"),
        ]
        liquidity = [bank("cash", "company"), bank("loan_deposit_ratio", "company")]
        commitments = [
            bank("customer_deposits", "company"),
            bank("credit_institution_funding", "company"),
        ]
        capital = bank("cet1", "group") or bank("cet1", "company")
        if capital:
            commitments.insert(0, capital)
        regulatory_liquidity = [
            bank(metric, "group")
            or bank(metric, "company")
            or bank(metric, "bank (regulatory perimeter unspecified)")
            for metric in ("lcr", "nsfr")
        ]
        liquidity.extend(value for value in regulatory_liquidity if value)

    def findings(values, missing):
        values = list(dict.fromkeys(value for value in values if value))
        return " ".join(values) if values else missing

    reputation, unavailable = [], []
    for provider in groups.get("reviews", []):
        found = False
        for eid in provider.get("rating_ids", []):
            rating = by_id[eid].structured_fact["platform_customer_rating"]
            if rating["status"] not in ("confirmed_on_profile", "confirmed_via_tavily_extract"):
                continue
            scope = (
                "individual location"
                if rating["scope"] == "individual_location"
                else "brand/business profile"
            )
            cached = (
                "; Tavily extraction, may be cached"
                if rating["status"] == "confirmed_via_tavily_extract"
                else ""
            )
            reputation.append(
                f"{provider['provider']}: {rating['rating']:g}/{rating['scale_max']:g} from "
                f"{rating['review_count']:,} reviews ({scope}{cached}) [{eid}]."
            )
            found = True
        if not found:
            unavailable.append(provider["provider"])
    if unavailable:
        reputation.append("Ratings unavailable for " + ", ".join(unavailable) + ".")
    reputation.append(
        "Scores are separate; legal-entity links and platform update times are unverified. Reviews do not establish creditworthiness."
    )
    gaps = []
    if not observations:
        gaps.append("Financial health cannot be summarised from the available accounts evidence.")
    if not groups.get("financial_notes"):
        gaps.append(
            "Accounts-note coverage is missing; profit explanations, leases and group exposures require further evidence."
        )
    if groups.get("banking"):
        absent = [
            label
            for metric, label in [("cet1", "CET1"), ("lcr", "LCR"), ("nsfr", "NSFR")]
            if not (
                bank(metric, "group")
                or bank(metric, "company")
                or bank(metric, "bank (regulatory perimeter unspecified)")
            )
        ]
        if absent:
            gaps.append(
                "No reliable current-period value extracted for "
                + ", ".join(absent)
                + ". Review relevant Pillar 3 disclosures."
            )
        gaps.append(
            "Regulatory capital adequacy and liquidity compliance are not independently assessed. Group results must not be treated as standalone company results; note coverage remains selective."
        )
    if any(value is None for value in liquidity):
        gaps.append("Liquidity evidence is incomplete.")
    gaps.append(
        "Review original filings to confirm OCR figures, reporting-period comparability, lease terms and group exposures before assessing lending suitability."
    )

    return [
        "## Executive summary",
        "Financial highlights use Gemma-selected observations where available, with required topic coverage assembled from cited evidence. Accounts figures are OCR-derived; this is not a credit rating or lending recommendation.",
        "",
        "- **Business assessed:** "
        + ("; ".join(identity) if identity else "Registry identity unavailable.")
        + ". Trading activities beyond registered SIC codes are unverified.",
        "- **Financial performance:** "
        + findings(performance, "Comparable revenue and profit findings unavailable."),
        "- **Liquidity:** " + findings(liquidity, "Liquidity findings unavailable."),
        (
            "- **Capital and bank funding:** "
            if groups.get("banking")
            else "- **Leases and group exposure:** "
        )
        + findings(
            commitments,
            "Capital and bank-funding findings unavailable."
            if groups.get("banking")
            else "Lease and group-exposure findings unavailable; absence of findings does not mean no obligations.",
        ),
        "- **Customer reputation:** " + " ".join(reputation),
        "- **Follow-up:** " + " ".join(gaps),
    ]
