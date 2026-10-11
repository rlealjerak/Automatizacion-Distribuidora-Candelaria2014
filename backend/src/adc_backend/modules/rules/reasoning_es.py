"""
Spanish-language rendering of rule_trace (engine.py's RuleTraceEntry.
to_dict() shape: {rule, result, reasoning, inputs}) for the owner-facing
Telegram message.

Rewritten 2026-10-11 (second pass) per explicit instruction: don't use
"whichever rule happens to be the last trace entry" as the reason -
instead, look up specific named rules and build two distinct lines:

- "Por qué" (BUY and HIGH_RISK alike): built from roi_threshold,
  margin_threshold, and sales_velocity - the three rules that actually
  explain why this is a viable purchase at all, found by rule *name*
  anywhere in the trace, never by position.
- "Riesgo" (HIGH_RISK only): built from whichever risk rule actually
  fired - amazon_has_buy_box or seller_count_high_risk - also found by
  name, not position.

Plain business Spanish throughout, no internal/engine phrasing (e.g.
"ventas estimadas", not "eventos de mejora de ranking"; the Buy Box
line reads "Amazon vende este producto y tiene la Buy Box," not
anything about "excluded"/"flagged"). No Spanish-language client brief
document exists anywhere in this repo (searched directly, both passes)
- wording is based on CLAUDE.md's own English business-rules table,
translated sensibly, not a client-approved Spanish source.

roi_threshold and margin_threshold are always `result="pass"` for any
item that reaches here (BUY/HIGH_RISK) - a fail/negotiate result on
either one terminates before reaching an approval-gated label, per
engine.py's own control flow. sales_velocity is `"pass"` or `"info"`
(no Keepa data yet) - never `"fail"` for an approval-gated item either.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def format_pct_es(raw: str | int | float | None) -> str:
    """
    One format everywhere: decimal point, one decimal place, no space
    before "%" - e.g. "34.0%". Forces exactly one decimal regardless of
    how the source value stringified (a computed value vs. a plain
    config threshold previously differed - "35.0" vs "25" - forcing the
    same precision on both is the actual fix for "one format
    everywhere," not something either raw string already guaranteed).
    """
    if raw is None:
        return "?"
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return f"{raw}%"
    return f"{value.quantize(Decimal('0.1'))}%"


def format_money_es(raw: str | int | float | None) -> str:
    """
    One format everywhere: "US$" prefix, comma thousands separator,
    period decimal, two decimal places - e.g. "US$360.00", "US$1,234.56".
    """
    if raw is None:
        return "?"
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return f"US${raw}"
    return f"US${value.quantize(Decimal('0.01')):,}"


def _find_rule(rule_trace: list[dict], rule_name: str) -> dict | None:
    for entry in rule_trace:
        if entry.get("rule") == rule_name:
            return entry
    return None


def por_que_es(rule_trace: list[dict]) -> str | None:
    """
    Built from three specific named rules - roi_threshold,
    margin_threshold, sales_velocity - found anywhere in the trace,
    never assumed to be at any particular position. Returns None only
    if roi_threshold or margin_threshold is genuinely missing (would
    mean this wasn't actually a BUY/HIGH_RISK trace at all - never
    guess a reason in that case).
    """
    roi = _find_rule(rule_trace, "roi_threshold")
    margin = _find_rule(rule_trace, "margin_threshold")
    velocity = _find_rule(rule_trace, "sales_velocity")
    if roi is None or margin is None:
        return None

    roi_i, margin_i = roi.get("inputs") or {}, margin.get("inputs") or {}
    parts = [
        f"ROI de {format_pct_es(roi_i.get('roi_pct'))} (mínimo {format_pct_es(roi_i.get('threshold'))})",
        f"margen de {format_pct_es(margin_i.get('margin_pct'))} (mínimo {format_pct_es(margin_i.get('required_margin_pct'))})",
    ]

    if velocity is not None:
        v_i = velocity.get("inputs") or {}
        drops = v_i.get("sales_rank_drops_30d")
        minimum = v_i.get("minimum")
        if drops is not None:
            parts.append(f"ventas estimadas de {drops}/mes (mínimo {minimum}/mes)")
        else:
            parts.append("ventas estimadas sin datos de Keepa todavía")

    return ", ".join(parts) + "."


def riesgo_es(rule_trace: list[dict]) -> str | None:
    """
    HIGH_RISK only - whichever risk rule actually fired, found by name.
    Returns None if neither is present (shouldn't happen for a real
    HIGH_RISK item, but never fabricate a risk reason if it is).
    """
    buy_box = _find_rule(rule_trace, "amazon_has_buy_box")
    if buy_box is not None:
        return "Amazon vende este producto y tiene la Buy Box."

    seller_count = _find_rule(rule_trace, "seller_count_high_risk")
    if seller_count is not None:
        count = (seller_count.get("inputs") or {}).get("seller_count")
        if count is not None:
            return f"{count} vendedores compitiendo por este producto."
        return "Muchos vendedores compitiendo por este producto."

    return None
