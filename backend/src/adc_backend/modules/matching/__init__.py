"""
Matching engine (build-order step 7).

Owns: text/brand/description matching against the Amazon catalog (no
UPC/EAN is ever available from suppliers), confidence scoring, and the
persistent `supplier_id + supplier_item_number -> ASIN` mapping table so
future lists from the same supplier match instantly at high confidence
once the owner has confirmed a match once.

Highest-risk module in the system alongside the rule engine - needs the
most test coverage (build-order step 12).

Not yet implemented.
"""
