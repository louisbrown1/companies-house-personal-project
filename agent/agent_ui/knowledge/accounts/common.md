---
type: Playbook
title: Common accounts interpretation
description: Evidence boundaries shared by all company types.
tags: [accounts, common]
generated: {by: codex/accounts-context-v1, at: '2026-11-07T22:14:04+00:00'}
status: stable
stale_after: '2027-02-07'
sources:
  - id: implementation
    resource: repo:agent/agent_ui/financial_health.py
    title: Application financial evidence and calculations
  - id: extraction
    resource: repo:agent/agent_ui/hybrid_accounts.py
    title: Source-bound statement extraction
---
# Interpretation

Preserve legal entity, company/group scope, reporting date, currency and printed
scale. Use explicit source columns; never fill missing values with zero. Keep
same-filing comparatives distinct from a separate year's filed accounts.[^extraction]

Profit before tax and profit after tax are different metrics. Operating profit,
cash generated from operations, and closing cash are different measures. Match
only a permitted metric to the supplied source row; omit ambiguous labels.
Calculations and reconciliation belong to Python. Context does not establish
that a company has a particular balance, obligation or risk.[^implementation]

Select balanced observations: current performance, deterioration, liquidity and
material gaps. A management explanation is attributed, not independently proven.
Do not produce a credit score, lending decision or regulatory compliance finding.
Missing evidence must remain visible.[^implementation]
