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


def normalize_upc(raw: str | None) -> str | None:
    """
    Recovers a barcode from the forms spreadsheet tooling commonly mangles
    it into, before check-digit validation sees it (Codex review on PR #14):
    a float rendering (`22548419953.0`), separators (`0 22548 41995 3`),
    and - the important one - a stripped leading zero (11 digits for a
    12-digit UPC-A, 12 for a 13-digit EAN). A padded candidate is only
    accepted if it then passes the real GS1 check digit, so padding never
    turns an arbitrary short number into a "valid" barcode by accident.
    Anything unrecoverable is returned stripped but otherwise unchanged -
    validation downstream rejects it and matching falls back to text.
    """
    if raw is None:
        return None
    code = raw.strip()
    if not code:
        return None
    if code.endswith(".0") and code[:-2].isdigit():
        code = code[:-2]
    compact = code.replace(" ", "").replace("-", "")
    if not compact.isdigit():
        return code
    if len(compact) in (UPC_A_LENGTH, EAN_13_LENGTH) and validate_upc_ean_check_digit(compact):
        return compact
    for target_length in (UPC_A_LENGTH, EAN_13_LENGTH):
        if len(compact) < target_length:
            padded = compact.zfill(target_length)
            if validate_upc_ean_check_digit(padded):
                return padded
    return compact


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
