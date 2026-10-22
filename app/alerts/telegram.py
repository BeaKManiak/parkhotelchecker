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

import httpx

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 4.0)
TELEGRAM_LIMIT = 4096


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
        # Telegram rejects anything longer than 4096 characters. Truncating is
        # better than dropping the alert; the digest already caps itself at 12
        # lines, so this is a seatbelt, not the mechanism.
        text = text[: TELEGRAM_LIMIT - 20]

        delivered = True
        for chat_id in self._chats:
            delivered = await self._send_one(chat_id, text) and delivered
        return delivered

    async def _send_one(self, chat_id: str, text: str) -> bool:
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
                if response.status_code == 200 and response.json().get("ok"):
                    return True
                log.warning(
                    "telegram rejected message for chat %s: HTTP %s %s",
                    chat_id,
                    response.status_code,
                    response.text[:200],
                )
                # A 4xx (bad token, user blocked the bot) will not improve by
                # retrying. Only 5xx and network errors are worth a second try.
                if 400 <= response.status_code < 500:
                    return False
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("telegram attempt %d/%d failed: %s", attempt, MAX_ATTEMPTS, exc)
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(BACKOFF_SECONDS[attempt - 1])
        return False


def from_env(client: httpx.AsyncClient) -> Telegram | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    raw_chats = os.environ.get("TELEGRAM_CHAT_IDS", "")
    chats = [chat.strip() for chat in raw_chats.split(",") if chat.strip()]
    if not token or not chats:
        return None
    return Telegram(token, chats, client)
