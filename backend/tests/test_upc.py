"""
UPC-A/EAN-13 check-digit validation - pure logic, no DB needed. The 20
real barcode values below are from the actual MTZ price list (not
invented), used elsewhere this session as the 20-row live test set.
"""

from __future__ import annotations

from adc_backend.modules.matching.upc import identifier_type_for_upc, validate_upc_ean_check_digit

REAL_MTZ_BARCODES = [
    "8411061088180", "3614271992932", "3614273636513", "3614274322293", "3614273650311",
    "737052189765", "3614227693876", "5045252668009", "3348900627499", "8411061026342",
    "8411061869376", "3614229135145", "3346131795635", "3423470317527", "737052074306",
    "3614229382112", "022548419953", "3386460126199", "7640496670146", "3661163904016",
]


def test_all_real_mtz_barcodes_validate():
    for code in REAL_MTZ_BARCODES:
        assert validate_upc_ean_check_digit(code), f"{code} should validate"


def test_identifier_type_by_length():
    assert identifier_type_for_upc("737052189765") == "UPC"  # 12 digits
    assert identifier_type_for_upc("8411061088180") == "EAN"  # 13 digits
    assert identifier_type_for_upc("12345") is None


def test_corrupted_check_digit_fails():
    # Last digit of a real valid EAN-13, flipped.
    valid = "8411061088180"
    corrupted = valid[:-1] + str((int(valid[-1]) + 1) % 10)
    assert validate_upc_ean_check_digit(corrupted) is False


def test_wrong_length_fails_even_if_all_digits():
    assert validate_upc_ean_check_digit("12345") is False
    assert validate_upc_ean_check_digit("1" * 14) is False


def test_non_digit_characters_fail():
    assert validate_upc_ean_check_digit("84110610881A0") is False


def test_none_or_empty_fails():
    assert validate_upc_ean_check_digit(None) is False
    assert validate_upc_ean_check_digit("") is False


def test_whitespace_is_tolerated():
    assert validate_upc_ean_check_digit("  8411061088180  ") is True


# --- normalize_upc (added 2026-11-14, Codex review on PR #14) ---

from adc_backend.modules.matching.upc import normalize_upc  # noqa: E402


def test_normalize_restores_stripped_leading_zero_on_real_mtz_upc():
    # 022548419953 is one of the real MTZ barcodes above; a numeric Excel
    # cell hands it over as 22548419953.
    assert normalize_upc("22548419953") == "022548419953"
    assert validate_upc_ean_check_digit(normalize_upc("22548419953"))


def test_normalize_handles_float_rendering_and_separators():
    assert normalize_upc("22548419953.0") == "022548419953"
    assert normalize_upc(" 0 22548 41995 3 ") == "022548419953"
    assert normalize_upc("8411061-088180") == "8411061088180"


def test_normalize_leaves_valid_codes_untouched():
    for code in REAL_MTZ_BARCODES:
        assert normalize_upc(code) == code


def test_normalize_never_pads_into_a_false_valid_barcode():
    # Flip the check digit of the real code - padding must not rescue it.
    assert not validate_upc_ean_check_digit(normalize_upc("22548419954"))
    assert normalize_upc("12345") == "12345"  # too short to be any barcode - returned as-is, fails validation later


def test_normalize_empty_and_non_numeric():
    assert normalize_upc(None) is None
    assert normalize_upc("   ") is None
    assert normalize_upc("N/A") == "N/A"
