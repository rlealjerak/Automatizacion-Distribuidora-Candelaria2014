"""
Aggregates every ORM model class so `Base.metadata` is fully populated.

Model classes live inside the module that owns them (modules/ingestion,
modules/matching, ...) so each module stays self-contained. This module
exists purely so Alembic's env.py and anything doing
`Base.metadata.create_all()` can `import adc_backend.db.models` once and
get the whole schema - individual modules never need to import each
other's model files directly.
"""

from __future__ import annotations

from adc_backend.db.base import Base
from adc_backend.db.core_models import ListRun, Supplier
from adc_backend.modules.amazon.models import AmazonDataSnapshot
from adc_backend.modules.ingestion.models import RawLineItem, UnitLookup
from adc_backend.modules.matching.models import ProductMatch
from adc_backend.modules.review.models import ManualReviewQueue
from adc_backend.modules.rules.models import BusinessRulesConfig, ClassificationResult

__all__ = [
    "AmazonDataSnapshot",
    "Base",
    "BusinessRulesConfig",
    "ClassificationResult",
    "ListRun",
    "ManualReviewQueue",
    "ProductMatch",
    "RawLineItem",
    "Supplier",
    "UnitLookup",
]
