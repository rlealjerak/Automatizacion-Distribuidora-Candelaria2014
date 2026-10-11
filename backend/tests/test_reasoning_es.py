"""
Direct unit tests for modules/rules/reasoning_es.py - pure functions,
no DB needed.
"""

from __future__ import annotations

from adc_backend.modules.rules.reasoning_es import format_money_es, format_pct_es, por_que_es, riesgo_es


def test_format_pct_es_one_decimal_no_space():
    # "34.0%", "US$360.00", "US$1,234.56" - the exact format given.
    assert format_pct_es("34.0") == "34.0%"
    assert format_pct_es("25") == "25.0%"  # forced to one decimal, not left bare


def test_format_pct_es_none_is_a_placeholder_not_a_crash():
    assert format_pct_es(None) == "?"


def test_format_money_es_matches_the_worked_examples():
    assert format_money_es("360.00") == "US$360.00"
    assert format_money_es("1234.56") == "US$1,234.56"
    assert format_money_es("122.4") == "US$122.40"


def test_format_money_es_none_is_a_placeholder_not_a_crash():
    assert format_money_es(None) == "?"


def test_por_que_es_none_when_roi_or_margin_missing():
    assert por_que_es([]) is None
    assert por_que_es([{"rule": "sales_velocity", "result": "pass", "inputs": {}}]) is None


def test_por_que_es_finds_rules_by_name_not_position():
    # Deliberately out of the real engine.py order and interleaved with
    # unrelated entries - por_que_es must find each by name, not assume
    # a position.
    trace = [
        {"rule": "sales_velocity", "result": "pass", "inputs": {"sales_rank_drops_30d": 62, "minimum": 30}},
        {"rule": "seller_count_risk", "result": "info", "inputs": {}},
        {"rule": "margin_threshold", "result": "pass", "inputs": {"margin_pct": "18.5", "required_margin_pct": "10"}},
        {"rule": "roi_threshold", "result": "pass", "inputs": {"roi_pct": "34.0", "threshold": "25"}},
    ]
    assert por_que_es(trace) == (
        "ROI de 34.0% (mínimo 25.0%), margen de 18.5% (mínimo 10.0%), ventas estimadas de 62/mes (mínimo 30/mes)."
    )


def test_por_que_es_without_velocity_entry_at_all():
    trace = [
        {"rule": "roi_threshold", "result": "pass", "inputs": {"roi_pct": "34.0", "threshold": "25"}},
        {"rule": "margin_threshold", "result": "pass", "inputs": {"margin_pct": "18.5", "required_margin_pct": "10"}},
    ]
    assert por_que_es(trace) == "ROI de 34.0% (mínimo 25.0%), margen de 18.5% (mínimo 10.0%)."


def test_por_que_es_velocity_with_no_keepa_data_uses_plain_business_wording():
    trace = [
        {"rule": "roi_threshold", "result": "pass", "inputs": {"roi_pct": "34.0", "threshold": "25"}},
        {"rule": "margin_threshold", "result": "pass", "inputs": {"margin_pct": "18.5", "required_margin_pct": "10"}},
        {"rule": "sales_velocity", "result": "info", "inputs": {}},
    ]
    out = por_que_es(trace)
    assert "ventas estimadas sin datos de Keepa todavía" in out
    assert "eventos de mejora de ranking" not in out  # the old internal phrasing, never again


def test_riesgo_es_buy_box_exact_wording():
    trace = [{"rule": "amazon_has_buy_box", "result": "fail", "inputs": {}}]
    assert riesgo_es(trace) == ["Amazon vende este producto y tiene la Buy Box."]


def test_riesgo_es_seller_count_uses_real_number():
    trace = [{"rule": "seller_count_high_risk", "result": "fail", "inputs": {"seller_count": 14}}]
    assert riesgo_es(trace) == ["14 vendedores compitiendo por este producto."]


def test_riesgo_es_empty_list_when_no_risk_rule_present():
    trace = [{"rule": "roi_threshold", "result": "pass", "inputs": {"roi_pct": "34.0", "threshold": "25"}}]
    assert riesgo_es(trace) == []


def test_riesgo_es_finds_rule_by_name_not_position():
    trace = [
        {"rule": "sales_velocity", "result": "pass", "inputs": {"sales_rank_drops_30d": 10, "minimum": 30}},
        {"rule": "amazon_has_buy_box", "result": "fail", "inputs": {}},
        {"rule": "margin_threshold", "result": "pass", "inputs": {}},
    ]
    assert riesgo_es(trace) == ["Amazon vende este producto y tiene la Buy Box."]


def test_riesgo_es_no_velocity_data_adds_flag_even_without_a_high_risk_rule():
    # A BUY item (no amazon_has_buy_box/seller_count_high_risk present at
    # all) can still carry a data-quality risk line - decided 2026-10-11,
    # doesn't change what gets classified, only visibility.
    trace = [
        {"rule": "roi_threshold", "result": "pass", "inputs": {"roi_pct": "34.0", "threshold": "25"}},
        {"rule": "margin_threshold", "result": "pass", "inputs": {"margin_pct": "18.5", "required_margin_pct": "10"}},
        {"rule": "sales_velocity", "result": "info", "inputs": {}},
    ]
    assert riesgo_es(trace) == ["Sin datos de ventas (Keepa) para este producto."]


def test_riesgo_es_can_return_both_a_risk_line_and_a_data_quality_line():
    trace = [
        {"rule": "amazon_has_buy_box", "result": "fail", "inputs": {}},
        {"rule": "sales_velocity", "result": "info", "inputs": {}},
    ]
    assert riesgo_es(trace) == [
        "Amazon vende este producto y tiene la Buy Box.",
        "Sin datos de ventas (Keepa) para este producto.",
    ]
