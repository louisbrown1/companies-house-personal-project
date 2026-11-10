---
type: Playbook
title: Banking accounts interpretation
description: Banking statement labels and disclosure distinctions.
tags: [accounts, bank]
generated: {by: codex/accounts-context-v1, at: '2026-11-07T22:14:04+00:00'}
status: stable
stale_after: '2027-02-07'
sources:
  - id: labels
    resource: repo:agent/agent_ui/scoped_accounts.py
    title: Supported bank statement labels
  - id: disclosures
    resource: repo:agent/agent_ui/banking_notes.py
    title: Regulatory disclosure extraction rules
---
# Statement interpretation

Interest income, interest expense, net interest income and total operating income
are separate metrics. Customer accounts and customer deposits may refer to the
customer_deposits metric; amounts due to banks may refer to
credit_institution_funding. These labels still require source scope, date and
unit checks. Central-bank balances alone are not total cash equivalents.[^labels]

# Observation selection

Prioritise company and group profitability, net interest income, loan impairment,
customer loans, deposits, cash and funding movements where supported. Keep group
results separate from the standalone bank. Include adverse movements and gaps.

Capital amounts are not capital ratios. CET1, total capital, leverage, LCR and
NSFR are distinct disclosures. Preserve point-in-time versus average reporting
and averaging windows. Customer loans/deposits is not LCR or NSFR. Never infer
regulatory headroom, compliance or missing ratios from other balances. Missing
ratios require further source review; they do not establish non-disclosure.[^disclosures]
