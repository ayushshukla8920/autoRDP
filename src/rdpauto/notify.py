"""Telegram alerts, for when a run cannot recover on its own.

One use only: after every reconnect attempt has failed, send a message so you
find out the remote session died without watching the log. It is best-effort --
a failed alert must never become a second failure on top of the first -- so
every error here is swallowed and logged, not raised.

Set ``RDP_TELEGRAM_TOKEN`` and ``RDP_TELEGRAM_CHAT_ID`` (in ``.env`` or the
environment). Get the token from @BotFather; get the chat id by messaging your
bot once and reading ``result[0].message.chat.id`` from
``https://api.telegram.org/bot<token>/getUpdates``.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("rdpauto.notify")

_API = "https://api.telegram.org/bot{token}/sendMessage"


def configured(settings) -> bool:
    return bool(settings.telegram_token and settings.telegram_chat_id)


def send(settings, message: str, timeout: float = 10.0) -> bool:
    """Post ``message`` to the configured Telegram chat. Never raises."""
    if not configured(settings):
        logger.debug("Telegram not configured; skipping alert")
        return False

    data = urllib.parse.urlencode({
        "chat_id": settings.telegram_chat_id,
        "text": message,
    }).encode("utf-8")
    url = _API.format(token=settings.telegram_token)
    try:
        with urllib.request.urlopen(url, data=data, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not body.get("ok"):
            logger.warning("Telegram rejected the alert: %s", body.get("description"))
            return False
        logger.info("Telegram alert sent")
        return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("Could not send Telegram alert: %s", exc)
        return False


if __name__ == "__main__":  # tiny self-check: build the request without sending
    class _S:
        telegram_token = "T"
        telegram_chat_id = "C"
    assert configured(_S())
    _S.telegram_token = ""
    assert not configured(_S())
    print("notify self-check ok")
