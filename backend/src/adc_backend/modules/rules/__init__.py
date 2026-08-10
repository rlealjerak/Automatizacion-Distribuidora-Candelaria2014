"""
Rule engine and classification (build-order step 8).

Owns: all financial math (ROI, margin, price-dependent exception tables),
sales velocity/seasonality evaluation, restricted/gated/manufacturer-sells/
buy-box/seller-count rules, and producing the final classification (Buy /
Review / Negotiate / No-buy / High-risk) with a full reasoning/rule trace.

Rules are configurable data (structured config, DB-backed), not hardcoded
constants - a non-technical owner must be able to change thresholds
without a code change.

Highest-risk module in the system alongside matching - needs the most
test coverage (build-order step 12).

Not yet implemented.
"""
