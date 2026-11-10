"""Cited figures and deterministic calculations for Gemma's financial selection."""

from decimal import Decimal

from .validation import EvidenceItem


def add_financial_evidence(bundle, groups, accounts):
    _add_financial_evidence(bundle, groups, accounts)
    grouped = [
        {
            **f,
            "figures": f.get("group_figures", {}),
            "comparatives": f.get("group_comparatives", {}),
            "notes": None,
        }
        for f in accounts["filings"]
    ]
    groups["banking"] = any(f.get("banking") for f in accounts["filings"])
    groups["has_group_accounts"] = any(f["figures"] for f in grouped)
    if groups["banking"] or groups["has_group_accounts"]:
        for eid in groups["financial_observations"]:
            item = next(e for e in bundle.evidence if e.evidence_id == eid)
            item.structured_fact["financial_observation"]["observation"] = (
                "Company: " + item.structured_fact["financial_observation"]["observation"]
            )
    if groups["has_group_accounts"]:
        group_groups = {}
        start = len(bundle.evidence)
        _add_financial_evidence(bundle, group_groups, {"filings": grouped, "gaps": []})
        for item in bundle.evidence[start:]:
            if item.claim == "financial_observation":
                item.structured_fact[item.claim]["observation"] = (
                    "Group: " + item.structured_fact[item.claim]["observation"]
                )
        for key in ("financial", "financial_observations", "financial_notes"):
            groups[key].extend(group_groups[key])
    from .banking_notes import add_banking_note_evidence

    add_banking_note_evidence(
        bundle, groups, accounts["filings"] + accounts.get("supplemental_filings", [])
    )


def _add_financial_evidence(bundle, groups, accounts):
    groups["financial"] = []
    groups["financial_observations"] = []
    groups["financial_selected"] = []
    groups["financial_notes"] = []
    bundle.uncertainty.extend(accounts["gaps"])

    def add(label, fact, url, excerpt="", observation=False):
        eid = f"ev_{len(bundle.evidence) + 1:03d}"
        bundle.evidence.append(
            EvidenceItem.model_validate(
                {
                    "evidence_id": eid,
                    "claim": label,
                    "source": {"url": url, "kind": "companies_house_api"},
                    "verbatim_excerpt": excerpt,
                    "structured_fact": {label: fact},
                    "confidence": 0.8,
                }
            )
        )
        groups["financial_observations" if observation else "financial"].append(eid)
        return eid

    periods = []
    for filing in accounts["filings"]:
        period, figures, ids = filing["period"], filing["figures"], {}
        add(
            "accounts_filing",
            {
                "period_ended": period,
                "filed": filing["filed"],
                "scope": next(iter(figures.values()), {}).get("scope", "company"),
                "type": filing["description"],
                "sha256": filing.get("sha256"),
                "extracted_figures": len(figures),
                "extraction_diagnostics": filing.get("extraction_diagnostics", {}),
                "page_selection": {
                    k: v
                    for k, v in filing.get("page_selection", {}).items()
                    if k
                    in (
                        "status",
                        "selected_pages",
                        "unresolved_fields",
                        "missing_validated_fields",
                        "index_sha256",
                        "index_truncated",
                        "coverage_basis",
                    )
                },
            },
            filing["url"],
        )
        for name, fact in figures.items():
            ids[name] = add(name, fact, filing["url"] + f"#page={fact['page']}", fact["excerpt"])

        def ratio(a, b, multiplier, title, suffix, absolute=False):
            if a not in figures or b not in figures:
                return
            left, right = figures[a], figures[b]
            if not compatible(left, right):
                return
            den = amount(right)
            if absolute:
                den = abs(den)
            if den <= 0:
                return
            result = amount(left) / den * multiplier
            message = f"{period}: {title} {result:.2f}{suffix}."
            add(
                "financial_observation",
                {
                    "observation": message,
                    "formula": f"{a} / {'abs(' + b + ')' if absolute else b} × {multiplier}",
                    "inputs": [ids[a], ids[b]],
                    "period": period,
                },
                filing["url"] + f"#page={left['page']}",
                observation=True,
            )

        ratio("operating_profit", "revenue", 100, "operating margin", "%")
        ratio("current_assets", "current_liabilities", 1, "current ratio", "×", absolute=True)
        if "operating_cash_flow" in figures:
            value = amount(figures["operating_cash_flow"])
            add(
                "financial_observation",
                {
                    "observation": f"{period}: operating cash flow was {'positive' if value > 0 else 'negative' if value < 0 else 'zero'}.",
                    "inputs": [ids["operating_cash_flow"]],
                },
                filing["url"] + f"#page={figures['operating_cash_flow']['page']}",
                observation=True,
            )
        if "total_assets" in figures and "total_liabilities" in figures:
            a, b = figures["total_assets"], figures["total_liabilities"]
            if compatible(a, b):
                net = amount(a) - abs(amount(b))
                add(
                    "financial_observation",
                    {
                        "observation": f"{period}: derived net assets {net / a['scale']:,.2f} ({a['currency']}, scale {a['scale']:,}).",
                        "formula": "total_assets - abs(total_liabilities)",
                        "inputs": [ids["total_assets"], ids["total_liabilities"]],
                    },
                    filing["url"] + f"#page={a['page']}",
                    observation=True,
                )
        periods.append((filing, ids))
        if filing.get("banking"):
            add_banking_observations(filing, ids, add)
        add_commitments(filing, ids, add)
        if filing.get("notes"):
            add_notes(filing, ids, add, groups)
    if len(periods) == 2:
        (new, ni), (old, oi) = periods
        if not new.get("banking"):
            add_profit_bridge(new, old, ni, oi, add, bundle)
        for metric in ("revenue", "operating_profit", "profit_after_tax", "cash"):
            a, b = new["figures"].get(metric), old["figures"].get(metric)
            comparative = new.get("comparatives", {}).get(metric)
            if not a or not b or not compatible(a, b) or amount(b) <= 0:
                continue
            # Do not silently compare against an unreconciled or restated prior year.
            if (
                not comparative
                or not compatible(b, comparative)
                or amount(comparative) != amount(b)
            ):
                bundle.uncertainty.append(
                    f"{metric}: prior filing could not be reconciled with the latest comparative column; growth calculation withheld."
                )
                continue
            change = (amount(a) / amount(b) - 1) * 100
            add(
                "financial_observation",
                {
                    "observation": f"{metric.replace('_', ' ').capitalize()} {'increased' if change >= 0 else 'decreased'} {abs(change):.2f}% between periods ended {old['period']} and {new['period']}.",
                    "formula": "(latest / previous - 1) × 100",
                    "inputs": [ni[metric], oi[metric]],
                    "comparison": "As-filed reporting periods; not annualised. Latest comparative agrees with prior filing.",
                },
                new["url"] + f"#page={a['page']}",
                observation=True,
            )


