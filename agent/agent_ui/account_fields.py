"""Typed analysis requirements. These describe evidence sought, not inferred values."""

from pydantic import BaseModel, ConfigDict, Field


class AccountingField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    field_id: str = Field(description="Stable identifier for the required analysis field.")
    description: str = Field(
        description="Meaning and evidence needed; not a company-specific fact."
    )
    evidence_kind: str = Field(
        description="Published amounts, published ratios, or attributed narrative."
    )
    companion_headings: tuple[str, ...] = Field(
        default=(),
        description="Generic observed note-heading aliases to check alongside the primary note. Candidate retrieval only; no inferred financial findings.",
    )
    scope_rule: str = Field(
        default="Preserve company/group, reporting date, currency and units. Do not infer missing values."
    )


COMMON = [
    AccountingField(
        field_id="profit_explanation",
        description="Profit movements, operating costs, adjustments and management explanations. Find detailed income/cost notes, not only the statement.",
        evidence_kind="amounts and attributed narrative",
    ),
    AccountingField(
        field_id="cash_flow",
        description="Cash equivalents, operating cash flow and changes in cash composition, including deposits maturing.",
        evidence_kind="published amounts",
    ),
    AccountingField(
        field_id="related_parties",
        description="Related-party funding, transfers, balances, terms and guarantees; balances differ from flows. Relevant loans and balances may also appear in other assets, other receivables or other liabilities, not only the dedicated related-party note. Follow explicit note references where available; do not infer a related-party relationship from the heading alone.",
        evidence_kind="amounts and attributed narrative",
        companion_headings=(
            "other assets",
            "other receivables",
            "trade and other receivables",
            "other liabilities",
            "other payables",
            "trade and other payables",
        ),
    ),
    AccountingField(
        field_id="guarantees",
        description="Contingent liabilities, commitments and guarantees, including group exposures.",
        evidence_kind="amounts and attributed narrative",
    ),
]
BANK = [
    AccountingField(
        field_id="loan_impairments",
        description="Loan quality and expected credit loss charges, staging and impairment allowances.",
        evidence_kind="published amounts",
    ),
    AccountingField(
        field_id="funding",
        description="Customer deposits, bank funding, debt maturities and funding concentration.",
        evidence_kind="amounts and attributed narrative",
    ),
    AccountingField(
        field_id="cet1",
        description="Published CET1 ratio and capital adequacy disclosures. Capital amounts are not ratios.",
        evidence_kind="published ratio",
        scope_rule="Require explicit regulatory perimeter, date, numerator/denominator basis if given; no compliance inference.",
    ),
    AccountingField(
        field_id="lcr",
        description="Published liquidity coverage ratio (LCR), not loans/deposits or cash balances.",
        evidence_kind="published ratio",
        scope_rule="Keep regulatory perimeter, date, average versus point-in-time basis and averaging window.",
    ),
    AccountingField(
        field_id="nsfr",
        description="Published net stable funding ratio (NSFR); do not substitute other liquidity measures.",
        evidence_kind="published ratio",
        scope_rule="Keep regulatory perimeter, date and reporting basis.",
    ),
    AccountingField(
        field_id="securitisation",
        description="Securitisation, transferred assets, retained risks and associated funding.",
        evidence_kind="amounts and attributed narrative",
    ),
]
TRADING = [
    AccountingField(
        field_id="leases",
        description="Undiscounted lease maturity commitments: look in liquidity risk or financial risk management maturity tables. Lease accounting policies and right-of-use asset depreciation do not supply a maturity schedule. Keep lease liabilities and lease cash payments separate.",
        evidence_kind="published amounts",
    ),
    AccountingField(
        field_id="working_capital",
        description="Inventory, receivables and payables; movements affecting liquidity.",
        evidence_kind="published amounts",
    ),
    AccountingField(
        field_id="employee_costs",
        description="Employee remuneration: wages and salaries, social security costs, pensions and share-based payment expense. Look for the employee remuneration or staff costs note, separately from the operating-profit reconciliation.",
        evidence_kind="published amounts",
    ),
]


def fields_for(company_type):
    return COMMON + (BANK if company_type == "bank" else TRADING)
