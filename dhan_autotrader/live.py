"""Thread-safe bridge between the trading loop and outside controllers (dashboard, Telegram).

* The trading loop PUBLISHES a snapshot after every tick; readers never touch bot objects.
* Controllers SUBMIT commands to the CommandHub; the trading loop executes them between
  ticks (so a command never races with order logic) and resolves each with a reply.
"""
from __future__ import annotations

import copy
import queue
import threading
from datetime import datetime


class Command:
    def __init__(self, name: str, source: str):
        self.name, self.source = name, source
        self.reply: str | None = None
        self._done = threading.Event()

    def resolve(self, reply: str) -> None:
        self.reply = reply
        self._done.set()

    def wait(self, timeout: float) -> str | None:
        return self.reply if self._done.wait(timeout) else None


class CommandHub:
    def __init__(self):
        self._q: queue.Queue[Command] = queue.Queue()

    def submit(self, name: str, source: str) -> Command:
        cmd = Command(name, source)
        self._q.put(cmd)
        return cmd

    def get(self, timeout: float) -> Command | None:
        try:
            return self._q.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None

    def drain(self, reply: str) -> None:
        """Answer anything left in the queue (e.g. when a session ends)."""
        while True:
            try:
                self._q.get_nowait().resolve(reply)
            except queue.Empty:
                return

    def close(self) -> None:  # run_session calls close() on its command source
        self.drain("Session ended")


class LiveRegistry:
    """Holds the latest published snapshot of the running bot (or nothing)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._snapshot: dict | None = None
        self.running = False
        self.hub = CommandHub()

    def attach(self) -> None:
        with self._lock:
            self.running = True

    def detach(self) -> None:
        with self._lock:
            self.running = False
            if self._snapshot:
                self._snapshot = {**self._snapshot, "running": False}
        self.hub.drain("Bot is not running")

    def publish(self, snapshot: dict) -> None:
        snap = {**snapshot, "running": self.running, "published": datetime.now().isoformat(timespec="seconds")}
        with self._lock:
            self._snapshot = snap

    def snapshot(self) -> dict | None:
        with self._lock:
            return copy.deepcopy(self._snapshot)


LIVE = LiveRegistry()
