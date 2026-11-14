"""Bounded registry discovery and source-validated financial benchmarking."""

import asyncio
import hashlib
import os
from datetime import date
from pathlib import Path

import yaml

from .accounts import collect_accounts, latest_periods
from .evidence_models import FinancialFact, RegulatoryRatio
from .peer_discovery import discover_web_peers
from .peer_models import (
    RATIOS,
    MetricSource,
    PeerComparison,
    PeerIdentity,
    PeerMeasurement,
    PeerPolicy,
    PeerReport,
    PeerSelection,
)

ROOT = Path(__file__).parent / "knowledge" / "peers"


def load_peer_runbook(sector):
    if sector not in {"bank", "retail", "trading"}:
        raise ValueError("Unknown peer sector")
    raw = (ROOT / f"{sector}.md").read_bytes()
    if len(raw) > 8192:
        raise ValueError("Oversized peer runbook")
    text = raw.decode("utf-8").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise ValueError("Missing peer runbook frontmatter")
    head, guidance = text[4:].split("\n---\n", 1)
    meta = yaml.safe_load(head)
    policy = PeerPolicy.model_validate(meta["policy"])
    if meta["type"] != "Playbook" or meta["status"] != "stable" or policy.sector != sector:
        raise ValueError("Invalid peer runbook")
    return policy, guidance, hashlib.sha256(raw).hexdigest()


def source(filing, field, fact, unit):
    return MetricSource(
        url=filing["url"],
        page=fact.page,
        excerpt=fact.excerpt,
        field=field,
        value=fact.value,
        scale=fact.scale,
        unit=unit,
        period=fact.period,
        scope=fact.scope,
    )


def selected_period(accounts, reference=None):
    periods = [f["period"] for f in accounts.get("filings", [])]
    if reference is not None:
        return min(
            periods,
            key=lambda p: (
                abs((date.fromisoformat(p) - reference).days),
                -date.fromisoformat(p).toordinal(),
            ),
            default=None,
        )
    return max(periods, default=None)


def measurements(number, accounts, policy, reference=None):
    """Choose one period before metrics; never fill individual gaps from other years."""
    filings = accounts.get("filings", [])
    latest = selected_period(accounts, reference)
    result, gaps = [], []
    for filing in filings + accounts.get("supplemental_filings", []):
        if filing.get("period") != latest:
            continue
        for scope, key in (("company", "figures"), ("group", "group_figures")):
            figures = filing.get(key, {})
            for metric in policy.metrics:
                if metric not in RATIOS:
                    continue
                left, right, multiplier = RATIOS[metric]
                try:
                    a, b = [FinancialFact.model_validate(figures[k]) for k in (left, right)]
                    if (
                        a.scope != scope
                        or b.scope != scope
                        or a.period != b.period
                        or str(a.period) != latest
                    ):
                        raise ValueError("Input scope or period mismatch")
                    if b.value <= 0:
                        raise ValueError("Nonpositive denominator")
                    value = a.value * a.scale / (b.value * b.scale) * multiplier
                    result.append(
                        PeerMeasurement(
                            company_number=number,
                            metric=metric,
                            value=value,
                            unit="times" if metric == "current_ratio" else "%",
                            period=a.period,
                            scope=scope,
                            reporting_basis="point_in_time",
                            sources=[
                                source(filing, left, a, "GBP"),
                                source(filing, right, b, "GBP"),
                            ],
                            formula=f"{left} / {right} * {multiplier}",
                        )
                    )
                except (KeyError, ValueError, TypeError, ArithmeticError):
                    gaps.append(
                        f"{number}: {scope} {metric} unavailable from compatible selected-period inputs."
                    )
        for raw in (filing.get("banking_notes") or {}).get("metrics", []):
            if raw.get("metric") not in policy.metrics:
                continue
            try:
                fact = RegulatoryRatio.model_validate(raw)
                if str(fact.period) != latest:
                    continue
                result.append(
                    PeerMeasurement(
                        company_number=number,
                        metric=fact.metric,
                        value=fact.value,
                        unit="%",
                        period=fact.period,
                        scope=fact.scope,
                        reporting_basis=fact.reporting_basis,
                        averaging_window=fact.averaging_window,
                        sources=[source(filing, fact.metric, fact, "%")],
                        formula="published ratio",
                    )
                )
            except (ValueError, TypeError):
                gaps.append(
                    f"{number}: regulatory comparison withheld for unknown scope, basis or averaging window."
                )
    # Conflicting duplicates must not win by ordering of documents.
    indexed = {}
    for item in result:
        key = (item.metric, item.scope, item.reporting_basis, item.averaging_window)
        indexed.setdefault(key, []).append(item)
    clean = []
    for key, items in indexed.items():
        if len({i.value for i in items}) != 1:
            gaps.append(f"{number}: conflicting {key[0]} {key[1]} values; comparison withheld.")
        else:
            clean.append(items[0])
    return clean, list(dict.fromkeys(gaps))


