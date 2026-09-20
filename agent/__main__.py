"""Run with:  python -m agent selftest | once | forever"""
from __future__ import annotations
import sys
import time

import requests

from . import osm
from .audit import AuditClient, AuditError
from .config import load_settings, missing_required
from .logutil import log, safe_exc, set_secrets
from .portal import Portal, PortalError
from .scheduler import run_cycle
from .slack import Slack
from .web import Fetcher
from .writer import Gemini
from .wordpress import WordPress, WordPressError


def build(cfg):
    set_secrets(cfg.secret_values())
    portal = Portal(cfg.portal_url, cfg.agent_token)
    slack = Slack(cfg)
    audit = AuditClient(cfg.audit_url, cfg.audit_token) if cfg.audit_url and cfg.audit_token else None
    gemini = Gemini(cfg.gemini_keys, cfg.gemini_model) if cfg.gemini_keys else None
    return portal, slack, audit, gemini, Fetcher()


def build_extras(cfg):
    """Optional helpers: Overture Maps (needs duckdb) and WordPress publishing."""
    overture = None
    if cfg.overture_on:
        try:
            import duckdb  # noqa: F401
            from .overture import Overture
            overture = Overture()
        except ImportError:
            log("Overture Maps is off: the 'duckdb' package is not installed.")
    wp = WordPress(cfg.wp_url, cfg.wp_user, cfg.wp_password, cfg.wp_mode) if (cfg.wp_url and cfg.wp_user and cfg.wp_password) else None
    return overture, wp


def selftest(cfg) -> int:
    """A friendly checklist. Prints only OK / WARNING / PROBLEM - never any secrets."""
    bad = 0

    def line(ok, text):
        nonlocal bad
        icon = {True: "OK      ", False: "PROBLEM ", None: "WARNING "}[ok]
        if ok is False:
            bad += 1
        print(f"  [{icon}] {text}", flush=True)

    print("AI Agent self-test\n", flush=True)
    miss = missing_required(cfg)
    if miss:
        line(False, "Settings missing: " + ", ".join(miss) + "  (fill them in settings.env)")
        return 1
    set_secrets(cfg.secret_values())
    line(True, "Settings file loaded")
    portal, slack, audit, gemini, _ = build(cfg)

    try:
        portal.ping()
        st = portal.stats()
        line(True, f"Portal connected ({st['leads_total']} leads saved so far)")
    except Exception as e:  # noqa: BLE001
        line(False, f"Portal: {safe_exc(e, 150)}  -> is the Phase 2 update installed on Hostinger?")
        return 1

    if not cfg.audit_url or not cfg.audit_token:
        line(False, "AUDIT_URL / AUDIT_API_TOKEN not set - audits cannot run")
    else:
        try:
            audit.ping()
            line(True, "Audit tool connected")
        except Exception as e:  # noqa: BLE001
            line(False, f"Audit tool: {safe_exc(e, 150)}")

    if not gemini:
        line(None, "No GEMINI_API_KEY - messages will use plain templates instead of AI")
    else:
        for i, ok, note in gemini.check_keys():
            line(True if ok else None, f"Gemini key #{i + 1} of {len(gemini.keys)}: {'works' if ok else 'PROBLEM - ' + note}" + (f" ({note})" if ok else ""))
        try:
            model = gemini.pick_model()
            out = gemini.generate_json('Reply with exactly this JSON: {"ok": true}')
            line(bool(out), f"Gemini AI writing works (model {model}; {gemini.live_count()} key(s) in use)")
        except Exception as e:  # noqa: BLE001
            line(None, f"Gemini writing problem ({safe_exc(e, 120)}) - templates will be used until it works")

    if not (cfg.slack_agent):
        line(None, "No Slack webhook set - you will not get Slack updates")
    elif slack.agent(":white_check_mark: AI Agent self-test: Slack is connected."):
        line(True, "Slack connected (check your channel for a test message)")
    else:
        line(False, "Slack did not accept the message - check the webhook URL")

    try:
        found = osm.search(51.5074, -0.1278, 1500, ['["amenity"="dentist"]'], "London", limit=3)
        line(True, f"OpenStreetMap (free lead source) reachable - test search returned {len(found)} results")
    except Exception as e:  # noqa: BLE001
        line(None, f"OpenStreetMap busy right now ({safe_exc(e, 80)}) - the agent retries automatically")

    overture, wp = build_extras(cfg)
    if not cfg.overture_on:
        line(None, "Overture Maps switched off (USE_OVERTURE=no)")
    elif overture is None:
        line(None, "Overture Maps not available (duckdb missing) - only OpenStreetMap will be used")
    else:
        try:
            got = overture.search(51.5074, -0.1278, 1500, ["dentist"], "London", limit=5)
            line(True, f"Overture Maps (bigger free lead source) works - release {overture.release}, test search returned {len(got)} results")
        except Exception as e:  # noqa: BLE001
            line(None, f"Overture Maps problem ({safe_exc(e, 140)}) - the agent falls back to OpenStreetMap")
    if wp is None:
        line(None, "WordPress not connected (optional) - approved blog posts must be pasted into your website by you")
    else:
        try:
            line(True, f"WordPress connected as '{wp.ping()}' (new posts are sent as {wp.mode})")
        except (WordPressError, requests.RequestException) as e:
            line(False, f"WordPress: {safe_exc(e, 140)}")

    if cfg.explorium_key:
        try:
            r = requests.get("https://api.explorium.ai/v2/credits", headers={"api_key": cfg.explorium_key}, timeout=20)
            if r.status_code == 200:
                d = r.json()
                line(True, f"Explorium key works ({d.get('remaining_credits')} of {d.get('allocated_credits')} credits left)")
            else:
                line(None, f"Explorium answered HTTP {r.status_code} - key may be wrong (not needed yet)")
        except Exception as e:  # noqa: BLE001
            line(None, f"Explorium not reachable ({safe_exc(e, 80)}) - not needed yet")
    else:
        line(None, "No EXPLORIUM_API_KEY (optional - not used yet)")

    if not cfg.postal_address:
        line(None, "BUSINESS_POSTAL_ADDRESS is empty - emails will have no address line (required by US law)")
    else:
        line(True, "Business address set for email footers")

    print("\nRESULT: " + ("everything important works." if bad == 0 else f"{bad} problem(s) to fix above."), flush=True)
    return 0 if bad == 0 else 1


