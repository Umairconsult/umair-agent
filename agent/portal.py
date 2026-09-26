"""Talks to your portal (api/agent_api.php).

Why this file is careful about non-JSON replies: something in front of agent_api.php sometimes
answers a request from GitHub's servers with an HTML/plain-text page instead of letting it reach
the script, and that page is not JSON. Two different things can cause this, and they need
different fixes, so this file tells them apart by status code:

  * HTTP 403/406/409/418/451/503 (or a 200 full of HTML) - usually YOUR site's own firewall
    (ModSecurity / Imunify360 / a CDN's WAF) reacting to something about the request. This is
    fixable in hPanel: allow /portal/api/agent_api.php, or ask Hostinger support to whitelist it
    for GitHub Actions.
  * HTTP 429 - almost always Hostinger's own network-edge rate limit on automated traffic from
    whole cloud IP ranges (AWS, Microsoft/Azure - which is what GitHub-hosted runners use - and
    Meta). This is NOT a per-site WAF setting and hPanel has no toggle for it; it applies to every
    Hostinger customer and, per Hostinger's own docs, "typically resolve[s] within a few hours".
    So it's handled with a longer backoff and, if it still hasn't cleared, the run gives up
    cleanly and leaves it to the next scheduled run rather than burning the whole job waiting.

Either way this version:
  * introduces itself with a normal browser-style User-Agent (bare "python-requests" is a
    classic firewall trigger),
  * waits and tries again when it gets a firewall-style or rate-limited answer, instead of
    failing at once,
  * spaces its calls a little so a burst of requests doesn't look like an attack,
  * copes with harmless PHP warnings printed in front of the JSON,
  * says WHO blocked it (server name / page title) so the cause is visible in the run log.
A real "wrong token" answer is JSON and is never retried.
"""
from __future__ import annotations
import base64
import json
import re
import time

import requests

from .logutil import log

BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36 UmairConsultAgent/3.2")
MIN_GAP = 0.25                       # seconds between two portal calls (be gentle with the firewall)
FIREWALL_WAITS = (6, 20, 45, 90)     # seconds to wait before each retry of a firewall-style answer (403/406/... block pages)
# HTTP 429 from a "server=hcdn"/"server=..." reply is usually NOT your site's WAF - Hostinger applies this at the
# network edge to whole IP ranges (AWS/Azure/Meta) that GitHub-hosted runners live in, regardless of hPanel
# security settings. Hostinger's own guidance for this is "wait and retry with exponential backoff", and says
# these windows typically clear within a few hours - much longer than one job can afford to sleep for. So we
# back off longer than for a same-site firewall page, but we still give up well before the job timeout and let
# the *next* scheduled run (twice a day) pick it back up, rather than block the runner for hours.
RATE_LIMIT_WAITS = (15, 45, 120, 240, 240)
COOLDOWN = 120                       # after a call gives up on the firewall, fail fast for this long


class PortalError(Exception):
    pass


def parse_json_reply(text: str):
    """Returns a dict from the portal's reply, or None if there is no JSON object in it.
    Tolerates PHP warnings/notices printed in front of (or after) the JSON."""
    text = (text or "").strip().lstrip("\ufeff")
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    for m in re.finditer(r"\{", text):
        try:
            data, _ = json.JSONDecoder().raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(data, dict) and "ok" in data:
            return data
    return None


def describe_block(r) -> str:
    """A few safe, non-secret words about a non-JSON reply, e.g. 'server=cloudflare, page="Attention Required"'."""
    bits = []
    server = (r.headers.get("Server") or "").strip()
    if server:
        bits.append(f"server={server[:30]}")
    if r.headers.get("cf-ray"):
        bits.append("via Cloudflare")
    if r.headers.get("x-imunify360-request-id") or "imunify360" in (r.headers.get("Server") or "").lower():
        bits.append("Imunify360 firewall")
    m = re.search(r"<title[^>]*>(.*?)</title>", r.text or "", re.I | re.S)
    page = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    if not page:
        page = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text or "")).strip()
    if page:
        bits.append(f'page="{page[:70]}"')
    return ", ".join(bits)


