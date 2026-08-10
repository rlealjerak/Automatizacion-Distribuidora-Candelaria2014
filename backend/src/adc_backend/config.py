"""
Application configuration.

Non-secret settings (region, resource names/URLs, environment name) come
from environment variables — set them via ECS task definition environment
in prod, or a local `.env` file in dev (see `.env.example`).

Actual secret VALUES (SP-API credentials, Keepa API key, DB password) are
never read from environment variables. They live in AWS Secrets Manager
and are fetched at runtime via `get_secret()`, which relies on the ECS
task role's IAM permissions (or a local AWS CLI profile in dev). This is
the one rule in this module that must never be relaxed: no secret value
is ever hardcoded, put in a config file, or logged.
"""

from __future__ import annotations

import json
from functools import lru_cache
from urllib.parse import quote_plus

import boto3
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "dev"
    aws_region: str = "us-east-1"

    # Resource identifiers - non-secret, safe to set as plain env vars.
    s3_bucket_name: str = ""
    sqs_queue_url: str = ""

    # Secrets Manager *names* (not values) for the credentials this app needs.
    sp_api_secret_name: str = ""
    keepa_secret_name: str = ""
    db_secret_name: str = ""  # RDS-managed master user secret

    # Non-secret DB connection info; password comes from db_secret_name at runtime.
    db_host: str = ""
    db_port: int = 5432
    db_name: str = "adc"
    db_username: str = "adc_admin"

    # Local dev / CI escape hatch only: a full connection URL, used as-is if
    # set. Lets local work (Docker Postgres, no AWS creds needed) happen
    # without faking a Secrets Manager secret. Must stay unset in any real
    # environment - if it's set, db_secret_name below is never consulted.
    database_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def _secrets_client():
    return boto3.client("secretsmanager", region_name=get_settings().aws_region)


def get_secret(secret_name: str) -> dict:
    """
    Fetch and parse a JSON secret from Secrets Manager by name or ARN.

    Not cached across calls by design — credentials can rotate (the RDS
    master password does, automatically). Callers that need the value
    repeatedly should cache it themselves for the lifetime that makes
    sense for their use case, not rely on this function to do it silently.
    """
    if not secret_name:
        raise ValueError("secret_name must be set (check environment configuration)")
    response = _secrets_client().get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


def get_database_url() -> str:
    """
    Build the SQLAlchemy connection URL.

    Local dev / CI: set DATABASE_URL directly (see .env.example) and this
    returns it unchanged - no AWS credentials needed.

    Everywhere else: db_host/port/name/username come from plain settings,
    and the password is fetched fresh from Secrets Manager on every call
    (RDS rotates it automatically - never cache this beyond one call's
    lifetime). The RDS-managed master user secret has a `password` key
    alongside `username`, `host`, etc.
    """
    settings = get_settings()
    if settings.database_url:
        return settings.database_url

    password = get_secret(settings.db_secret_name)["password"]
    return (
        f"postgresql+psycopg2://{quote_plus(settings.db_username)}:{quote_plus(password)}"
        f"@{settings.db_host}:{settings.db_port}/{settings.db_name}"
    )
