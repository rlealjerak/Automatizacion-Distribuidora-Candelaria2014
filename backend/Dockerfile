# Pinned exactly (not a floating "3.12" tag alone) so a rebuild months from
# now doesn't silently pick up a different patch release.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PYTHONPATH=/app/src

RUN pip install --no-cache-dir poetry==1.8.3

WORKDIR /app

COPY pyproject.toml poetry.lock* ./
RUN poetry install --no-root --only main

COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

EXPOSE 8000

# Runs migrations + seeds the default rules config (both idempotent, safe
# on every start - see entrypoint.sh) before starting the API. Worker mode
# is a separate entrypoint (worker.py), run as a separate ECS
# service/task, not through this CMD.
CMD ["./entrypoint.sh"]
