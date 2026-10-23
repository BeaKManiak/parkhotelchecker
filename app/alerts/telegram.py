"""Telegram delivery: plain text, no parse mode.

Plain text is a deliberate choice. Markdown/HTML parse modes turn a hotel name
with an apostrophe or an ampersand into a 400, and "the alert failed because of
punctuation" is an absurd way to lose a price drop. Hotel names are ours, but
the principle inside a notifier is: never let formatting be able to reject a
message.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 4.0)
TELEGRAM_LIMIT = 4096


@dataclass(frozen=True, slots=True)
class Delivery:
    """One chat's outcome. ``detail`` is Telegram's own description."""

    chat_id: str
    ok: bool
    status: int | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class Settings:
    token: str
    chats: tuple[str, ...]

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chats)

    @property
    def missing(self) -> tuple[str, ...]:
        missing = []
        if not self.token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.chats:
            missing.append("TELEGRAM_CHAT_IDS")
        return tuple(missing)


def mask_chat(chat_id: str) -> str:
    """Enough to recognise the configured chat, not enough to leak it."""
    cleaned = chat_id.strip()
    return f"…{cleaned[-4:]}" if len(cleaned) > 4 else "…"


def settings_from_env() -> Settings:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    raw_chats = os.environ.get("TELEGRAM_CHAT_IDS", "")
    chats = tuple(chat.strip() for chat in raw_chats.split(",") if chat.strip())
    return Settings(token=token, chats=chats)


class Telegram:
    def __init__(self, token: str, chat_ids: list[str], client: httpx.AsyncClient) -> None:
        self._token = token
        self._chats = chat_ids
        self._client = client

    @property
    def url(self) -> str:
        return f"https://api.telegram.org/bot{self._token}/sendMessage"

    async def send(self, text: str) -> bool:
        """True only if every configured chat accepted the message."""
        deliveries = await self.deliver(text)
        return bool(deliveries) and all(delivery.ok for delivery in deliveries)

    async def deliver(self, text: str) -> list[Delivery]:
        """Send to every chat, returning each outcome instead of only a bool."""
        # Telegram rejects anything longer than 4096 characters. Truncating is
        # better than dropping the alert; the digest already caps itself at 12
        # lines, so this is a seatbelt, not the mechanism.
        text = text[: TELEGRAM_LIMIT - 20]
        return [await self._send_one(chat_id, text) for chat_id in self._chats]

    async def _send_one(self, chat_id: str, text: str) -> Delivery:
        last = Delivery(chat_id, False, None, "no attempt")
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._client.post(
                    self.url,
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                )
                detail = _description(response)
                if response.status_code == 200 and _ok(response):
                    return Delivery(chat_id, True, response.status_code)
                last = Delivery(chat_id, False, response.status_code, detail)
                log.warning(
                    "telegram rejected message for chat %s: HTTP %s %s",
                    chat_id,
                    response.status_code,
                    detail,
                )
                # A 4xx (bad token, user blocked the bot) will not improve by
                # retrying. Only 5xx and network errors are worth a second try.
                if 400 <= response.status_code < 500:
                    return last
            except (httpx.HTTPError, ValueError) as exc:
                last = Delivery(chat_id, False, None, str(exc))
                log.warning("telegram attempt %d/%d failed: %s", attempt, MAX_ATTEMPTS, exc)
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(BACKOFF_SECONDS[attempt - 1])
        return last


def _ok(response: httpx.Response) -> bool:
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and bool(payload.get("ok"))


def _description(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("description")
        if detail:
            return str(detail)[:300]
    return (response.text or f"HTTP {response.status_code}")[:300]


def from_env(client: httpx.AsyncClient) -> Telegram | None:
    settings = settings_from_env()
    if not settings.configured:
        return None
    return Telegram(settings.token, list(settings.chats), client)
