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


class TelegramCommands:
    """Long-polls Telegram for commands sent from YOUR chat only and queues them.

    The trading loop picks commands off the queue between ticks, so a command never
    runs concurrently with order logic.
    """

    API = "https://api.telegram.org/bot{token}/getUpdates"

    def __init__(self, token: str, chat_id: str, get=None, poll_timeout: int = 25, hub=None):
        self.hub = hub  # when set, commands go to the shared CommandHub instead of the local queue
        self.url = self.API.format(token=token)
        self.chat_id, self.poll_timeout = str(chat_id), poll_timeout
        self._get = get or self._http_get
        self._offset: int | None = None
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._worker, name="telegram-commands", daemon=True)
        self._t.start()

    def _http_get(self, params: dict) -> dict:
        url = f"{self.url}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=params.get("timeout", 0) + 10) as r:
            return json.loads(r.read() or b"{}")

    def _worker(self) -> None:
        first = True  # skip commands sent while the bot was not running
        while not self._stop.is_set():
            try:
                params = {"timeout": 0 if first else self.poll_timeout, "allowed_updates": '["message"]'}
                if self._offset is not None:
                    params["offset"] = self._offset
                data = self._get(params)
                for upd in data.get("result", []):
                    self._offset = upd["update_id"] + 1
                    if first:
                        continue
                    msg = upd.get("message") or {}
                    if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                        log.warning("Ignoring Telegram message from unknown chat %s", (msg.get("chat") or {}).get("id"))
                        continue
                    text = (msg.get("text") or "").strip()
                    if text.startswith("/"):
                        name = text.split()[0].split("@")[0].lower()
                        if self.hub is not None:
                            self.hub.submit(name, "telegram")
                        else:
                            self._q.put(name)
                first = False
            except Exception as exc:
                log.warning("Telegram command poll failed: %s", exc)
                self._stop.wait(5)

    def get(self, timeout: float) -> str | None:
        try:
            return self._q.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None

    def close(self) -> None:
        self._stop.set()


def build_commands(cfg, hub=None):
    if cfg.telegram_token and cfg.telegram_chat_id and getattr(cfg, "telegram_commands", True):
        return TelegramCommands(cfg.telegram_token, cfg.telegram_chat_id, hub=hub)
    return None
