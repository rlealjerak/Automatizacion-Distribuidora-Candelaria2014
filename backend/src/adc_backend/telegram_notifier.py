"""
Direct Telegram Bot API client - used only by reminder_job.py for the
periodic approval-reminder ping.

This is a deliberate, explicit exception to "OpenClaw owns Telegram
conversation" (CLAUDE.md): OpenClaw's finalized deployment plan
(docs/openclaw/OPENCLAW_DEPLOYMENT_PLAN.md Section 4) confirms it takes
*no* inbound traffic at all (loopback bind, no ALB) - there is no network
path for this backend to ask OpenClaw to send a reminder. Everything else
about the approval conversation (presenting items, capturing
approve/deny, relaying to the backend via MCP) still flows through
OpenClaw untouched; this is only the timed nudge when nothing else is
watching the clock, since worker.py's SQS loop is purely event-driven
and can't do that either (see docs/openclaw/OPENCLAW_TECHNICAL_SPEC.md
Section 5).

Bot token + chat id come from Secrets Manager
(telegram_reminder_secret_name), same shape as every other credential in
this project - never hardcoded, never logged.
"""

from __future__ import annotations

import httpx

from adc_backend.config import get_secret, get_settings


class TelegramNotifierError(Exception):
    pass


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id

    @classmethod
    def from_settings(cls) -> TelegramNotifier:
        settings = get_settings()
        if not settings.telegram_reminder_secret_name:
            raise TelegramNotifierError("telegram_reminder_secret_name is not set - can't send reminders")
        secret = get_secret(settings.telegram_reminder_secret_name)
        return cls(bot_token=secret["bot_token"], chat_id=secret["chat_id"])

    def send_reminder(self, text: str) -> None:
        """
        Raises TelegramNotifierError on any failure - callers must not
        advance reminder state (reminder_count/last_reminder_at) unless
        the message actually sent, or the client silently misses a
        reminder while its clock keeps ticking toward `holding`.
        """
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        try:
            response = httpx.post(url, json={"chat_id": self._chat_id, "text": text}, timeout=10.0)
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise TelegramNotifierError(f"Telegram sendMessage failed: {e}") from e