def add_banking_observations(filing, ids, add):
    figures = filing["figures"]
    for metric in (
        "net_interest_income",
        "total_income",
        "profit_before_tax",
        "profit_after_tax",
        "loan_impairment_expense",
        "customer_loans",
        "customer_deposits",
        "cash",
        "total_equity",
        "credit_institution_funding",
    ):
        if metric not in figures:
            continue
        fact = figures[metric]
        text = f"{filing['period']}: {metric.replace('_', ' ')} {fact['currency']} {amount(fact) / 1000000:,.3f}m."
        inputs = [ids[metric]]
        prior = filing.get("comparatives", {}).get(metric)
        if prior and compatible(fact, prior):
            # The same filing's comparative remains explicitly attributed, not assumed unrevised.
            prior_id = add(
                "comparative_" + metric,
                prior,
                filing["url"] + f"#page={prior['page']}",
                prior["excerpt"],
            )
            inputs.append(prior_id)
            text += f" Same-filing comparative ({prior['period']}): {prior['currency']} {amount(prior) / 1000000:,.3f}m; movement {amount(fact) / 1000000 - amount(prior) / 1000000:+,.3f}m."
        if metric == "profit_after_tax" and amount(fact) < 0:
            text += " The group/company remains loss-making on this measure."
        add(
            "financial_observation",
            {
                "observation": text,
                "inputs": inputs,
                "period": filing["period"],
                "category": "banking",
                "metric": metric,
                "scope": fact["scope"],
            },
            filing["url"] + f"#page={fact['page']}",
            observation=True,
        )
    if "customer_loans" in figures and "customer_deposits" in figures:
        a, b = figures["customer_loans"], figures["customer_deposits"]
        if compatible(a, b) and amount(b) > 0:
            add(
                "financial_observation",
                {
                    "observation": f"{filing['period']}: carrying-value customer loans / customer deposits {amount(a) / amount(b) * 100:.2f}%. This is not a regulatory liquidity or capital ratio.",
                    "inputs": [ids["customer_loans"], ids["customer_deposits"]],
                    "period": filing["period"],
                    "category": "banking",
                    "metric": "loan_deposit_ratio",
                    "scope": a["scope"],
                },
                filing["url"] + f"#page={a['page']}",
                observation=True,
            )


def amount(fact):
    return Decimal(fact["value"]) * fact["scale"]


