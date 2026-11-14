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

Deployed as its own ECS Fargate service (`adc-prod-worker`, no load
balancer attachment - it doesn't receive inbound traffic), sharing the
API's image with `command` overridden at the task-definition level to
run this module instead of `entrypoint.sh` (see infra/modules/ecs_cluster).
Runnable locally against the real SQS queue too, for manual testing.
"""

from __future__ import annotations

import json
import logging
import uuid

from adc_backend.config import get_settings, get_sqs_client
from adc_backend.db.base import get_sessionmaker
from adc_backend.modules.amazon.keepa_client import KeepaClient
from adc_backend.modules.amazon.sp_api_client import SPAPIClient
from adc_backend.modules.tools.orchestration import OrchestrationError, process_run, process_single_item

logging.basicConfig(level=logging.INFO)
# See main.py's identical line for why: httpx logs the full request URL at
# INFO, and Keepa's API key travels as a URL query param - left unsuppressed
# this writes the real secret into CloudWatch logs on every Keepa call. This
# is the process that actually caught it live (2026-08-20).
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Rows between commits/heartbeats (2026-11-14). Small on purpose: at real
# rate limits (SP-API pricing ~0.5 req/s, Keepa 1 token/min on this
# account) 10 rows can still take several minutes, and every checkpoint
# also re-arms the SQS visibility timeout below.
CHECKPOINT_EVERY_ROWS = 10
# Matches the queue's own visibility_timeout_seconds (infra/modules/sqs) -
# each checkpoint pushes redelivery this far out again, so a multi-hour
# run is never handed to a second receive while it's still progressing.
VISIBILITY_EXTENSION_SECONDS = 1800


def main() -> None:
    settings = get_settings()
    if not settings.sqs_queue_url:
        raise RuntimeError("SQS_QUEUE_URL is not set - can't start the worker")

    sqs = get_sqs_client()
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

    raw_line_item_id = None
    if body.get("raw_line_item_id"):
        try:
            raw_line_item_id = uuid.UUID(body["raw_line_item_id"])
        except (ValueError, TypeError) as e:
            logger.error("Malformed raw_line_item_id, leaving on queue for SQS's redrive policy to handle: %s", e)
            return

    def _heartbeat(_run) -> None:
        # Best-effort: a failed extension only risks a redelivery, which
        # the run's own checkpoint/resume logic already handles safely.
        try:
            sqs.change_message_visibility(
                QueueUrl=queue_url, ReceiptHandle=receipt_handle, VisibilityTimeout=VISIBILITY_EXTENSION_SECONDS
            )
        except Exception:  # noqa: BLE001
            logger.warning("Couldn't extend SQS visibility for run %s", run_id, exc_info=True)

    session = get_sessionmaker()()
    try:
        if raw_line_item_id is not None:
            summary = process_single_item(session, run_id, raw_line_item_id, SPAPIClient(), KeepaClient())
        else:
            summary = process_run(
                session,
                run_id,
                SPAPIClient(),
                KeepaClient(),
                checkpoint_every=CHECKPOINT_EVERY_ROWS,
                on_checkpoint=_heartbeat,
            )
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
