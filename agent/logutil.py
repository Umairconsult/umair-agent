"""Logging that never leaks secrets or lead details.

IMPORTANT: if the GitHub repo is public, the run logs are public too. So the
agent only ever logs COUNTS and generic messages here - never business names,
emails, phone numbers, or keys. Full details live in your private portal.
"""
from __future__ import annotations
import time

_secrets: list[str] = []


def set_secrets(values) -> None:
    global _secrets
    vals = {v for v in values if v and len(v) >= 6}
    _secrets = sorted(vals, key=len, reverse=True)  # longest first


def redact(text) -> str:
    s = str(text)
    for v in _secrets:
        s = s.replace(v, "***")
    return s


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {redact(msg)}", flush=True)


def safe_exc(e: BaseException, limit: int = 220) -> str:
    return redact(f"{type(e).__name__}: {e}")[:limit]