def sector_accounts_verified(accounts, sector, reference=None):
    latest = selected_period(accounts, reference)
    fields = (
        ("customer_loans", "customer_deposits") if sector == "bank" else ("revenue", "total_assets")
    )
    for filing in accounts.get("filings", []):
        if filing.get("period") != latest:
            continue
        try:
            facts = [FinancialFact.model_validate(filing["figures"][k]) for k in fields]
            if all(f.value > 0 and f.scope == "company" and str(f.period) == latest for f in facts):
                return True
        except (KeyError, ValueError, TypeError):
            pass
    return False


def compare_measurements(subject, peer, policy):
    comparisons, gaps = [], []
    for a in subject:
        matches = [
            b
            for b in peer
            if (a.metric, a.scope, a.reporting_basis, a.averaging_window)
            == (b.metric, b.scope, b.reporting_basis, b.averaging_window)
        ]
        if not matches:
            gaps.append(
                f"{a.metric} ({a.scope}): peer has no matching scope and measurement basis."
            )
        for b in matches:
            days = abs((a.period - b.period).days)
            if days > policy.max_period_gap_days:
                gaps.append(
                    f"{a.metric} ({a.scope}): reporting dates differ by {days} days; comparison withheld."
                )
                continue
            comparisons.append(
                PeerComparison(
                    subject=a,
                    peer=b,
                    difference=a.value - b.value,
                    difference_unit="percentage_points" if a.unit == "%" else "times",
                    period_gap_days=days,
                )
            )
    return comparisons, gaps


def benchmark_registry(registry_tool, number, subject_periods, max_gap, selection_audit=None):
    """Select one eligible source filing before expensive OCR; keep original metadata."""

    async def call(name, arguments):
        result = await registry_tool(name, arguments)
        if name != "filing_history" or arguments.get("company_number") != number:
            return result
        filings = latest_periods(result.get("items", []))
        chosen = []
        for period in sorted(subject_periods, reverse=True):
            eligible = [
                f
                for f in filings
                if abs((date.fromisoformat(f["description_values"]["made_up_date"]) - period).days)
                <= max_gap
            ]
            if eligible:
                chosen = [
                    min(
                        eligible,
                        key=lambda f: abs(
                            (
                                date.fromisoformat(f["description_values"]["made_up_date"]) - period
                            ).days
                        ),
                    )
                ]
                break
        if selection_audit is not None:
            selection_audit.update(
                available_periods=[f["description_values"]["made_up_date"] for f in filings],
                selected_periods=[f["description_values"]["made_up_date"] for f in chosen],
            )
        return {**result, "items": chosen}

    return call


