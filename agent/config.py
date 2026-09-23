"""Reads your settings.env file (or environment variables)."""
from __future__ import annotations
import os
import re
from pathlib import Path

SECRET_KEYS = [
    "AGENT_API_TOKEN", "AUDIT_API_TOKEN", "EXPLORIUM_API_KEY", "WP_APP_PASSWORD", "WP_AGENT_KEY", "WP_URL", "WP_USER",
    "SLACK_AUDIT_LOG_URL", "SLACK_LEADS_URL", "SLACK_AGENT_URL",
    "PORTAL_URL", "AUDIT_URL", "BUSINESS_POSTAL_ADDRESS", "COMPANIES_HOUSE_API_KEY", "BUSINESS_PHONE", "BUSINESS_EMAIL",
    "GSC_SERVICE_ACCOUNT_JSON", "GSC_SITE_URL", "INDEXNOW_KEY",
]


def parse_env_text(text: str) -> dict:
    """KEY=value lines. Ignores comments and the '<-- YOU FILL THIS' hints."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            continue
        val = re.split(r"\s*<--", val, maxsplit=1)[0].strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1].strip()
        out[key] = val
    return out


class Settings:
    def __init__(self, values: dict):
        self.v = values

    # ---- raw getters ----
    def get(self, key: str, default: str = "") -> str:
        val = os.environ.get(key) or self.v.get(key, "")
        return val.strip() if val and val.strip() else default

    def int(self, key: str, default: int) -> int:
        try:
            return int(float(self.get(key, str(default))))
        except ValueError:
            return default

    def float(self, key: str, default: float) -> float:
        try:
            return float(self.get(key, str(default)))
        except ValueError:
            return default

    def list(self, key: str, default: str = "") -> list[str]:
        return [x.strip() for x in self.get(key, default).split(",") if x.strip()]

    # ---- named settings ----
    @property
    def portal_url(self): return self.get("PORTAL_URL").rstrip("/")
    @property
    def agent_token(self): return self.get("AGENT_API_TOKEN")
    @property
    def audit_url(self): return self.get("AUDIT_URL")
    @property
    def audit_token(self): return self.get("AUDIT_API_TOKEN")
    @property
    def gemini_keys(self) -> list[str]:
        """Every Gemini key you gave: GEMINI_API_KEY, GEMINI_API_KEY_2 ... _20, or GEMINI_API_KEYS=a,b,c."""
        found = [self.get("GEMINI_API_KEY")] + [self.get(f"GEMINI_API_KEY_{i}") for i in range(2, 21)]
        found += [k.strip() for k in self.get("GEMINI_API_KEYS").split(",")]
        out: list[str] = []
        for k in found:
            if k and k not in out:
                out.append(k)
        return out
    @property
    def gemini_key(self):
        keys = self.gemini_keys
        return keys[0] if keys else ""
    @property
    def gemini_model(self): return self.get("GEMINI_MODEL", "auto")
    @property
    def explorium_key(self): return self.get("EXPLORIUM_API_KEY")
    @property
    def slack_audit(self): return self.get("SLACK_AUDIT_LOG_URL")
    @property
    def slack_leads(self): return self.get("SLACK_LEADS_URL") or self.slack_audit
    @property
    def slack_agent(self): return self.get("SLACK_AGENT_URL") or self.slack_audit
    @property
    def your_name(self): return self.get("YOUR_NAME", "Umair")
    @property
    def postal_address(self): return self.get("BUSINESS_POSTAL_ADDRESS")
    @property
    def business_phone(self): return self.get("BUSINESS_PHONE")
    @property
    def business_email(self): return self.get("BUSINESS_EMAIL")
    @property
    def gsc_service_account_json(self): return self.get("GSC_SERVICE_ACCOUNT_JSON")
    @property
    def gsc_site_url(self): return self.get("GSC_SITE_URL")
    @property
    def indexnow_key(self): return self.get("INDEXNOW_KEY")
    @property
    def pitch(self):
        return self.get("BUSINESS_PITCH",
                        "I help local businesses get more customers from Google and Meta ads, tracking and SEO")
    @property
    def language(self): return self.get("MESSAGE_LANGUAGE", "English")
    @property
    def own_website(self): return self.get("OWN_WEBSITE", "https://umairconsult.com")
    @property
    def timezone(self): return self.get("TIMEZONE", "Asia/Karachi")
    @property
    def brief_hour(self): return self.int("BRIEF_HOUR", 9)
    @property
    def run_minutes(self): return self.int("RUN_MINUTES", 40)
    @property
    def daily_target(self): return self.int("DAILY_OUTREACH_TARGET", 125)
    @property
    def start_target(self): return self.int("START_OUTREACH_PER_DAY", 25)
    @property
    def warmup_days(self): return max(1, self.int("WARMUP_DAYS", 21))
    @property
    def max_new_leads(self): return self.int("MAX_NEW_LEADS_PER_DAY", 200)
    @property
    def max_audits(self): return self.int("MAX_AUDITS_PER_DAY", 300)
    @property
    def audit_pause(self): return self.float("SECONDS_BETWEEN_AUDITS", 5)
    @property
    def countries(self): return [c.upper() for c in self.list("TARGET_COUNTRIES", "US,GB,CA,AU")]
    @property
    def blocked_niches(self): return [x.lower() for x in self.list("BLOCKED_NICHES")]
    @property
    def restricted_niches(self): return [x.lower() for x in self.list("RESTRICTED_NICHES")]
    @property
    def priority_niches(self): return [x.lower() for x in self.list("PRIORITY_NICHES")]
    @property
    def channel_caps(self):
        return {
            "email": self.int("MAX_EMAIL_PER_DAY", 60),
            "whatsapp": self.int("MAX_WHATSAPP_PER_DAY", 25),
            "linkedin": self.int("MAX_LINKEDIN_PER_DAY", 25),
            "messenger": self.int("MAX_MESSENGER_PER_DAY", 15),
        }

    def secret_values(self) -> list[str]:
        vals = [self.get(k) for k in SECRET_KEYS] + self.gemini_keys
        return [v for v in vals if v]

    # ---- newer settings ----
    @property
    def max_backlog(self): return self.int("MAX_UNAUDITED_BACKLOG", 60)
    @property
    def overture_on(self): return self.get("USE_OVERTURE", "yes").lower() not in ("no", "false", "0", "off")
    @property
    def blog_per_week(self): return self.int("BLOG_POSTS_PER_WEEK", 2)
    @property
    def blog_voice(self):
        return self.get("BLOG_VOICE", "conversational, practical and direct, like an experienced consultant explaining things to a busy business owner")
    @property
    def wp_url(self): return self.get("WP_URL").rstrip("/")
    @property
    def wp_user(self): return self.get("WP_USER")
    @property
    def wp_password(self): return self.get("WP_APP_PASSWORD")
    @property
    def seo_pages_per_run(self): return self.int("SEO_PAGES_PER_RUN", 3)
    @property
    def seo_auto_apply(self): return self.get("SEO_AUTO_APPLY", "no").lower() in ("yes", "true", "1", "on")
    @property
    def wp_agent_key(self): return self.get("WP_AGENT_KEY")
    @property
    def wp_mode(self): return "draft" if self.get("WP_PUBLISH_MODE", "publish").lower() == "draft" else "publish"

    # ---- extra lead sources / signals ----
    @property
    def companies_house_key(self): return self.get("COMPANIES_HOUSE_API_KEY")
    @property
    def use_registries(self): return self.get("USE_REGISTRIES", "yes").lower() not in ("no", "false", "0", "off")
    @property
    def use_associations(self): return self.get("USE_ASSOCIATIONS", "yes").lower() not in ("no", "false", "0", "off")
    @property
    def use_hiring_signals(self): return self.get("USE_HIRING_SIGNALS", "yes").lower() not in ("no", "false", "0", "off")


def load_settings(path: str | None = None) -> Settings:
    """Load settings.env next to the project (or the path given)."""
    values: dict[str, str] = {}
    candidates = [Path(path)] if path else [Path("settings.env"), Path(__file__).resolve().parent.parent / "settings.env"]
    for p in candidates:
        if p.is_file():
            values = parse_env_text(p.read_text(encoding="utf-8", errors="ignore"))
            break
    return Settings(values)


def missing_required(s: Settings) -> list[str]:
    need = []
    if not s.portal_url: need.append("PORTAL_URL")
    if not s.agent_token: need.append("AGENT_API_TOKEN")
    return need
