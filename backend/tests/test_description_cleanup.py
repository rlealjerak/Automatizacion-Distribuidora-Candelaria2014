"""
Description cleanup for fuzzy matching - pure logic, no DB needed. The 5
rows below are real MTZ price list rows (not invented), used as the
before/after example this session.
"""

from __future__ import annotations

from adc_backend.modules.matching.description_cleanup import clean_description_for_matching

REAL_MTZ_BEFORE_AFTER = [
    ("212 3.4 EDT L  (100010) - Spain - 20pcs ByBox", "212 3.4 EDT L"),
    ("ACQUA DI GIO ABSOLU 4.2 EDP M  (111675) - France - 21pcs ByBox", "ACQUA DI GIO ABSOLU 4.2 EDP M"),
    ("ARMANI CODE 4.2 EDT M REFILLABLE (100325) - France - 20pcs ByBox", "ARMANI CODE 4.2 EDT M REFILLABLE"),
    ("BOSS # 6 6.7 EDT M  (100532) - Spain - 12pcs ByBox", "BOSS # 6 6.7 EDT M"),
    ("NAUTICA 3.4 EDT M  (102963) - Spain - 6pcs ByBox", "NAUTICA 3.4 EDT M"),
]


def test_strips_trailing_item_number_country_and_case_pack_on_real_rows():
    for before, after in REAL_MTZ_BEFORE_AFTER:
        assert clean_description_for_matching(before) == after


def test_leaves_description_with_no_trailing_noise_unchanged():
    assert clean_description_for_matching("Blue Widget 10-Pack") == "Blue Widget 10-Pack"


def test_parenthesized_number_mid_string_is_not_touched():
    # Only a trailing "(N) - Country - Npcs" pattern is noise - a
    # parenthesized number elsewhere in a real product name must survive.
    desc = "Vitamin C (1000mg) Serum - 30ml"
    assert clean_description_for_matching(desc) == desc


def test_empty_description_returns_empty():
    assert clean_description_for_matching("") == ""


def test_none_safe():
    assert clean_description_for_matching(None) is None
