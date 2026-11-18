"""Alerts. Telegram is used when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set.

Messages are sent from a background thread so a slow network never delays
an order. Repeated messages with the same `key` (e.g. the same error every
poll) are suppressed for `dedupe_seconds`.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)


class Notifier:
    """No-op notifier (used when Telegram is not configured)."""

    def send(self, text: str, key: str | None = None) -> None:
        pass

    def close(self, timeout: float = 5.0) -> None:
        pass


class TelegramNotifier(Notifier):
    API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str, chat_id: str, prefix: str = "", dedupe_seconds: float = 300.0,
                 post=None):
        self.url = self.API.format(token=token)
        self.chat_id, self.prefix, self.dedupe = chat_id, prefix, dedupe_seconds
        self._post = post or self._http_post
        self._seen: dict[str, float] = {}
        self._q: queue.Queue = queue.Queue()
        self._t = threading.Thread(target=self._worker, name="telegram", daemon=True)
        self._t.start()

    def send(self, text: str, key: str | None = None) -> None:
        if key:
            now = time.monotonic()
            if now - self._seen.get(key, -1e9) < self.dedupe:
                return
            self._seen[key] = now
        self._q.put(f"{self.prefix}{text}")

    def close(self, timeout: float = 5.0) -> None:
        self._q.put(None)
        self._t.join(timeout)

    def _http_post(self, payload: dict) -> None:
        data = urllib.parse.urlencode(payload).encode()
        with urllib.request.urlopen(urllib.request.Request(self.url, data=data), timeout=10) as r:
            body = json.loads(r.read() or b"{}")
            if not body.get("ok", False):
                raise RuntimeError(body)

    def _worker(self) -> None:
        while True:
            text = self._q.get()
            if text is None:
                return
            try:
                self._post({"chat_id": self.chat_id, "text": text, "disable_web_page_preview": "true"})
            except Exception as exc:  # never let alerts break trading
                log.warning("Telegram send failed: %s", exc)


def build_notifier(cfg) -> Notifier:
    if cfg.telegram_token and cfg.telegram_chat_id:
        log.info("Telegram alerts enabled")
        return TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id,
                                prefix="[LIVE] " if cfg.is_live else "[PAPER] ")
    return Notifier()
