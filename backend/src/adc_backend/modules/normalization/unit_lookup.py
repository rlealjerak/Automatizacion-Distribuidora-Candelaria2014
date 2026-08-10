"""
Case-unit lookup: seed data + application to a raw case-unit code.

Per CLAUDE.md, `DP` and `ST` (and others) are not standard units and must
not be guessed at - `multiplier_confirmed=False` on a row means "this
system knows the code exists but not what quantity it actually represents
for this supplier," and normalization must not silently assume one.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from adc_backend.modules.ingestion.models import UnitLookup

# (raw_unit, normalized_unit, units_per_case, multiplier_confirmed)
# Only genuinely unambiguous units get a confirmed multiplier out of the
# box. EA/PCS/DZ mean the same thing everywhere; CA/DP/ST/BX vary by
# supplier and must not be assumed - flagged unconfirmed until someone
# (the owner, or a future per-supplier override) actually confirms it.
DEFAULT_UNITS: list[tuple[str, str, float | None, bool]] = [
    ("EA", "EACH", 1, True),
    ("PCS", "PIECES", 1, True),
    ("PC", "PIECES", 1, True),
    ("DZ", "DOZEN", 12, True),
    ("CA", "CASE", None, False),
    ("CS", "CASE", None, False),
    ("BX", "BOX", None, False),
    ("DP", "DISPLAY_PACK", None, False),  # explicitly called out in CLAUDE.md as needing a lookup, not a guess
    ("ST", "SET", None, False),  # same
]


def seed_default_unit_lookup(db: Session) -> int:
    """Idempotent: only inserts units not already present. Returns count inserted."""
    existing = {row.raw_unit for row in db.execute(select(UnitLookup.raw_unit)).all()}
    inserted = 0
    for raw_unit, normalized_unit, units_per_case, confirmed in DEFAULT_UNITS:
        if raw_unit in existing:
            continue
        db.add(
            UnitLookup(
                raw_unit=raw_unit,
                normalized_unit=normalized_unit,
                units_per_case=units_per_case,
                multiplier_confirmed=confirmed,
            )
        )
        inserted += 1
    db.flush()
    return inserted


def resolve_unit(db: Session, raw_unit: str | None) -> UnitLookup | None:
    if not raw_unit:
        return None
    return db.execute(select(UnitLookup).where(UnitLookup.raw_unit == raw_unit.strip().upper())).scalar_one_or_none()
