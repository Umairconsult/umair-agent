"""Run with:  python -m agent selftest | once | forever | social"""
from __future__ import annotations
import os
import sys
import time
import requests

from . import osm
from .audit import AuditClient, AuditError
from .config import load_settings, missing_required
from .logutil import log, redact, safe_exc, set_secrets
from .portal import Portal, PortalError
from .scheduler import local_now, run_cycle
from .slack import Slack
from .web import Fetcher
from .writer import Gemini
from .wordpress import WordPress, WordPressBridge, WordPressError


EXPECTED_PORTAL_VERSION = "3.2"


def _esc(t: str) -> str:
    return t.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def gh(level: str, text: str, title: str = "AI Agent") -> None:
    """Shows a message in the yellow/red box at the top of the GitHub run page (plain words, no digging in logs)."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level} title={_esc(title)}::{_esc(redact(text))}", flush=True)


def gh_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(redact(l) for l in lines) + "\n")
        except OSError:
            pass


FIREWALL_HELP = ("If this says 403 / firewall: your hosting (Hostinger) is blocking GitHub's servers. Open hPanel > Security "
                 "(Imunify360 / IP Manager / WAF) and allow the path /portal/api/agent_api.php, or ask Hostinger support to "
                 "whitelist it for GitHub Actions. The agent already retries automatically.")

# A 429 (with e.g. server=hcdn) is a DIFFERENT problem from a 403 firewall page, and the 403 advice above does not
# fix it: Hostinger applies this rate limit at the network edge to whole IP ranges (AWS, Microsoft/Azure, Meta) -
# GitHub-hosted runners live in Azure's range - not per-site, so there is no hPanel WAF/Imunify360 rule to add for
# /portal/api/agent_api.php here. See: https://www.hostinger.com/support/429-errors-on-automated-integrations-and-link-previews/
RATE_LIMIT_HELP = ("If this says 429: this is Hostinger's own network-level rate limit on automated traffic from cloud IP "
                    "ranges (including GitHub Actions' Azure IPs) - it is not your site's firewall/WAF, so adding an "
                    "Imunify360/WAF allow-rule for the path will not fix it, and it isn't tied to your hosting plan or "
                    "domain. It typically clears on its own within a few hours, so the agent backs off and lets the next "
                    "scheduled run (twice a day) try again. If it keeps happening every run, ask Hostinger support about "
                    "their 'server-level rate limits for automated traffic' and whether your domain/IP can be excluded, "
                    "or run the agent from a non-cloud IP (e.g. a small VPS or self-hosted runner) instead of GitHub-hosted runners.")


def portal_down_text(e: Exception) -> str:
    txt = safe_exc(e, 300)
    if "HTTP 429" in txt:
        return txt + " " + RATE_LIMIT_HELP
    if any(w in txt for w in ("403", "firewall", "non-JSON")):
        return txt + " " + FIREWALL_HELP
    return txt


def build(cfg):
    set_secrets(cfg.secret_values())
    portal = Portal(cfg.portal_url, cfg.agent_token)
    slack = Slack(cfg)
    audit = AuditClient(cfg.audit_url, cfg.audit_token) if cfg.audit_url and cfg.audit_token else None
    gemini = Gemini(cfg.gemini_keys, cfg.gemini_model, cfg.gemini_image_model) if cfg.gemini_keys else None
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
    wp = None
    if cfg.wp_url and cfg.wp_agent_key:
        wp = WordPressBridge(cfg.wp_url, cfg.wp_agent_key, cfg.wp_mode)
    elif cfg.wp_url and cfg.wp_user and cfg.wp_password:
        wp = WordPress(cfg.wp_url, cfg.wp_user, cfg.wp_password, cfg.wp_mode)
    return overture, wp


def selftest(cfg) -> int:
    """A friendly checklist. Prints only OK / WARNING / PROBLEM - never any secrets."""
    bad = 0
    results: list[tuple] = []

    def line(ok, text):
        nonlocal bad
        icon = {True: "OK      ", False: "PROBLEM ", None: "WARNING "}[ok]
        if ok is False:
            bad += 1
            gh("error", text, "Problem to fix")
        elif ok is None:
            gh("warning", text, "Note")
        results.append((ok, text))
        print(f"  [{icon}] {text}", flush=True)

    def finish(code: int) -> int:
        mark = {True: "✅", False: "❌", None: "⚠️"}
        gh_summary(["### AI Agent self-test", ""] + [f"- {mark[o]} {t}" for o, t in results]
                   + ["", "**Result:** " + ("everything important works." if code == 0 else "there are problems to fix (marked ❌ above).")])
        return code

    print("AI Agent self-test\n", flush=True)
    miss = missing_required(cfg)
    if miss:
        line(False, "Settings missing: " + ", ".join(miss) + "  (fill them in settings.env, then update the GitHub secret SETTINGS_ENV)")
        return finish(1)
    set_secrets(cfg.secret_values())
    line(True, "Settings file loaded")
    portal, slack, audit, gemini, _ = build(cfg)

    try:
        ver = portal.ping().get("version")
        st = portal.stats()
        line(True, f"Portal connected ({st['leads_total']} leads saved so far)")
        if ver != EXPECTED_PORTAL_VERSION:
            line(None, f"Portal files are version {ver or 'OLD (no version)'} but this agent expects {EXPECTED_PORTAL_VERSION} - install hostinger_phase3_update.zip (open portal/agent_doctor.php to see which file is old)")
    except Exception as e:  # noqa: BLE001
        line(False, f"Portal: {portal_down_text(e)}  -> open https://umairconsult.com/portal/agent_doctor.php to see which file is old or missing")
        return finish(1)

    try:
        ctl = portal.state_list("ctl:")
        paused = ctl.get("ctl:pause_finding") == "1"
        line(None if paused else True, "Lead finding is PAUSED from the portal (resume it on the AI Agent page)" if paused else "Lead finding is running (you can pause it on the portal)")
    except PortalError:
        pass
    if not cfg.audit_url or not cfg.audit_token:
        line(False, "AUDIT_URL / AUDIT_API_TOKEN not set - audits cannot run")
    else:
        try:
            info = audit.ping_info()
            line(True, "Audit tool connected")
            if info.get("version") != EXPECTED_PORTAL_VERSION:
                line(False, f"Audit door is version {info.get('version') or 'OLD'} but {EXPECTED_PORTAL_VERSION} is needed to store full reports - replace audit/agent_audit.php on Hostinger with the one from the update zip")
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
        line(None, "WordPress not connected (optional) - approved blog posts and SEO fixes must be applied by you. Install the WPCode snippet and add WP_AGENT_KEY.")
    else:
        try:
            name = wp.ping()
            extra = " (Rank Math found)" if getattr(wp, "rank_math", False) else ""
            line(True, f"WordPress connected{': ' + name if name else ''}{extra}. Approved posts are {'published live' if wp.mode == 'publish' else 'saved as drafts'}.")
        except (WordPressError, requests.RequestException) as e:
            line(False, f"WordPress: {safe_exc(e, 200)}")

    if not cfg.use_registries:
        line(None, "Business registries switched off (USE_REGISTRIES=no)")
    elif not cfg.companies_house_key:
        line(None, "No COMPANIES_HOUSE_API_KEY - UK Companies House registry lookups are off (free key at developer.company-information.service.gov.uk)")
    else:
        line(True, "Business registry (UK Companies House) key set")

    if not cfg.use_associations:
        line(None, "Industry-association directories switched off (USE_ASSOCIATIONS=no)")
    else:
        from .associations import ASSOCIATIONS
        n = sum(len(v) for v in ASSOCIATIONS.values())
        line(True if n else None, f"{n} approved association director{'y' if n == 1 else 'ies'} configured" if n
             else "No association directories approved yet (edit agent/associations.py to add ones you've checked)")

    line(True if cfg.use_hiring_signals else None,
         "Hiring-signal boost is on (RemoteOK + careers-page check)" if cfg.use_hiring_signals
         else "Hiring-signal boost switched off (USE_HIRING_SIGNALS=no)")

    if not cfg.gsc_service_account_json or not cfg.gsc_site_url:
        line(None, "Search Console not set up yet (GSC_SERVICE_ACCOUNT_JSON / GSC_SITE_URL) - optional")
    else:
        try:
            from .search_console import SearchConsole, SearchConsoleError
            sc = SearchConsole(cfg.gsc_service_account_json, cfg.gsc_site_url)
            sc.ping()
            line(True, f"Search Console connected for {cfg.gsc_site_url}")
        except SearchConsoleError as e:
            line(None, f"Search Console problem: {safe_exc(e, 500)}")
        except Exception as e:  # noqa: BLE001
            line(None, f"Search Console problem: {safe_exc(e, 500)}")

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
    # else: nothing prints here on purpose - EXPLORIUM_API_KEY isn't wired into any feature
    # yet, so warning about a key you haven't set for a feature that doesn't exist yet is just noise.

    from .media.drive import DriveError, build_drive, drive_configured
    if not drive_configured(cfg):
        line(None, "Google Drive not set up yet (GOOGLE_DRIVE_FOLDER_ID + either GOOGLE_SERVICE_ACCOUNT_JSON or the three "
                   "GOOGLE_OAUTH_* settings) - the social media content pipeline cannot run without it")
    else:
        try:
            d = build_drive(cfg)
            name = d.ping()
            line(True, f"Google Drive connected as {'your Google account' if d.mode == 'oauth' else 'a service account'} (folder: {name or cfg.google_drive_folder_id})")
            if d.mode == "service_account":
                line(None, "Drive login is a service account: uploads only work if the folder is inside a Google Workspace "
                           "Shared Drive (a normal My Drive folder fails with 'storage quota'). If the social run reports that, "
                           "use the GOOGLE_OAUTH_* login instead (see get_drive_token.py)")
        except DriveError as e:
            line(False, f"Google Drive: {safe_exc(e, 400)}")
    if drive_configured(cfg):
        from pathlib import Path
        logo = Path(cfg.logo_path)
        line(True if logo.is_file() else False,
             f"Logo file found ({cfg.logo_path})" if logo.is_file() else f"Logo file NOT found at '{cfg.logo_path}' - social images need it")

    if not cfg.postal_address:
        line(None, "BUSINESS_POSTAL_ADDRESS is empty - emails will have no address line (required by US law)")
    else:
        line(True, "Business address set for email footers")

    print("\nRESULT: " + ("everything important works." if bad == 0 else f"{bad} problem(s) to fix above."), flush=True)
    return finish(0 if bad == 0 else 1)


def once(cfg, minutes: float | None = None) -> int:
    miss = missing_required(cfg)
    if miss:
        print("Settings missing: " + ", ".join(miss))
        return 1
    portal, slack, audit, gemini, fetcher = build(cfg)
    overture, wp = build_extras(cfg)
    try:
        portal.wait_until_reachable()        # waits out a short firewall hiccup instead of failing the run at once
    except PortalError as e:
        log(f"Portal not reachable: {safe_exc(e)}")
        gh("error", f"The agent cannot reach your portal ({portal_down_text(e)}). Open umairconsult.com/portal/agent_doctor.php.", "Portal not reachable")
        slack.error("The agent cannot reach your portal, so this run stopped. It will try again at the next scheduled run. "
                    + portal_down_text(e)[:400])
        return 1
    try:
        summary = run_cycle(cfg, portal, slack, audit, gemini, fetcher, minutes or cfg.run_minutes, overture=overture, wp=wp)
    except PortalError as e:
        log(f"Run stopped: {safe_exc(e)}")
        gh("error", f"Run stopped: {portal_down_text(e)}", "Run stopped")
        slack.error(f"Run stopped: {portal_down_text(e)[:500]}")
        return 1
    if portal.firewall_hits:
        gh("warning", f"The portal answered {portal.firewall_hits} time(s) with a block/rate-limit page; the agent waited and retried. "
                      "If the run log shows HTTP 429, that's Hostinger's own automated-traffic rate limit (see RATE_LIMIT_HELP in "
                      "agent/__main__.py) and it isn't fixed by a firewall allow-rule; a 403 usually is.", "Portal firewall")
    log("Run finished: " + ", ".join(f"{k}={v}" for k, v in _counts(summary).items()))
    for err in summary.get("errors", []):
        gh("warning", err, "A step had a problem (the run continued)")
    gh_summary(["### AI Agent run", ""] + [f"- **{k}**: {v}" for k, v in _counts(summary).items()]
               + ([""] + [f"- ⚠️ {e}" for e in summary.get("errors", [])] if summary.get("errors") else []))
    return 0


def social(cfg) -> int:
    """Runs today's social-media content pipeline: generates one branded image per platform,
    uploads it to Google Drive, records it in the portal for review, and sends a Slack
    notification. Never posts anything to social media - see agent/social.py."""
    miss = missing_required(cfg)
    if miss:
        print("Settings missing: " + ", ".join(miss))
        return 1
    portal, slack, _audit, gemini, _fetcher = build(cfg)
    try:
        portal.wait_until_reachable()
    except PortalError as e:
        log(f"Portal not reachable: {safe_exc(e)}")
        gh("error", f"The agent cannot reach your portal ({portal_down_text(e)}).", "Portal not reachable")
        slack.error("The social media run could not reach your portal, so it stopped. It will try again next time. "
                    + portal_down_text(e)[:400])
        return 1

    from .social import run_social
    today = local_now(cfg).date()
    try:
        result = run_social(cfg, portal, gemini, today)
    except Exception as e:  # noqa: BLE001 - a crash must still be reported, not swallowed
        log(f"Social run crashed: {safe_exc(e)}")
        gh("error", f"Social media run crashed: {safe_exc(e, 200)}", "Social pipeline")
        slack.error(f"Today's social media content run crashed unexpectedly: {safe_exc(e, 200)}")
        return 1

    done, target = result["done"], result["target"]
    already = result.get("already_done_today", 0)
    lines = [f":art: *Social media content - {today.isoformat()}*"]
    if done:
        lines.append(f"New this run: *{done}* (today's total so far: *{done + already}* of *{target}*)")
    elif target and already >= target:
        lines.append(f"Already complete for today - all *{target}* were made in an earlier run.")
    else:
        lines.append(f"Nothing new this run. Today's total so far: *{already}* of *{target}*")
    for p in result["posts"]:
        cap = " ".join(p["caption"].split())
        cap = cap[:220] + ("..." if len(cap) > 220 else "")
        lines.append(f"*{p['label']}* - {p.get('platform_label', p['platform'])} (variant {p['variant']}): <{p['link']}|open image in Drive>")
        lines.append(f">{cap}" + (f"\n>*CTA:* {p['cta']}" if p["cta"] else ""))
    if result["posts"]:
        lines.append(f"Review them on your portal: {cfg.portal_url}/admin_ai_agent.php (nothing is posted automatically).")
    if result["failures"]:
        lines.append(":warning: *Problems today:*")
        for f in result["failures"]:
            lines.append(f"- {f}")
    slack.social("\n".join(lines))

    log(f"Social run finished: {done} new, {already} already done today, target {target}"
        + (f", {len(result['failures'])} problem(s)" if result["failures"] else ""))
    gh_summary(["### Social media content run", "",
               f"- **new this run**: {done}", f"- **already done today**: {already}", f"- **target**: {target}"]
               + ([""] + [f"- ⚠️ {f}" for f in result["failures"]] if result["failures"] else []))
    for f in result["failures"]:
        gh("warning", f, "Social content problem (the run continued)")
    if not done and not already and result["failures"]:
        return 1        # nothing was made at all: show the run as failed so you notice (details are in Slack)
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
    if cmd == "social":
        try:
            return social(cfg)
        except Exception as e:  # noqa: BLE001
            print("Social run crashed: " + safe_exc(e))
            return 1
    print("Usage: python -m agent selftest | once | forever | social")
    return 2


if __name__ == "__main__":
    sys.exit(main())