class Portal:
    def __init__(self, base_url: str, token: str, timeout: int = 40):
        self.url = base_url.rstrip("/") + "/api/agent_api.php"
        self.token = token
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": BROWSER_UA, "Accept": "application/json, */*;q=0.5",
                               "Accept-Language": "en-US,en;q=0.9"})
        self._last_call = 0.0
        self._cooldown_until = 0.0
        self._cooldown_reason = ""
        self.firewall_hits = 0        # how many firewall-style answers we met this run (for the summary)
        self.prefer_b64 = False       # True once the base64 envelope got through where plain JSON was blocked
        self.b64_ok = True            # set False if the portal (an older agent_api.php) does not understand the envelope

    def _sleep_gap(self) -> None:
        wait = self._last_call + MIN_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def call(self, action: str, **payload) -> dict:
        if time.time() < self._cooldown_until:
            raise PortalError(self._cooldown_reason)
        last = "unknown"
        fw_tries = 0          # firewall-style answers so far for THIS call
        other_tries = 0       # network trouble / 5xx so far for THIS call
        while True:
            self._sleep_gap()
            body = {"action": action, **payload}
            # Some hosting firewalls block a request because its TEXT looks like an attack (e.g. an email with "--" or
            # "select ... from" in it), and then every retry of the same text is blocked too. After the first block we
            # send the same JSON base64-encoded ({"b64": ...}), which portal/api/agent_api.php unpacks.
            use_b64 = action != "ping" and self.b64_ok and (self.prefer_b64 or fw_tries >= 1)
            if use_b64:
                body = {"b64": base64.b64encode(json.dumps(body, ensure_ascii=False).encode("utf-8")).decode("ascii")}
            try:
                r = self.s.post(self.url, json=body, headers={"X-Agent-Token": self.token}, timeout=self.timeout)
            except requests.RequestException as e:
                last = type(e).__name__
                other_tries += 1
                if other_tries >= 4:
                    break
                time.sleep(2 * other_tries)
                continue

            data = parse_json_reply(r.text)
            if data is not None:
                if use_b64 and not data.get("ok") and data.get("error") == "unknown action":
                    self.b64_ok = False        # old portal file: fall back to plain JSON
                    self.prefer_b64 = False
                    continue
                if use_b64 and data.get("ok") and fw_tries >= 1:
                    self.prefer_b64 = True     # plain JSON was blocked, the envelope worked: keep using it
                if (r.status_code == 429 or r.status_code >= 500) and not data.get("ok"):
                    last = f"HTTP {r.status_code}"
                    other_tries += 1
                    if other_tries >= 4:
                        break
                    time.sleep(3 * other_tries)
                    continue
                if not data.get("ok"):
                    raise PortalError(f"{action}: {data.get('error', 'failed')} (HTTP {r.status_code})")
                return data

            # ---- not JSON ----
            who = describe_block(r)
            if r.status_code in (403, 406, 409, 418, 429, 451, 503) or (r.status_code == 200 and "<html" in (r.text or "").lower()):
                is_rate_limit = r.status_code == 429
                waits = RATE_LIMIT_WAITS if is_rate_limit else FIREWALL_WAITS
                # firewall / security page, or a 429 rate limit, in front of the portal: usually clears by itself
                self.firewall_hits += 1
                last = f"HTTP {r.status_code}" + (f"; {who}" if who else "")
                if fw_tries >= len(waits):
                    self._cooldown_until = time.time() + COOLDOWN
                    kind = "Hostinger's edge is rate-limiting this runner's network range" if is_rate_limit else "portal firewall is blocking this runner"
                    self._cooldown_reason = f"{kind} ({last}) - pausing portal calls for a couple of minutes"
                    reason = (" - Hostinger's network-level rate limit for automated traffic (not a per-site WAF rule); "
                              "this usually clears on its own within a few hours, so the next scheduled run should work"
                              if is_rate_limit else " - looks like a hosting firewall block")
                    raise PortalError(f"portal sent a non-JSON reply (HTTP {r.status_code}"
                                      + (f"; {who}" if who else "") + f"){reason}; gave up after {fw_tries + 1} tries")
                wait = waits[fw_tries]
                try:
                    retry_after = int(r.headers.get("Retry-After", "0"))
                    wait = max(wait, min(300 if is_rate_limit else 120, retry_after))
                except ValueError:
                    pass
                if fw_tries == 0:
                    what = "a network-level rate limit (HTTP 429)" if is_rate_limit else "a firewall-style page"
                    log(f"Portal answered with {what} ({last}) - waiting and retrying (this is usually temporary)")
                fw_tries += 1
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                other_tries += 1
                if other_tries >= 4:
                    break
                time.sleep(3 * other_tries)
                continue
            raise PortalError(f"portal sent a non-JSON reply (HTTP {r.status_code}"
                              + (f"; {who}" if who else "") + ")")
        raise PortalError(f"portal unreachable after several tries ({last})")

    def wait_until_reachable(self, max_wait: int = 240) -> dict:
        """Ping until the portal answers, for up to ~max_wait seconds. Used at the start of a run so a
        short firewall hiccup doesn't waste the whole run. Raises PortalError if it never answers."""
        end = time.time() + max_wait
        err: PortalError | None = None
        while True:
            try:
                self._cooldown_until = 0.0
                return self.ping()
            except PortalError as e:
                err = e
                if time.time() + 30 >= end:
                    raise err
                log(f"Portal not answering yet ({str(e)[:120]}) - trying again in 30 seconds")
                time.sleep(30)

    # ---- convenience wrappers ----
    def ping(self): return self.call("ping")
    def stats(self) -> dict: return self.call("stats")["stats"]
    def upsert_lead(self, **lead) -> dict: return self.call("upsert_lead", **lead)
    def leads_to_audit(self, limit=10) -> list: return self.call("leads_to_audit", limit=limit)["leads"]
    def save_audit(self, **kw): return self.call("save_audit", **kw)
    def mark_bad_data(self, id: int, reason: str): return self.call("mark_bad_data", id=id, reason=reason)
    def leads_to_write(self, limit=10) -> list: return self.call("leads_to_write", limit=limit)["leads"]
    def save_messages(self, **kw): return self.call("save_messages", **kw)
    def get_dnc(self) -> list: return self.call("get_dnc")["items"]
    def log(self, kind: str, message: str, meta=None):
        try:
            self.call("log", kind=kind, message=message, meta=meta)
        except PortalError:
            pass
    def add_seo(self, **kw): return self.call("add_seo", **kw)
    def seo_update(self, **kw): return self.call("seo_update", **kw)
    def add_note(self, title: str, body: str = "", subject: str = "", kind: str = "general"):
        return self.call("add_note", title=title, body=body, subject=subject, kind=kind)
    def add_content(self, **kw) -> dict: return self.call("add_content", **kw)
    def content_list(self, **kw) -> list: return self.call("content_list", **kw)["items"]
    def content_update(self, **kw): return self.call("content_update", **kw)
    def seo_list(self, **kw) -> list: return self.call("seo_list", **kw)["items"]
    def leads_to_followup(self, limit=10) -> list: return self.call("leads_to_followup", limit=limit)["leads"]
    def save_followup(self, **kw): return self.call("save_followup", **kw)
    def state_set(self, key: str, value: str): return self.call("state_set", key=key, value=value)
    def state_list(self, prefix: str) -> dict:
        items = self.call("state_list", prefix=prefix, limit=20000)["items"]
        return {i["k"]: i["v"] for i in items}