def compatible(a, b):
    return (
        a.get("currency") is not None
        and a["currency"] == b.get("currency")
        and a["scope"] == b["scope"]
    )


def add_profit_bridge(new, old, ni, oi, add, bundle):
    metrics = (
        "revenue",
        "cost_of_sales",
        "gross_profit",
        "administrative_expenses",
        "operating_profit",
    )
    if any(m not in f["figures"] for m in metrics for f in (new, old)):
        bundle.uncertainty.append(
            "Operating profit bridge unavailable: required cost lines were not all extracted."
        )
        return
    if any(not compatible(new["figures"][m], old["figures"][m]) for m in metrics):
        return
    for m in metrics:
        comparative = new.get("comparatives", {}).get(m)
        if (
            not comparative
            or not compatible(comparative, old["figures"][m])
            or amount(comparative) != amount(old["figures"][m])
        ):
            bundle.uncertainty.append(
                "Operating profit bridge withheld: comparative figures do not reconcile with prior filing."
            )
            return
    vals = [{m: amount(f["figures"][m]) for m in metrics} for f in (new, old)]
    # Use signed statement expenses; never assume an unrecognised positive cost is negative.
    for v in vals:
        if (
            v["cost_of_sales"] > 0
            or v["administrative_expenses"] > 0
            or v["revenue"] + v["cost_of_sales"] != v["gross_profit"]
            or v["gross_profit"] + v["administrative_expenses"] != v["operating_profit"]
        ):
            bundle.uncertainty.append(
                "Operating profit bridge withheld: statement arithmetic does not reconcile."
            )
            return
    a, b = vals
    scale = new["figures"]["revenue"]["scale"]
    unit = f"{new['figures']['revenue']['currency']} {scale:,} units"
    delta = {m: (a[m] - b[m]) / scale for m in metrics}
    msg = (
        f"Operating profit bridge ({old['period']} to {new['period']}, {unit}): "
        f"revenue movement {delta['revenue']:+,.2f}, cost-of-sales impact {delta['cost_of_sales']:+,.2f}, "
        f"giving gross-profit movement {delta['gross_profit']:+,.2f}; administrative-expense impact "
        f"{delta['administrative_expenses']:+,.2f}; total operating-profit movement {delta['operating_profit']:+,.2f}. "
        "This reconciles the accounting movements; it does not establish their underlying business causes."
    )
    add(
        "financial_observation",
        {
            "observation": msg,
            "inputs": [ids[m] for ids in (ni, oi) for m in metrics],
            "category": "profit_explanation",
        },
        new["url"] + f"#page={new['figures']['operating_profit']['page']}",
        observation=True,
    )


def add_commitments(filing, ids, add):
    figures = filing["figures"]

    def observation(metrics, message):
        if not all(m in figures for m in metrics):
            return
        first = figures[metrics[0]]
        if not all(compatible(first, figures[m]) for m in metrics):
            return
        values = [amount(figures[m]) / first["scale"] for m in metrics]
        text = f"{filing['period']} ({first['currency']}, scale {first['scale']:,}): " + message(
            values
        )
        add(
            "financial_observation",
            {"observation": text, "inputs": [ids[m] for m in metrics]},
            filing["url"] + f"#page={first['page']}",
            observation=True,
        )

    observation(
        ["current_liabilities_leases", "non_current_liabilities_leases"],
        lambda v: (
            f"Recognised lease liabilities total {abs(v[0]) + abs(v[1]):,.2f}: current {abs(v[0]):,.2f}, non-current {abs(v[1]):,.2f}. These are balance-sheet amounts, not undiscounted future payments."
        ),
    )
    observation(
        ["lease_principal_paid", "lease_interest_paid", "operating_cash_flow"],
        lambda v: (
            f"Lease principal cash paid {abs(v[0]):,.2f} plus lease interest paid {abs(v[1]):,.2f}; combined {abs(v[0]) + abs(v[1]):,.2f}, compared with operating cash flow {v[2]:,.2f}. This is not a debt-service coverage ratio or free cash flow."
        ),
    )
    observation(
        ["intragroup_investing_cash_flow", "intragroup_financing_cash_flow"],
        lambda v: (
            f"Net intragroup investing cash flow {v[0]:+,.2f} and net intragroup financing cash flow {v[1]:+,.2f}; combined {sum(v):+,.2f} (positive=inflow, negative=outflow). Gross transfers and counterparties cannot be inferred from these net flows."
        ),
    )
    observation(
        ["dividends_paid"],
        lambda v: (
            f"Dividends paid {abs(v[0]):,.2f}, shown separately from intragroup cash flows. The cash-flow label alone does not identify the recipient."
        ),
    )


