"""Talks to your portal (api/agent_api.php)."""
from __future__ import annotations
import time
import requests


class PortalError(Exception):
    pass


class Portal:
    def __init__(self, base_url: str, token: str, timeout: int = 40):
        self.url = base_url.rstrip("/") + "/api/agent_api.php"
        self.token = token
        self.timeout = timeout
        self.s = requests.Session()

    def call(self, action: str, **payload) -> dict:
        last = "unknown"
        for attempt in range(3):
            try:
                r = self.s.post(self.url, json={"action": action, **payload},
                                headers={"X-Agent-Token": self.token}, timeout=self.timeout)
            except requests.RequestException as e:
                last = type(e).__name__
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code in (429, 502, 503, 504) or (r.status_code >= 500 and attempt < 2):
                last = f"HTTP {r.status_code}"
                time.sleep(3 * (attempt + 1))
                continue
            try:
                data = r.json()
            except ValueError:
                raise PortalError(f"portal sent a non-JSON reply (HTTP {r.status_code})")
            if not data.get("ok"):
                raise PortalError(f"{action}: {data.get('error', 'failed')} (HTTP {r.status_code})")
            return data
        raise PortalError(f"portal unreachable after 3 tries ({last})")

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
