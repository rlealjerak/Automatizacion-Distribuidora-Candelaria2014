"""
Direct unit tests for modules/rules/reasoning_es.py - pure functions,
no DB needed. Covers all 13 real rule codes from engine.py (confirmed
via `grep -n 'rule="' engine.py`, 2026-10-11), not just the three that
can actually be the last trace entry for an approval-gated item.
"""

from __future__ import annotations

from adc_backend.modules.rules.reasoning_es import format_pct_es, motivo_es


def test_format_pct_es_matches_the_worked_example():
    # "ROI de 35,0 % por encima del mínimo de 25 %" - the exact example given.
    assert format_pct_es("35.0") == "35,0 %"
    assert format_pct_es("25") == "25 %"


def test_format_pct_es_none_is_a_placeholder_not_a_crash():
    assert format_pct_es(None) == "?"


def test_motivo_es_empty_trace_is_none():
    assert motivo_es(None) is None
    assert motivo_es({}) is None


# --- the three rules that CAN be "Motivo" for a real approval item ---


def test_amazon_has_buy_box():
    entry = {"rule": "amazon_has_buy_box", "result": "fail", "inputs": {}}
    assert motivo_es(entry) == "Amazon tiene la Buy Box - no se excluye, pero se marca como de mayor riesgo/dificultad."


def test_seller_count_high_risk_uses_real_seller_count():
    entry = {"rule": "seller_count_high_risk", "result": "fail", "inputs": {"seller_count": 14}}
    assert motivo_es(entry) == "14 vendedores competidores representa un riesgo alto, aunque no es un límite estricto."


def test_sales_velocity_pass_with_data():
    entry = {"rule": "sales_velocity", "result": "pass", "inputs": {"sales_rank_drops_30d": 60, "minimum": 30}}
    assert motivo_es(entry) == "60 eventos de mejora de ranking de ventas en 30 días, frente al mínimo de 30."


def test_sales_velocity_info_no_data():
    entry = {"rule": "sales_velocity", "result": "info", "inputs": {}}
    assert motivo_es(entry) == "No hay datos de velocidad de ventas (Keepa) disponibles para este ASIN todavía - se continúa sin ellos."


# --- the other ten rules - never reachable as "Motivo" today, templated for completeness ---


def test_restricted_product():
    assert "exclusión permanente" in motivo_es({"rule": "restricted_product", "result": "hard_exclude", "inputs": {}})


def test_manufacturer_sells_directly():
    assert "fabricante" in motivo_es({"rule": "manufacturer_sells_directly", "result": "hard_exclude", "inputs": {}})


def test_ambiguous_restriction_reason():
    assert "revisión manual" in motivo_es({"rule": "ambiguous_restriction_reason", "result": "info", "inputs": {}})


def test_financial_data_available():
    assert "ROI ni margen" in motivo_es({"rule": "financial_data_available", "result": "fail", "inputs": {}})


def test_gated_product_approved_vs_not_approved():
    approved = motivo_es({"rule": "gated_product", "result": "info", "inputs": {"gated_approval_status": "approved"}})
    not_approved = motivo_es({"rule": "gated_product", "result": "info", "inputs": {"gated_approval_status": "not_approved"}})
    assert "tratado como producto normal" in approved
    assert "esperar aprobación" in not_approved


def test_gated_pending_strength_check():
    assert "pass" in "pass"  # sanity
    passed = motivo_es({"rule": "gated_pending_strength_check", "result": "pass", "inputs": {}})
    failed = motivo_es({"rule": "gated_pending_strength_check", "result": "fail", "inputs": {}})
    assert "suficientemente atractiva" in passed
    assert "no lo bastante atractiva" in failed


def test_financial_calculation():
    entry = {"rule": "financial_calculation", "result": "info", "inputs": {"sell_price": "20.00", "fees": "0", "unit_cost": "10.00", "profit": "10.00"}}
    assert motivo_es(entry) == "Precio de venta $20.00 - comisiones $0 - costo $10.00 = ganancia $10.00."


def test_roi_threshold_all_three_results():
    passed = motivo_es({"rule": "roi_threshold", "result": "pass", "inputs": {"roi_pct": "35.0", "threshold": "25"}})
    negotiate = motivo_es({"rule": "roi_threshold", "result": "info", "inputs": {"roi_pct": "22.0", "threshold": "25", "negotiate_floor": "20"}})
    failed = motivo_es({"rule": "roi_threshold", "result": "fail", "inputs": {"roi_pct": "10.0", "threshold": "25", "negotiate_floor": "20"}})
    assert passed == "ROI de 35,0 % por encima del mínimo de 25 %."
    assert "inflados" in negotiate
    assert "por debajo del mínimo de negociación" in failed


def test_margin_threshold():
    entry = {"rule": "margin_threshold", "result": "pass", "inputs": {"margin_pct": "18.5", "required_margin_pct": "10"}}
    assert motivo_es(entry) == "Margen de 18,5 % frente al mínimo requerido de 10 %."


def test_seller_count_risk():
    entry = {"rule": "seller_count_risk", "result": "info", "inputs": {"seller_count": 5, "risk_level": "low"}}
    assert motivo_es(entry) == "5 vendedores → riesgo bajo."


def test_unrecognized_rule_code_falls_back_clearly_marked():
    entry = {"rule": "some_future_rule", "result": "info", "reasoning": "English text", "inputs": {}}
    out = motivo_es(entry)
    assert "some_future_rule" in out
    assert "sin plantilla" in out
    assert "English text" in out
