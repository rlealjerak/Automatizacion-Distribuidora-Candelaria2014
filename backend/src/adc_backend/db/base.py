"""
Shared SQLAlchemy infrastructure: declarative base, engine, session factory.

Individual module packages (modules/ingestion, modules/matching, ...) define
their own ORM model classes against `Base` below - this file owns no
business tables itself except via db/models.py re-exporting them all so
Alembic autogenerate and `Base.metadata.create_all()` see the full schema.
"""

from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from adc_backend.config import get_database_url

# Explicit naming convention so Alembic autogenerate produces stable,
# predictable constraint/index names instead of DB-assigned defaults that
# differ between environments and make diffs noisy.
#
# fk deliberately omits %(referred_table_name)s: table_name + column_0_name
# is already unique per constraint, and two of this schema's real FK names
# exceed Postgres's 63-byte identifier limit with the referred table name
# included (silently truncated otherwise - not acceptable for a system
# this rule engine's audit trail depends on).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


@lru_cache
def get_engine():
    return create_engine(get_database_url(), pool_pre_ping=True, future=True)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: yields one Session per request, always closed after."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()
