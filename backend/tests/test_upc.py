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
