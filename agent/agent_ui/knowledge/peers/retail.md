---
type: Playbook
title: Retail financial benchmarks
status: stable
policy:
  sector: retail
  metrics: [current_ratio, equity_to_assets]
  max_period_gap_days: 93
  max_candidates: 20
  max_profiles: 6
  max_attempts: 3
  max_peers: 2
  max_asset_multiple: 10
  budget_seconds: 600
---
Require positive company revenue and total assets before admitting a candidate. Retail SIC matches can cover different products, channels and locations. Do not describe them as direct competitors. Current ratio is a balance-sheet measure, not a liquidity forecast. Negative liabilities are not silently converted to positive denominators.

Use only validated comparison evidence IDs. Describe differences without declaring a winner or producing a credit score. Keep company and consolidated group scopes separate. Retain both measurement dates and disclose any date mismatch, even within the 93-day limit. Do not compare unknown regulatory bases or incompatible averaging windows.

The bounded candidate sample is not representative of the market. Quoted issuer profiles support product/customer/geography interpretations; registry identity and accounts establish financial comparability, not direct competition or independent ownership. Do not calculate market share, rankings, sector averages or a competition score. Profitability/growth comparisons require validated period lengths and accounting definitions; these are not available in this version and must be withheld. OCR evidence still requires source review. Missing or conflicting evidence remains an explicit gap.

Select the subject latest filing and the nearest peer period from its two available annual filings before comparing metrics. Never fill individual missing metrics from another period. An older peer filing may be used only with its date displayed and within the configured date window.

If no compatible comparison exists for the latest subject period, permit a whole prior subject period with explicit historical labelling and dates. Never portray a historical benchmark as current performance, or fill isolated missing metrics using earlier years.

## Web peer selection
Establish the subject business profile using issuer pages and quoted evidence. Search snippets only propose candidates. Require a source-backed overlap in at least one product category, customer segment and operating geography. A registered office is not operating-geography evidence. Resolve each brand to its legal entity using an explicit registration number on issuer pages and Companies House candidates; reject ambiguous parent/subsidiary mappings and subject aliases. Describe semantic profile labels as model interpretations, not independently established facts.
For banks distinguish savings, residential mortgages, buy-to-let, business lending, consumer credit, asset finance and corporate banking, and individuals, landlords, SMEs and corporates. Do not classify all banks as equivalent merely because they accept deposits. Use the same source/identity discipline for retail and trading companies. Company total assets for the selected periods must be positive and within a 10x size ratio; missing or conflicting size evidence blocks admission. Ownership independence remains unverified unless separately established. Retain inclusion/exclusion reasons and the exact supporting quotes/URLs. Never fill a failed web shortlist with SIC-only candidates. Explicit user-configured peers remain an override of web business matching, never of the financial validation gates.
