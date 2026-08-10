"""
Default business rules configuration - the structured, DB-backed,
owner-editable form of the rules table in CLAUDE.md. Every number here is
a starting default, not a hardcoded constant the rule engine (engine.py)
assumes - engine.py always takes a config dict as input and never
hardcodes a threshold itself. Changing a rule means inserting a new
`business_rules_configs` row and flipping `is_active` (see
db schema step 2), never a code change.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from adc_backend.modules.rules.models import BusinessRulesConfig

DEFAULT_RULES_CONFIG: dict = {
    # ROI >= this is a clean pass. Between the floor and this is "Negotiate"
    # - CLAUDE.md: "first-round supplier prices are typically inflated."
    "roi_threshold_pct": 20.0,
    "roi_negotiate_floor_pct": 10.0,
    # Margin needs a price-dependent minimum, not one flat number - lower-
    # priced items need a higher margin % since fixed fees eat more of it
    # proportionally. Table is ordered ascending by max_price; first row
    # where price <= max_price wins. A null max_price is the catch-all.
    "margin_exception_table": [
        {"max_price": 15.0, "min_margin_pct": 15.0},
        {"max_price": 50.0, "min_margin_pct": 10.0},
        {"max_price": None, "min_margin_pct": 8.0},
    ],
    # Velocity proxy, NOT units/month (Keepa doesn't report that - see
    # keepa_client.py). salesRankDrops30 is Keepa's own documented signal
    # for "how often this ASIN's rank improved in the last 30 days," which
    # correlates with sales frequency without being a literal unit count.
    "sales_rank_drops_30d_min": 40,
    # Seller count is a risk-scoring input, not a cutoff (CLAUDE.md is
    # explicit about this one).
    "seller_count_low_risk_max": 3,
    "seller_count_medium_risk_max": 8,
    # How much stronger than the normal ROI/margin threshold a gated-but-
    # not-yet-approved item's numbers need to be before it's worth
    # surfacing at all (CLAUDE.md: "surface only if ROI/margin is notably
    # strong"). 1.5x is a starting multiplier, not a claimed-precise value.
    "gated_pending_strong_deal_multiplier": 1.5,
}


def seed_default_rules_config(db: Session) -> BusinessRulesConfig | None:
    """Idempotent: does nothing if any active config already exists. Returns the created row, or None."""
    existing = db.execute(select(BusinessRulesConfig).where(BusinessRulesConfig.is_active.is_(True))).scalar_one_or_none()
    if existing is not None:
        return None
    config = BusinessRulesConfig(name="default", config=DEFAULT_RULES_CONFIG, is_active=True)
    db.add(config)
    db.flush()
    return config
