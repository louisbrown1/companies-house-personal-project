---
type: Playbook
title: Retail accounts interpretation
description: Retail performance, lease and group-exposure context.
tags: [accounts, retail]
generated: {by: codex/accounts-context-v1, at: '2026-11-07T22:14:04+00:00'}
status: stable
stale_after: '2027-02-07'
sources:
  - id: calculations
    resource: repo:agent/agent_ui/financial_health.py
    title: Financial comparisons and reconciled bridges
  - id: notes
    resource: repo:agent/agent_ui/accounts_notes.py
    title: Selected lease and related-party note analysis
---
# Interpretation

Revenue and turnover can identify revenue. Gross profit and operating profit are
distinct. Prefer observations covering revenue, operating margin, profitability
changes, cash flow and working capital, including deterioration even when revenue
grows. Period lengths and reporting dates may differ; preserve the stated dates.
An arithmetic profit bridge does not establish the business cause.[^calculations]

Recognised lease liabilities and undiscounted lease commitments are distinct;
do not add them together. Management-adjusted profit is not statutory profit.
Cash increases may reflect maturing deposits rather than increased total liquid
resources. Group receivables are balances; intragroup cash transfers are flows.
Net flows do not identify gross transfers or counterparties. Guarantees require
review of their terms; standalone borrowings do not establish total exposure.
Only discuss inventory, lease commitments or explanations when supplied evidence
supports them; this playbook is not evidence that such findings exist.[^notes]
