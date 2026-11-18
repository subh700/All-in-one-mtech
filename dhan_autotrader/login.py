"""Automatic daily Dhan login with PIN + TOTP.

Set in .env:
    DHAN_PIN=1234
    DHAN_TOTP_SECRET=JBSWY3DPEHPK3PXP      # the secret shown when you enable TOTP on Dhan
and leave DHAN_ACCESS_TOKEN empty. At start-up the bot generates a fresh access
token and caches it in state/token.json for the rest of the day.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


def totp(secret: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 time-based one-time password (same as Google Authenticator)."""
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def _extract_token(resp) -> str:
    if isinstance(resp, dict):
        for k in ("accessToken", "access_token", "token"):
            if resp.get(k):
                return str(resp[k])
        if isinstance(resp.get("data"), dict):
            return _extract_token(resp["data"])
    raise RuntimeError(f"No access token in Dhan login response: {resp}")


def get_access_token(client_id: str, pin: str, totp_secret: str, cache_path: str = "state/token.json",
                     login=None, now: datetime | None = None) -> str:
    """Return today's cached token, or log in with PIN + TOTP and cache the new one."""
    today = (now or datetime.now(IST)).date().isoformat()
    try:
        with open(cache_path) as f:
            cached = json.load(f)
        if cached.get("date") == today and cached.get("client_id") == client_id and cached.get("token"):
            log.info("Using cached Dhan access token for %s", today)
            return cached["token"]
    except (OSError, ValueError):
        pass

    if login is None:
        from dhanhq import DhanLogin

        login = DhanLogin(client_id)
    # Avoid using a code that is about to roll over.
    if 30 - (time.time() % 30) < 3:
        time.sleep(3)
    token = _extract_token(login.generate_token(pin, totp(totp_secret)))
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    fd = os.open(cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # owner-only
    with os.fdopen(fd, "w") as f:
        json.dump({"date": today, "client_id": client_id, "token": token}, f)
    log.info("Generated new Dhan access token via TOTP")
    return token
