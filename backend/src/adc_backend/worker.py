"""
SQS worker entrypoint (background job processor).

Long-polls the list-processing queue for `{"run_id": "..."}` messages (one
message per run, per infra's SQS design - see
docs/decisions/0001-initial-architecture-decisions.md) and runs
`process_run` for each. Per-row error isolation happens inside
`process_run` itself (CLAUDE.md acceptance criteria: one bad row must not
fail the whole run) - what this file adds is run-level message handling:
a malformed message or a run-level failure (no confirmed mapping, no
active rules config) is left on the queue rather than deleted, so SQS's
own redrive policy (maxReceiveCount, then the DLQ - see infra/modules/sqs)
handles retry/eventual quarantine instead of this process reinventing it.

Not yet deployed - no ECS task definition/service exists for this yet
(see infra/modules/ecs_cluster's docstring). Runnable locally against the
real SQS queue right now, though (`adc-prod-list-processing`, live per
docs/decisions/0003-infra-apply-findings.md), for manual testing.
"""

from __future__ import annotations

import json
import logging
import uuid

import boto3

from adc_backend.config import get_settings
from adc_backend.db.base import get_sessionmaker
from adc_backend.modules.amazon.keepa_client import KeepaClient
from adc_backend.modules.amazon.sp_api_client import SPAPIClient
from adc_backend.modules.tools.orchestration import OrchestrationError, process_run

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    if not settings.sqs_queue_url:
        raise RuntimeError("SQS_QUEUE_URL is not set - can't start the worker")

    sqs = boto3.client("sqs", region_name=settings.aws_region)
    logger.info("adc-backend worker starting | queue=%s", settings.sqs_queue_url)

    while True:
        response = sqs.receive_message(
            QueueUrl=settings.sqs_queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,  # long poll - avoids busy-looping SQS ReceiveMessage calls
        )
        for message in response.get("Messages", []):
            _handle_message(sqs, settings.sqs_queue_url, message)


def _handle_message(sqs, queue_url: str, message: dict) -> None:
    receipt_handle = message["ReceiptHandle"]

    try:
        body = json.loads(message["Body"])
        run_id = uuid.UUID(body["run_id"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
        logger.error("Malformed message, leaving on queue for SQS's redrive policy to handle: %s", e)
        return  # deliberately not deleted

    session = get_sessionmaker()()
    try:
        summary = process_run(session, run_id, SPAPIClient(), KeepaClient())
        session.commit()
        logger.info("Processed run %s: %s", run_id, summary)
        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
    except OrchestrationError as e:
        session.rollback()
        logger.error("Run-level failure for %s, leaving on queue for retry/DLQ: %s", run_id, e)
    except Exception:
        session.rollback()
        logger.exception("Unexpected failure processing run %s, leaving on queue for retry/DLQ", run_id)
    finally:
        session.close()


if __name__ == "__main__":
    main()