def add_notes(filing, statement_ids, add, groups):
    notes = filing["notes"]
    ids = {}

    def detail(label, fact):
        eid = add(label, fact, filing["url"] + f"#page={fact['page']}", fact["excerpt"])
        groups["financial"].remove(eid)
        groups["financial_notes"].append(eid)
        return eid

    def observation(text, inputs, page):
        add(
            "financial_observation",
            {"observation": text, "inputs": inputs, "category": "accounts_notes"},
            filing["url"] + f"#page={page}",
            observation=True,
        )

    pairs = notes["pairs"]
    for metric, fact in pairs.items():
        ids[metric] = detail("note_" + metric, fact)
    for counterparty in notes.get("counterparties", []):
        detail("group_funding_counterparty", counterparty)

    def millions(fact, key):
        return Decimal(fact[key]) * fact["scale"] / 1000000

    costs = ("wages_and_salaries", "social_security_costs", "share_based_payments", "pension_costs")
    if (
        all(k in pairs for k in costs)
        and len(
            {
                (
                    pairs[k]["currency"],
                    pairs[k]["period"],
                    pairs[k]["prior_period"],
                    pairs[k]["scope"],
                )
                for k in costs
            }
        )
        == 1
    ):
        current = sum(millions(pairs[k], "current") for k in costs)
        prior = sum(millions(pairs[k], "previous") for k in costs)
        observation(
            f"Employee benefit components total GBP {current:,f}m versus {prior:,f}m, a change of {current - prior:+,.0f}m. "
            "These are components of reported costs, not additional costs to add to the operating-profit bridge.",
            [ids[k] for k in costs],
            pairs[costs[0]]["page"],
        )
    adjusted = pairs.get("adjusted_operating_profit")
    statutory = filing["figures"].get("operating_profit")
    comparative = filing.get("comparatives", {}).get("operating_profit")
    if (
        adjusted
        and statutory
        and comparative
        and statutory["currency"] == "GBP"
        and comparative["currency"] == "GBP"
    ):
        current, previous = millions(adjusted, "current"), millions(adjusted, "previous")
        a = amount(statutory) / 1000000
        b = amount(comparative) / 1000000
        interpretation = (
            "The decline is not explained solely by adjusting items."
            if current < previous and a < b
            else "Both statutory and adjusted measures should be considered."
        )
        observation(
            f"Management-adjusted operating profit is GBP {current:,f}m versus {previous:,f}m. "
            f"The gap to statutory operating profit is {current - a:,f}m versus {previous - b:,f}m. "
            "Adjusted profit remains a management-defined measure. " + interpretation,
            [ids["adjusted_operating_profit"], statement_ids["operating_profit"]],
            adjusted["page"],
        )
    for schedule in notes["maturities"]:
        eid = detail("lease_maturity_schedule", schedule)
        observation(
            f"{schedule['period']}: lease maturity schedule totals GBP {millions(schedule, 'total'):,f}m: "
            f"{millions(schedule, 'within_one_year'):g}m within one year, {millions(schedule, 'one_to_two_years'):g}m in years 1–2, "
            f"{millions(schedule, 'two_to_five_years'):g}m in years 2–5 and {millions(schedule, 'after_five_years'):g}m after five years. "
            "These scheduled payments are separate from the balance-sheet carrying value; do not add them together.",
            [eid],
            schedule["page"],
        )
    for metric, term in [
        (
            "intergroup_loan_receivables",
            "non-current funding receivable; not cash available within twelve months",
        ),
        ("intergroup_loan_payables", "current funding payable"),
    ]:
        if metric in pairs:
            fact = pairs[metric]
            observation(
                f"Group {term}: GBP {millions(fact, 'current'):g}m versus {millions(fact, 'previous'):g}m. "
                "Funding balances are not the same as cash transfers; do not assume netting or unrestricted access to group funds.",
                [ids[metric]],
                fact["page"],
            )
    for excerpt in notes["excerpts"]:
        eid = detail("accounts_note_disclosure", excerpt)
        if excerpt["topic"] == "group_guarantees":
            observation(
                "The quoted disclosure discusses guarantees. Check whether a guarantee exists, its beneficiaries, limits and conditions; a mention alone does not establish an outstanding obligation.",
                [eid],
                excerpt["page"],
            )
        if excerpt["topic"] == "cash_comparability":
            observation(
                "The quoted disclosure discusses deposit maturity or cash classification. Establish the timing and amounts before attributing cash movements to changes in total liquid resources.",
                [eid],
                excerpt["page"],
            )
