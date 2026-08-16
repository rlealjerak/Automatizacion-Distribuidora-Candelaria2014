#!/bin/sh
# Container entrypoint: apply migrations, seed the default rules config if
# none is active yet, then start the API. Both steps are idempotent
# (alembic upgrade head is a no-op if already current; seed_default_rules_
# config checks for an existing active config first) - safe to run on
# every single container start, including every deploy and every task
# restart, not just "the first time". This is what makes a fresh RDS
# database ready without a separate manual bootstrap step.
set -e

echo "entrypoint: running migrations..."
alembic upgrade head

echo "entrypoint: ensuring a default rules config exists..."
python -c "
from adc_backend.db import models  # noqa: F401 - register all mapped classes
from adc_backend.db.base import get_sessionmaker
from adc_backend.modules.rules.config import seed_default_rules_config

db = get_sessionmaker()()
result = seed_default_rules_config(db)
db.commit()
print('seeded a new default config' if result else 'active config already exists - no-op')
"

echo "entrypoint: starting API..."
exec uvicorn adc_backend.main:app --host 0.0.0.0 --port 8000
