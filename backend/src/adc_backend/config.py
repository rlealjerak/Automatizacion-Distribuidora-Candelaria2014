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