async def collect_peer_comparison(pack, accounts, registry_tool, sector, *, deadline=None):
    policy, guidance, digest = load_peer_runbook(sector)
    report = PeerReport(sector=sector, runbook_sha256=digest)
    report.gaps.append(
        "Bounded peer sample; registry identity, SIC and sector accounts are checked, but web business overlap is assessed from quoted pages where available; direct competition and independent ownership remain unverified. These are financial benchmarks, not verified direct competitors or a representative market sample."
    )
    report.gaps.append(
        "Profitability and growth comparisons withheld: consistent period lengths and accounting definitions are not yet validated."
    )
    report.gaps.append(
        "Peer extraction focuses on validated statements. Targeted notes and supplementary Pillar 3 discovery are not run for peers; absent regulatory comparisons remain gaps."
    )
    subject, gaps = measurements(pack["company_number"], accounts, policy)
    report.gaps.extend(gaps)
    if not subject or not sector_accounts_verified(accounts, sector):
        report.gaps.append(
            "Subject lacks validated latest-period sector accounts; peer collection not attempted."
        )
        return report, guidance
    codes = pack["profile"].get("sic_codes", [])
    if not codes:
        report.gaps.append("No SIC classification for peer discovery.")
        return report, guidance
    now = asyncio.get_running_loop().time()
    deadline = min(now + policy.budget_seconds, deadline if deadline is not None else float("inf"))
    if deadline <= now:
        report.gaps.append(
            "Insufficient remaining report time for peer collection; primary evidence retained."
        )
        return report, guidance
    try:
        async with asyncio.timeout_at(deadline):
            selection = PeerSelection(
                company_numbers=list(
                    dict.fromkeys(
                        n.strip().upper()
                        for n in os.getenv("PEER_COMPANY_NUMBERS", "").split(",")
                        if n.strip()
                    )
                )
            )
            if selection.company_numbers:
                report.selection_mode = "explicit"
                discovery = {"items": [{"company_number": n} for n in selection.company_numbers]}
            else:
                report.discovery = await discover_web_peers(
                    pack,
                    registry_tool,
                    guidance,
                    deadline=deadline,
                    accounts=accounts,
                    sector_query=policy.discovery_query,
                )
                report.gaps.extend(report.discovery.gaps)
                discovery = {
                    "items": [
                        {
                            "company_number": c.company_number,
                            "company_name": c.profile.company_name if c.profile else c.name,
                        }
                        for c in report.discovery.candidates
                        if c.status == "shortlisted" and c.company_number
                    ]
                }
            candidates = {}
            for item in discovery.get("items", [])[: policy.max_candidates]:
                number = item.get("company_number")
                if number and number != pack["company_number"]:
                    candidates.setdefault(number, item)
            report.candidates_considered = len(candidates)
            # Names prioritize expensive verification only; they never establish eligibility.
            ordered = sorted(
                candidates.values(),
                key=lambda c: (
                    sector == "bank" and "BANK" not in c.get("company_name", "").upper(),
                    c.get("company_number", ""),
                ),
            )
            profiles = []
            target = max(m.period for m in subject)
            subject_periods = {date.fromisoformat(f["period"]) for f in accounts.get("filings", [])}
            for candidate in ordered[: policy.max_profiles]:
                number = candidate["company_number"]
                try:
                    p = await registry_tool("company_profile", {"company_number": number})
                    if (
                        p.get("company_number") != number
                        or p.get("company_status") != "active"
                        or not set(codes) & set(p.get("sic_codes", []))
                    ):
                        raise ValueError("Registry identity, status or SIC mismatch")
                    identity = PeerIdentity(
                        company_number=number,
                        company_name=p["company_name"],
                        sic_codes=p["sic_codes"],
                        registry_url=f"https://api.company-information.service.gov.uk/company/{number}",
                    )
                    if p["accounts"]["last_accounts"].get("type") == "dormant":
                        raise ValueError("Dormant accounts are not an operating peer")
                    end = date.fromisoformat(p["accounts"]["last_accounts"]["made_up_to"])
                    # Prior-year proximity ranks downloads only. Actual filing dates
                    # must still pass comparison validation after extraction.
                    proximity = min(
                        min(abs((end - period).days), abs((end - period).days - 365))
                        for period in subject_periods
                    )
                    profiles.append((proximity, number, identity))
                except Exception:
                    report.gaps.append(
                        f"Candidate {number}: registry identity or accounts metadata could not be validated."
                    )
                    record_decision(
                        report,
                        number,
                        "excluded",
                        "Registry identity, active status, SIC or accounts metadata failed validation.",
                    )
            attempts = 0
            for separation, number, identity in sorted(
                profiles,
                key=lambda row: (
                    -sum(
                        len(c.shared_facets)
                        for c in (report.discovery.candidates if report.discovery else [])
                        if c.company_number == row[1]
                    ),
                    *row[:2],
                ),
            ):
                if separation > policy.max_period_gap_days + 366:
                    report.gaps.append(
                        f"{number}: latest filed period outside comparison date window."
                    )
                    record_decision(
                        report,
                        number,
                        "excluded",
                        "Reporting periods fall outside the comparison window.",
                    )
                    continue
                if attempts >= policy.max_attempts or len(report.peers) >= policy.max_peers:
                    break
                attempts += 1
                selection_audit = {}
                peer_accounts = await collect_accounts(
                    number,
                    identity.company_name,
                    benchmark_registry(
                        registry_tool,
                        number,
                        subject_periods,
                        policy.max_period_gap_days,
                        selection_audit,
                    ),
                    sector,
                    include_notes=False,
                )
                if selection_audit and not selection_audit["selected_periods"]:
                    reason = (
                        f"No filed accounts within {policy.max_period_gap_days} days of subject periods "
                        + ", ".join(str(p) for p in sorted(subject_periods))
                        + "; available peer periods: "
                        + (", ".join(selection_audit["available_periods"]) or "none")
                        + ". No financial extraction attempted."
                    )
                    record_decision(report, number, "excluded", reason)
                    report.gaps.append(f"{number}: {reason}")
                    attempts -= 1
                    continue
                report.gaps.extend(f"{number}: {gap}" for gap in peer_accounts.get("gaps", []))
                values, peer_gaps = measurements(number, peer_accounts, policy, target)
                comparisons, comparison_gaps = compare_measurements(subject, values, policy)
                if not sector_accounts_verified(peer_accounts, sector, target):
                    comparisons = []
                if not comparisons:
                    # Fall back as a whole dated benchmark, never one metric at a time.
                    prior_periods = sorted(
                        {
                            date.fromisoformat(f["period"])
                            for f in accounts.get("filings", [])
                            if date.fromisoformat(f["period"]) < target
                        },
                        reverse=True,
                    )
                    for prior in prior_periods:
                        if not sector_accounts_verified(
                            accounts, sector, prior
                        ) or not sector_accounts_verified(peer_accounts, sector, prior):
                            continue
                        older_subject, _ = measurements(
                            pack["company_number"], accounts, policy, prior
                        )
                        older_peer, older_gaps = measurements(number, peer_accounts, policy, prior)
                        older_comparisons, older_comparison_gaps = compare_measurements(
                            older_subject, older_peer, policy
                        )
                        if older_comparisons:
                            comparisons, peer_gaps, comparison_gaps = (
                                older_comparisons,
                                older_gaps,
                                older_comparison_gaps,
                            )
                            report.gaps.append(
                                f"{number}: no validated benchmark for subject latest period {target}; historical subject period {prior} used explicitly. This does not describe current relative performance."
                            )
                            break
                report.gaps.extend(peer_gaps)
                report.gaps.extend(f"{number}: {g}" for g in comparison_gaps)
                if not comparisons:
                    report.gaps.append(
                        f"{number}: no comparable validated measurements; candidate excluded."
                    )
                    record_decision(
                        report,
                        number,
                        "excluded",
                        "Accounts did not yield compatible validated metrics for the selected periods and scope.",
                    )
                    continue
                size_reason = size_compatibility(
                    accounts, peer_accounts, comparisons[0], policy.max_asset_multiple
                )
                if size_reason:
                    report.gaps.append(f"{number}: {size_reason}")
                    record_decision(report, number, "excluded", size_reason)
                    continue
                report.peers.append(identity)
                report.comparisons.extend(comparisons)
                record_decision(
                    report,
                    number,
                    "included",
                    "Registry, sector accounts, asset-size band, period, scope and calculation checks passed.",
                )
    except TimeoutError:
        report.gaps.append(
            "Peer collection time budget exhausted; completed comparisons preserved."
        )
    except Exception as exc:
        report.gaps.append(
            f"Peer discovery/extraction unavailable ({type(exc).__name__}); completed comparisons preserved."
        )
    report.status = "partial" if report.comparisons else "unavailable"
    if report.discovery:
        for c in report.discovery.candidates:
            if c.status == "shortlisted":
                c.status = "excluded"
                c.reasons.append(
                    "Not admitted within the bounded accounts/time budget; financial comparability is unverified."
                )
    report.gaps = list(dict.fromkeys(report.gaps))
    return report, guidance


