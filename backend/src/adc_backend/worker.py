"""
SQS worker entrypoint (background job processor).

Deliberately a stub. Real job logic depends on:
  - the DB schema for list runs / raw line items (build-order step 2)
  - ingestion (step 3) having produced rows to process
  - matching (step 7) and the rule engine (step 8) to actually do anything

This file exists now so the deployment shape (a second ECS
service/task running this instead of the API) is decided early, without
pretending the processing logic is implemented.

Expected shape once real: long-poll SQS for `{"run_id": ...}` messages,
load that run's raw line items from Postgres, process each with per-row
error isolation (one bad row must not fail the run), write results,
delete the message on success, let SQS redrive to the DLQ on repeated
failure per the queue's maxReceiveCount.
"""

from __future__ import annotations

import logging

from adc_backend.config import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    logger.info(
        "adc-backend worker starting (stub - no job logic yet) | queue=%s",
        settings.sqs_queue_url or "(unset)",
    )
    raise NotImplementedError(
        "Worker job processing isn't implemented yet - lands with build-order steps 2-9."
    )


if __name__ == "__main__":
    main()
