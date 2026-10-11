"""
UPC-A / EAN-13 check-digit validation.

Added 2026-10-11: the real MTZ price list (a fragrance distributor) has a
real UPC column with real barcodes, disproving CLAUDE.md's original "no
supplier ever provides UPC/EAN" assumption. Before trusting a barcode
enough to call SP-API's identifier lookup with it, validate the GS1 check
digit - not just the digit count - per explicit instruction ("check the
digit"). A string of the right length that isn't a real barcode (typos,
truncated values, a non-barcode number that happened to land in the UPC
column) must fall back to text/brand matching instead of wasting an
SP-API call or trusting a bogus identifier lookup.
"""

from __future__ import annotations

# UPC-A is 12 digits, EAN-13 is 13 - the two formats actually seen in the
# real MTZ file (European 13-digit EAN for France/Spain-sourced goods,
# 12-digit UPC-A for some US-style ones).
UPC_A_LENGTH = 12
EAN_13_LENGTH = 13


def identifier_type_for_upc(code: str) -> str | None:
    """SP-API's identifiersType value for a validated code, or None if not a recognized length."""
    length = len(code)
    if length == UPC_A_LENGTH:
        return "UPC"
    if length == EAN_13_LENGTH:
        return "EAN"
    return None


def validate_upc_ean_check_digit(code: str | None) -> bool:
    """
    Real GS1 check-digit validation, not a digit-count check. Works
    identically for UPC-A (12 digits) and EAN-13 (13 digits): the GS1
    algorithm's weights (3/1, alternating) are defined relative to the
    check digit's position from the right, not the barcode's absolute
    length, so the same loop is correct for both.
    """
    if not code:
        return False
    code = code.strip()
    if not code.isdigit():
        return False
    if len(code) not in (UPC_A_LENGTH, EAN_13_LENGTH):
        return False

    digits = [int(c) for c in code]
    payload, check_digit = digits[:-1], digits[-1]

    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(payload)))
    expected_check_digit = (10 - (total % 10)) % 10
    return expected_check_digit == check_digit
