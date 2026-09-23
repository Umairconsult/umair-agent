"""IndexNow - a free, no-account, no-API-key protocol supported by Bing, Yandex, and other
participating search engines (not Google, which does not participate). When you publish or
update a page, one ping tells every participating engine to recrawl it, instead of waiting
for them to notice on their own. https://www.indexnow.org/documentation

Setup is one key, generated once and hosted as a plain-text file at your site's root - the
WPCode bridge snippet serves that file for you (see wpcode_snippet_agent_bridge.txt), so
there's nothing extra to upload."""
from __future__ import annotations
import secrets

import requests

from .logutil import log, safe_exc

API_URL = "https://api.indexnow.org/indexnow"


def generate_key() -> str:
    """Run this once to make a new key for INDEXNOW_KEY in settings.env."""
    return secrets.token_hex(16)


def submit(host: str, key: str, urls: list[str]) -> bool:
    """Best-effort: returns True on success, False (never raises) on any problem - a failed
    ping should never block or break a publish that already succeeded."""
    if not host or not key or not urls:
        return False
    key_location = f"https://{host}/{key}.txt"
    try:
        r = requests.post(API_URL, json={"host": host, "key": key, "keyLocation": key_location, "urlList": urls},
                          headers={"Content-Type": "application/json"}, timeout=15)
        ok = r.status_code in (200, 202)
        if not ok:
            log(f"IndexNow ping got HTTP {r.status_code} for {len(urls)} url(s)")
        return ok
    except requests.RequestException as e:
        log(f"IndexNow ping failed: {safe_exc(e, 100)}")
        return False