def record_decision(report, number, status, reason):
    if report.discovery:
        for candidate in report.discovery.candidates:
            if candidate.company_number == number:
                candidate.status = status
                candidate.reasons.append(reason)


def size_compatibility(subject_accounts, peer_accounts, comparison, maximum):
    amounts = []
    for accounts, period in [
        (subject_accounts, comparison.subject.period),
        (peer_accounts, comparison.peer.period),
    ]:
        found = set()
        for filing in accounts.get("filings", []):
            if filing.get("period") != str(period):
                continue
            try:
                fact = FinancialFact.model_validate(filing["figures"]["total_assets"])
                if fact.scope == "company" and fact.period == period and fact.value > 0:
                    found.add(fact.value * fact.scale)
            except (KeyError, ValueError, TypeError):
                pass
        if len(found) != 1:
            return (
                "Asset-size comparability could not be validated for the selected company periods."
            )
        amounts.append(next(iter(found)))
    if max(amounts) / min(amounts) > maximum:
        return f"Company asset sizes differ by more than the runbook's {maximum}× limit; peer excluded."
    return None


def add_peer_evidence(bundle, groups, report):
    from .validation import EvidenceItem

    groups["peer_report"] = report.model_dump(mode="json")
    groups["peer_comparisons"] = []
    for comparison in report.comparisons:
        eid = f"ev_{len(bundle.evidence) + 1:03d}"
        bundle.evidence.append(
            EvidenceItem(
                evidence_id=eid,
                claim="peer_comparison",
                source={"url": comparison.peer.sources[0].url, "kind": "companies_house_api"},
                verbatim_excerpt=comparison.peer.sources[0].excerpt,
                structured_fact={"peer_comparison": comparison.model_dump(mode="json")},
                confidence=0.8,
            )
        )
        groups["peer_comparisons"].append(eid)
    bundle.uncertainty.extend(report.gaps)


