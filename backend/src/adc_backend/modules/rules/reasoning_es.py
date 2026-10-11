"""
Spanish-language rendering of a single rule_trace entry (engine.py's
RuleTraceEntry.to_dict() shape: {rule, result, reasoning, inputs}).

Decided 2026-10-11, replacing an earlier approach that showed the raw
English `reasoning` string verbatim as the Telegram "Motivo" line - per
explicit instruction, this never translates that string after the
fact. Every template below is keyed by `rule` (+ `result` where the
same rule code has more than one real-world phrasing) and filled
*only* from that entry's own `inputs` dict - never reaching into other
trace entries, and never guessing a value that isn't actually there.

There are 13 distinct rule codes in engine.py. Only three of them can
ever be the *last* entry in a trace for an item that actually reaches
the approval queue (BUY/HIGH_RISK are the only labels that create an
approval item - see modules/approvals/service.py::APPROVAL_GATED_LABELS):
`amazon_has_buy_box`, `seller_count_high_risk`, `sales_velocity`. The
other ten are templated here anyway for completeness (asked for the
full list), but none of them can appear as "Motivo" today - each one's
docstring below says why.

No Spanish-language client brief document exists in this repo (searched
2026-10-11) - wording here is based on this file's own CLAUDE.md
business-rules table (English) translated sensibly, not a client-
approved Spanish source. Flagged, not claimed otherwise.
"""

from __future__ import annotations

from collections.abc import Callable


def format_pct_es(raw: str | int | float | None) -> str:
    """
    Spanish-locale percentage: comma decimal separator, space before
    the "%" sign - matches the exact worked example given this round
    ("ROI de 35,0 % por encima del mínimo de 25 %"). No digit-stripping
    needed: a computed value (roi_pct) naturally stringifies with a
    decimal ("35.0"), a plain config threshold naturally stringifies
    without one ("25") - confirmed directly, not assumed - so a plain
    "." -> "," swap already reproduces the example exactly either way.
    """
    if raw is None:
        return "?"
    return f"{str(raw).replace('.', ',')} %"


_RISK_LEVEL_ES = {"low": "bajo", "medium": "medio", "high": "alto", "unknown": "desconocido"}

_GATED_STATUS_ES = {
    "approved": "aprobado para esta cuenta - tratado como producto normal",
    "not_approved": "no aprobado - se debe enviar la factura a Amazon y esperar aprobación antes de poder comprarse",
    "pending_approval": "aprobación pendiente ante Amazon",
}


# Each entry: rule code -> callable(inputs: dict, result: str) -> str.
# Only given `inputs` and `result` (never other trace entries, never
# recomputed values) - exactly what that one trace entry carries.
_TEMPLATES: dict[str, Callable[[dict, str], str]] = {
    # --- hard excludes - never reach the approval queue, templated for completeness ---
    "restricted_product": lambda i, r: "Producto restringido - exclusión permanente, nunca se muestra como opción.",
    "manufacturer_sells_directly": lambda i, r: "El fabricante vende directamente en este listado - no se puede competir, exclusión permanente.",
    "ambiguous_restriction_reason": lambda i, r: (
        "SP-API devolvió un motivo de restricción ambiguo - no se puede determinar automáticamente si es una "
        "restricción permanente o un bloqueo de marca que se puede resolver. Enviado a revisión manual."
    ),
    "financial_data_available": lambda i, r: "Falta el costo del proveedor o el precio de venta en Amazon - no se puede calcular ROI ni margen.",
    # --- gating - informational, never the last entry for BUY/HIGH_RISK ---
    "gated_product": lambda i, r: f"Producto restringido por categoría (gated), {_GATED_STATUS_ES.get(i.get('gated_approval_status'), i.get('gated_approval_status', '?'))}.",
    "gated_pending_strength_check": lambda i, r: (
        "Oferta suficientemente atractiva para mostrarse a pesar de la aprobación pendiente de Amazon."
        if r == "pass"
        else "Oferta no lo bastante atractiva para mostrarse mientras está pendiente de aprobación de Amazon."
    ),
    # --- financial math - always present for BUY/HIGH_RISK, but never last ---
    "financial_calculation": lambda i, r: f"Precio de venta ${i.get('sell_price', '?')} - comisiones ${i.get('fees', '?')} - costo ${i.get('unit_cost', '?')} = ganancia ${i.get('profit', '?')}.",
    "roi_threshold": lambda i, r: (
        f"ROI de {format_pct_es(i.get('roi_pct'))} por encima del mínimo de {format_pct_es(i.get('threshold'))}."
        if r == "pass"
        else f"ROI de {format_pct_es(i.get('roi_pct'))} está por debajo de {format_pct_es(i.get('threshold'))} pero por encima del mínimo de negociación de {format_pct_es(i.get('negotiate_floor'))} - los precios iniciales del proveedor suelen estar inflados."
        if r == "info"
        else f"ROI de {format_pct_es(i.get('roi_pct'))} está por debajo del mínimo de negociación de {format_pct_es(i.get('negotiate_floor'))}."
    ),
    "margin_threshold": lambda i, r: f"Margen de {format_pct_es(i.get('margin_pct'))} frente al mínimo requerido de {format_pct_es(i.get('required_margin_pct'))}.",
    # --- risk factors - seller_count_risk is informational, never last ---
    "seller_count_risk": lambda i, r: f"{i.get('seller_count', 'desconocido')} vendedores → riesgo {_RISK_LEVEL_ES.get(i.get('risk_level'), i.get('risk_level', 'desconocido'))}.",
    # --- the three rules that CAN be "Motivo" for a real approval item ---
    "amazon_has_buy_box": lambda i, r: "Amazon tiene la Buy Box - no se excluye, pero se marca como de mayor riesgo/dificultad.",
    "seller_count_high_risk": lambda i, r: f"{i.get('seller_count', 'La cantidad de')} vendedores competidores representa un riesgo alto, aunque no es un límite estricto.",
    "sales_velocity": lambda i, r: (
        f"{i.get('sales_rank_drops_30d')} eventos de mejora de ranking de ventas en 30 días, frente al mínimo de {i.get('minimum')}."
        if r == "pass" and i.get("sales_rank_drops_30d") is not None
        else "No hay datos de velocidad de ventas (Keepa) disponibles para este ASIN todavía - se continúa sin ellos."
    ),
}


def motivo_es(entry: dict | None) -> str | None:
    """
    Renders one rule_trace entry (dict with rule/result/inputs) into
    its Spanish "Motivo" line. Returns None only if `entry` itself is
    falsy (empty trace) - an unrecognized rule code still returns
    something concrete (the raw code + its English reasoning, clearly
    marked untranslated) rather than silently going blank, consistent
    with this project's "never guess, never go silent" discipline.
    """
    if not entry:
        return None
    rule = entry.get("rule")
    template = _TEMPLATES.get(rule)
    if template is None:
        reasoning = entry.get("reasoning", "")
        return f"[{rule}, sin plantilla en español] {reasoning}"
    return template(entry.get("inputs") or {}, entry.get("result", ""))
