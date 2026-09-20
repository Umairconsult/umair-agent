"""Sends messages to your Slack channels (incoming webhooks). Never raises."""
from __future__ import annotations
import requests
from .logutil import log, safe_exc


def _post(url: str, text: str) -> bool:
    if not url or "YOUR_SLACK" in url:
        return False
    try:
        r = requests.post(url, json={"text": text}, timeout=12)
        return r.status_code == 200
    except requests.RequestException as e:
        log(f"Slack post failed: {safe_exc(e)}")
        return False


class Slack:
    def __init__(self, cfg):
        self.cfg = cfg

    def agent(self, text: str) -> bool: return _post(self.cfg.slack_agent, text)
    def leads(self, text: str) -> bool: return _post(self.cfg.slack_leads, text)
    def error(self, text: str) -> bool: return _post(self.cfg.slack_agent, f":warning: *AI Agent problem*\n{text}")