def once(cfg, minutes: float | None = None) -> int:
    miss = missing_required(cfg)
    if miss:
        print("Settings missing: " + ", ".join(miss))
        return 1
    portal, slack, audit, gemini, fetcher = build(cfg)
    overture, wp = build_extras(cfg)
    try:
        portal.ping()
    except PortalError as e:
        log(f"Portal not reachable: {safe_exc(e)}")
        slack.error("The agent cannot reach your portal, so this run stopped. It will try again at the next scheduled run.")
        return 1
    try:
        summary = run_cycle(cfg, portal, slack, audit, gemini, fetcher, minutes or cfg.run_minutes, overture=overture, wp=wp)
    except PortalError as e:
        log(f"Run stopped: {safe_exc(e)}")
        slack.error(f"Run stopped: {safe_exc(e, 200)}")
        return 1
    log("Run finished: " + ", ".join(f"{k}={v}" for k, v in _counts(summary).items()))
    return 0


def _counts(summary: dict) -> dict:
    out = {}
    for phase, val in summary.items():
        if isinstance(val, dict):
            for k, v in val.items():
                if isinstance(v, int):
                    out[f"{phase}.{k}"] = v
    out["errors"] = len(summary.get("errors", []))
    return out


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "once"
    cfg = load_settings()
    set_secrets(cfg.secret_values())
    if cmd == "selftest":
        try:
            return selftest(cfg)
        except Exception as e:  # noqa: BLE001
            print("Self-test crashed: " + safe_exc(e))
            return 1
    if cmd == "once":
        try:
            return once(cfg)
        except Exception as e:  # noqa: BLE001
            print("Run crashed: " + safe_exc(e))
            return 1
    if cmd == "forever":
        while True:
            try:
                once(cfg)
            except Exception as e:  # noqa: BLE001
                log(f"Unexpected error: {safe_exc(e)}")
            time.sleep(300)
    print("Usage: python -m agent selftest | once | forever")
    return 2


if __name__ == "__main__":
    sys.exit(main())
