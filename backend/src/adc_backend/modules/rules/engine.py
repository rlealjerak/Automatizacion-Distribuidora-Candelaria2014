"""
Rule engine: pure function from (financial inputs, live Amazon data,
rules config) to a classification + full reasoning trace. Deliberately
takes plain dataclasses/dicts, not ORM objects - "deterministic" per
CLAUDE.md describes this calculation code, and a pure function is how
that's actually verifiable, independent of the database.

Every evaluated factor is appended to the trace, in order, whether it
passed or failed or was merely informational - `ClassificationResult.
rule_trace` is NOT NULL specifically so the owner always sees the full
"why" (see docs/decisions/0002-database-schema-decisions.md). Hard
excludes short-circuit the remaining financial math (a restricted
product's ROI doesn't matter) but still produce a complete trace
explaining the exclusion.

Classification label mapping, reasoned from CLAUDE.md's rules table (this
system's fixed 5 labels don't include a separate "hard excluded" state,
so hard excludes map to NO_BUY with an unambiguous trace reason - a
caller building an owner-facing list should treat a NO_BUY whose top
trace entry is a hard exclude as "never show as an option" per CLAUDE.md,
distinct from an ordinary NO_BUY on the financials alone):
  - restricted                                  -> NO_BUY (hard exclude)
  - manufacturer sells directly                 -> NO_BUY (hard exclude)
  - gated, not approved, deal not notably strong -> NO_BUY (not discarded from
    the DB, just not worth surfacing as-is - CLAUDE.md: "don't discard")
  - gated, not approved, deal notably strong     -> REVIEW (must submit
    invoice + await Amazon approval before purchasable)
  - ROI below the negotiate floor, or margin fails -> NO_BUY
  - ROI in the negotiate band                   -> NEGOTIATE
  - ROI/margin clear, but Amazon owns the buy box
    or seller-count risk is high                -> HIGH_RISK
  - ROI/margin clear, velocity data present but weak -> REVIEW
  - everything clears                           -> BUY
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from adc_backend.modules.rules.models import ClassificationLabel


@dataclass
class ClassificationInputs:
    unit_cost: Decimal | None  # supplier's per-unit price (RawLineItem.unit_price)
    sell_price: Decimal | None  # buy_box_price if available, else current_price
    referral_fee: Decimal | None
    fba_fee: Decimal | None
    other_fees_total: Decimal
    is_restricted: bool
    is_gated: bool
    gated_approval_status: str  # "not_applicable" | "approved" | "pending_approval" | "not_approved"
    manufacturer_sells_directly: bool
    amazon_has_buy_box: bool | None
    seller_count: int | None
    sales_rank_drops_30d: int | None


@dataclass
class RuleTraceEntry:
    rule: str
    result: str  # "pass" | "fail" | "hard_exclude" | "info"
    reasoning: str
    inputs: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"rule": self.rule, "result": self.result, "reasoning": self.reasoning, "inputs": self.inputs}


@dataclass
class ClassificationDecision:
    classification: ClassificationLabel
    roi_pct: Decimal | None
    margin_pct: Decimal | None
    rule_trace: list[dict]


def _required_margin_pct(sell_price: Decimal, exception_table: list[dict]) -> float:
    for tier in exception_table:
        max_price = tier["max_price"]
        if max_price is None or sell_price <= Decimal(str(max_price)):
            return tier["min_margin_pct"]
    return exception_table[-1]["min_margin_pct"]  # defensive fallback if the table has no catch-all row


def _seller_count_risk(seller_count: int | None, config: dict) -> str:
    if seller_count is None:
        return "unknown"
    if seller_count <= config["seller_count_low_risk_max"]:
        return "low"
    if seller_count <= config["seller_count_medium_risk_max"]:
        return "medium"
    return "high"


def classify(inputs: ClassificationInputs, config: dict) -> ClassificationDecision:
    trace: list[RuleTraceEntry] = []

    # --- hard excludes, evaluated first, short-circuit everything else ---

    if inputs.is_restricted:
        trace.append(
            RuleTraceEntry(
                rule="restricted_product",
                result="hard_exclude",
                reasoning="Product is restricted - hard exclude per policy, never shown as a buyable option.",
            )
        )
        return ClassificationDecision(ClassificationLabel.NO_BUY, None, None, [e.to_dict() for e in trace])

    if inputs.manufacturer_sells_directly:
        trace.append(
            RuleTraceEntry(
                rule="manufacturer_sells_directly",
                result="hard_exclude",
                reasoning="The manufacturer sells directly on this listing - cannot compete, hard exclude.",
            )
        )
        return ClassificationDecision(ClassificationLabel.NO_BUY, None, None, [e.to_dict() for e in trace])

    # --- gating (two sub-states per CLAUDE.md, not a single flag) ---

    gated_pending = inputs.is_gated and inputs.gated_approval_status == "not_approved"
    if inputs.is_gated:
        trace.append(
            RuleTraceEntry(
                rule="gated_product",
                result="info",
                reasoning=(
                    "Gated, already approved for this seller account - treated as a normal product."
                    if inputs.gated_approval_status == "approved"
                    else "Gated, not yet approved - owner must submit invoice to Amazon and await approval "
                    "before this is purchasable, even if surfaced below."
                ),
                inputs={"gated_approval_status": inputs.gated_approval_status},
            )
        )

    # --- financial math ---

    if inputs.unit_cost is None or inputs.sell_price is None or inputs.unit_cost == 0:
        trace.append(
            RuleTraceEntry(
                rule="financial_data_available",
                result="fail",
                reasoning="Missing supplier cost or Amazon sell price - can't compute ROI/margin. Never guess.",
                inputs={"unit_cost": str(inputs.unit_cost), "sell_price": str(inputs.sell_price)},
            )
        )
        return ClassificationDecision(ClassificationLabel.REVIEW, None, None, [e.to_dict() for e in trace])

    fees = (inputs.referral_fee or Decimal(0)) + (inputs.fba_fee or Decimal(0)) + inputs.other_fees_total
    net_revenue = inputs.sell_price - fees
    profit = net_revenue - inputs.unit_cost
    roi_pct = (profit / inputs.unit_cost) * 100
    margin_pct = (profit / inputs.sell_price) * 100

    trace.append(
        RuleTraceEntry(
            rule="financial_calculation",
            result="info",
            reasoning=f"sell_price {inputs.sell_price} - fees {fees} - unit_cost {inputs.unit_cost} = profit {profit}",
            inputs={
                "sell_price": str(inputs.sell_price),
                "fees": str(fees),
                "unit_cost": str(inputs.unit_cost),
                "profit": str(profit),
                "roi_pct": str(roi_pct),
                "margin_pct": str(margin_pct),
            },
        )
    )

    roi_threshold = Decimal(str(config["roi_threshold_pct"]))
    roi_negotiate_floor = Decimal(str(config["roi_negotiate_floor_pct"]))
    roi_ok = roi_pct >= roi_threshold
    roi_negotiate = roi_negotiate_floor <= roi_pct < roi_threshold
    trace.append(
        RuleTraceEntry(
            rule="roi_threshold",
            result="pass" if roi_ok else ("info" if roi_negotiate else "fail"),
            reasoning=(
                f"ROI {roi_pct:.1f}% >= threshold {roi_threshold}%"
                if roi_ok
                else f"ROI {roi_pct:.1f}% is below {roi_threshold}% but above the negotiate floor "
                f"{roi_negotiate_floor}% - first-round supplier prices are often inflated."
                if roi_negotiate
                else f"ROI {roi_pct:.1f}% is below the negotiate floor {roi_negotiate_floor}%"
            ),
            inputs={"roi_pct": str(roi_pct), "threshold": str(roi_threshold), "negotiate_floor": str(roi_negotiate_floor)},
        )
    )

    required_margin = Decimal(str(_required_margin_pct(inputs.sell_price, config["margin_exception_table"])))
    margin_ok = margin_pct >= required_margin
    trace.append(
        RuleTraceEntry(
            rule="margin_threshold",
            result="pass" if margin_ok else "fail",
            reasoning=f"Margin {margin_pct:.1f}% vs required {required_margin}% for a ${inputs.sell_price} item",
            inputs={"margin_pct": str(margin_pct), "required_margin_pct": str(required_margin)},
        )
    )

    # --- gated-pending: only worth surfacing if the deal is notably strong ---

    if gated_pending:
        strong_multiplier = Decimal(str(config["gated_pending_strong_deal_multiplier"]))
        is_notably_strong = roi_pct >= roi_threshold * strong_multiplier and margin_ok
        trace.append(
            RuleTraceEntry(
                rule="gated_pending_strength_check",
                result="pass" if is_notably_strong else "fail",
                reasoning=(
                    f"ROI {roi_pct:.1f}% clears {strong_multiplier}x the normal threshold "
                    f"({roi_threshold * strong_multiplier}%) - strong enough to surface despite pending approval."
                    if is_notably_strong
                    else "Deal isn't notably strong enough to surface a gated-pending item - not excluded from "
                    "the database, just not worth showing as an actionable option yet."
                ),
            )
        )
        if not is_notably_strong:
            return ClassificationDecision(ClassificationLabel.NO_BUY, roi_pct, margin_pct, [e.to_dict() for e in trace])
        return ClassificationDecision(ClassificationLabel.REVIEW, roi_pct, margin_pct, [e.to_dict() for e in trace])

    if not roi_ok and not roi_negotiate:
        return ClassificationDecision(ClassificationLabel.NO_BUY, roi_pct, margin_pct, [e.to_dict() for e in trace])
    if not margin_ok:
        return ClassificationDecision(ClassificationLabel.NO_BUY, roi_pct, margin_pct, [e.to_dict() for e in trace])
    if roi_negotiate:
        return ClassificationDecision(ClassificationLabel.NEGOTIATE, roi_pct, margin_pct, [e.to_dict() for e in trace])

    # --- risk factors: not exclusions, but can promote to HIGH_RISK or REVIEW ---

    seller_risk = _seller_count_risk(inputs.seller_count, config)
    trace.append(
        RuleTraceEntry(
            rule="seller_count_risk",
            result="info",
            reasoning=f"{inputs.seller_count if inputs.seller_count is not None else 'unknown'} sellers -> {seller_risk} risk",
            inputs={"seller_count": inputs.seller_count, "risk_level": seller_risk},
        )
    )

    if inputs.amazon_has_buy_box:
        trace.append(
            RuleTraceEntry(
                rule="amazon_has_buy_box",
                result="fail",
                reasoning="Amazon holds the buy box - not excluded, but flagged as materially higher risk/difficulty.",
            )
        )
        return ClassificationDecision(ClassificationLabel.HIGH_RISK, roi_pct, margin_pct, [e.to_dict() for e in trace])

    if seller_risk == "high":
        trace.append(
            RuleTraceEntry(
                rule="seller_count_high_risk",
                result="fail",
                reasoning=f"{inputs.seller_count} competing sellers is high risk, even though it's not a hard cutoff.",
            )
        )
        return ClassificationDecision(ClassificationLabel.HIGH_RISK, roi_pct, margin_pct, [e.to_dict() for e in trace])

    velocity_min = config["sales_rank_drops_30d_min"]
    if inputs.sales_rank_drops_30d is not None:
        velocity_ok = inputs.sales_rank_drops_30d >= velocity_min
        trace.append(
            RuleTraceEntry(
                rule="sales_velocity",
                result="pass" if velocity_ok else "fail",
                reasoning=f"{inputs.sales_rank_drops_30d} sales-rank-improvement events/30d vs minimum {velocity_min}",
                inputs={"sales_rank_drops_30d": inputs.sales_rank_drops_30d, "minimum": velocity_min},
            )
        )
        if not velocity_ok:
            return ClassificationDecision(ClassificationLabel.REVIEW, roi_pct, margin_pct, [e.to_dict() for e in trace])
    else:
        trace.append(
            RuleTraceEntry(
                rule="sales_velocity",
                result="info",
                reasoning="No Keepa velocity data available for this ASIN yet - proceeding without it.",
            )
        )

    return ClassificationDecision(ClassificationLabel.BUY, roi_pct, margin_pct, [e.to_dict() for e in trace])