def render_peers(groups):
    report = groups.get("peer_report")
    if not report:
        return []
    lines = [
        "",
        "## Financial peer comparison",
        f"Coverage: {report['status']}. Sector: {report['sector']}.",
        f"Peer selection: {report['selection_mode']}.",
        "Financial benchmarks only: web business overlap is assessed from quoted pages where available; direct competition and independent ownership remain unverified. No market ranking, market share or credit score is inferred.",
        f"Runbook: {report['runbook_sha256']}; candidates considered: {report['candidates_considered']}.",
    ]
    if report.get("discovery"):
        from .peer_discovery import render_discovery

        lines += render_discovery(report["discovery"])
    for peer in report["peers"]:
        # JSON quoting prevents external names from becoming Markdown headings/links.
        import json

        lines.append(
            f"- Peer: {json.dumps(peer['company_name'])} ({peer['company_number']}); registry: {peer['registry_url']}"
        )
    if not report["comparisons"]:
        lines.append("No compatible peer measurements available; see explicit gaps below.")
    if groups.get("peer_highlights"):
        lines.append(
            "Model-selected comparison highlights: "
            + ", ".join(groups["peer_highlights"])
            + ". All validated comparisons follow."
        )
    for eid, raw in zip(groups["peer_comparisons"], report["comparisons"], strict=True):
        c = PeerComparison.model_validate(raw)
        a, b = c.subject, c.peer
        lines.append(
            f"- {a.metric} ({a.scope}; {a.reporting_basis}): subject {a.value:.2f}{a.unit} at {a.period}; peer {b.company_number} {b.value:.2f}{b.unit} at {b.period}; subject minus peer {c.difference:+.2f} {c.difference_unit.replace('_', ' ')}. Date separation: {c.period_gap_days} days. [{eid}]"
        )
        for label, measurement in (("Subject", a), ("Peer", b)):
            for s in measurement.sources:
                lines.append(
                    f"  - {label} input: {s.field} = {s.value} × {s.scale} {s.unit}; page {s.page}: {s.url}"
                )
    return lines
