"""Visits a business website (politely) and pulls out public contact details."""
from __future__ import annotations
import html as htmlmod
import ipaddress
import re
import socket
import threading
import urllib.robotparser
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

UA = "Mozilla/5.0 (compatible; UmairConsultAgent/1.0; +https://umairconsult.com)"
ROBOT_TOKEN = "UmairConsultAgent"
MAX_BYTES = 1_500_000

FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.fr", "yahoo.de", "yahoo.es", "yahoo.it",
    "hotmail.com", "hotmail.co.uk", "hotmail.fr", "outlook.com", "live.com", "msn.com", "icloud.com", "me.com",
    "aol.com", "proton.me", "protonmail.com", "gmx.com", "gmx.de", "gmx.net", "web.de", "mail.com", "yandex.com",
    "yandex.ru", "zoho.com", "btinternet.com", "sky.com", "talktalk.net", "virginmedia.com", "orange.fr",
    "wanadoo.fr", "free.fr", "t-online.de", "libero.it", "sbcglobal.net", "comcast.net", "verizon.net", "att.net",
}
JUNK_DOMAINS = {"example.com", "domain.com", "email.com", "yoursite.com", "yourdomain.com", "test.com",
                "sentry.io", "wixpress.com", "sentry-next.wixpress.com", "sentry.wixpress.com", "2x.png"}
JUNK_LOCAL = {"noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster", "abuse",
              "webmaster", "privacy", "dpo", "unsubscribe", "root", "hostmaster"}
FILE_TLDS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "css", "js", "ico", "pdf", "woff", "woff2"}
ROLE_ORDER = ["info", "contact", "hello", "office", "enquiries", "enquiry", "sales", "reception",
              "booking", "bookings", "appointments", "mail", "admin", "team", "service", "kontakt"]
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")

DIAL = {"US": "1", "CA": "1", "GB": "44", "IE": "353", "AU": "61", "DE": "49", "FR": "33", "ES": "34", "IT": "39",
        "NL": "31", "BE": "32", "SE": "46", "DK": "45", "NO": "47", "FI": "358", "AT": "43", "CH": "41",
        "PT": "351", "PL": "48"}
STRIP_TRUNK_ZERO = {"GB", "IE", "AU", "DE", "FR", "NL", "BE", "SE", "FI", "AT", "CH"}


# ------------------------------------------------------------------ phones
def normalize_phone(raw: str, country: str) -> str:
    """Return +E164 style number, or '' if it doesn't look valid."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if digits.startswith("00") and not plus:
        plus, digits = True, digits[2:]
    if not plus:
        cc = DIAL.get(country.upper())
        if not cc:
            return ""
        if country.upper() in ("US", "CA"):
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            if len(digits) != 10:
                return ""
            digits = "1" + digits
        else:
            if country.upper() in STRIP_TRUNK_ZERO and digits.startswith("0"):
                digits = digits[1:]
            elif digits.startswith(cc) and len(digits) > len(cc) + 6:
                pass  # already has the country code without '+'
            else:
                digits = cc + digits
            if not digits.startswith(cc):
                digits = cc + digits
    if not (8 <= len(digits) <= 15):
        return ""
    return "+" + digits


# ------------------------------------------------------------------ emails
def decode_cfemail(hexstr: str) -> str:
    try:
        r = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ r) for i in range(2, len(hexstr), 2))
    except (ValueError, IndexError):
        return ""


def clean_email(e: str) -> str:
    e = htmlmod.unescape(e).strip().strip(".,;:<>()[]\"'").lower()
    if not EMAIL_RE.fullmatch(e):
        return ""
    local, _, domain = e.rpartition("@")
    tld = domain.rsplit(".", 1)[-1]
    if tld in FILE_TLDS or domain in JUNK_DOMAINS or domain in FREE_MAIL:
        return ""
    if local in JUNK_LOCAL or len(local) > 40 or local.startswith("u00"):
        return ""
    return e


def pick_best_email(emails: list[str], site_domain: str) -> str:
    cands = []
    for e in dict.fromkeys(emails):  # unique, keep order
        ce = clean_email(e)
        if not ce:
            continue
        local, _, dom = ce.rpartition("@")
        same = dom == site_domain or dom.endswith("." + site_domain) or site_domain.endswith("." + dom)
        role = ROLE_ORDER.index(local) if local in ROLE_ORDER else 50
        cands.append((0 if same else 1, role, ce))
    cands.sort()
    return cands[0][2] if cands else ""


# ------------------------------------------------------------------ html parsing
class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.cf: list[str] = []
        self.text: list[str] = []
        self._href = None
        self._label: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        if tag == "a" and a.get("href"):
            self._href, self._label = a["href"], []
        if a.get("data-cfemail"):
            self.cf.append(a["data-cfemail"])

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._label).strip()))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        self.text.append(data)
        if self._href is not None:
            self._label.append(data)


def _clean_social(url: str, kind: str) -> str:
    try:
        p = urlparse(url)
    except ValueError:
        return ""
    host = (p.netloc or "").lower()
    for pre in ("www.", "m."):
        if host.startswith(pre):
            host = host[len(pre):]
    path = p.path.rstrip("/")
    if kind == "facebook":
        if not (host.endswith("facebook.com") or host == "fb.com"):
            return ""
        if any(x in path.lower() for x in ("/sharer", "/share", "/plugins", "/dialog", "/tr", "/login", "/events", "/photo", "/posts", "/watch", "/groups")) or path in ("", "/"):
            return ""
        return f"https://www.facebook.com{path}" + (f"?{p.query}" if "profile.php" in path and p.query else "")
    if kind == "instagram":
        if not host.endswith("instagram.com") or path in ("", "/") or path.startswith(("/p/", "/reel/", "/explore", "/accounts")):
            return ""
        return f"https://www.instagram.com{path}"
    if kind == "linkedin":
        if not host.endswith("linkedin.com") or not path.startswith("/company/"):
            return ""
        return f"https://www.linkedin.com{path}"
    return ""


def extract_contacts(html_text: str, base_url: str, country: str = "") -> dict:
    """Pull emails, phones, social links and the contact page out of one HTML page."""
    pg = _Page()
    try:
        pg.feed(html_text[:MAX_BYTES])
    except Exception:  # broken HTML should never crash the agent
        pass
    site_domain = (urlparse(base_url).hostname or "").lower().replace("www.", "")
    emails: list[str] = []
    phones: list[str] = []
    social = {"facebook": "", "instagram": "", "linkedin": ""}
    contact_page = ""
    impressum = ""
    for href, label in pg.links:
        h = href.strip()
        low = h.lower()
        if low.startswith("mailto:"):
            emails.append(h[7:].split("?")[0])
            continue
        if low.startswith("tel:"):
            phones.append(h[4:])
            continue
        if low.startswith(("javascript:", "#", "data:")):
            continue
        absu = urljoin(base_url, h)
        for kind in social:
            if not social[kind]:
                social[kind] = _clean_social(absu, kind)
        pu = urlparse(absu)
        same_site = (pu.hostname or "").lower().replace("www.", "") == site_domain
        blob = (label + " " + pu.path).lower()
        if same_site and not contact_page and re.search(r"contact|kontakt|contacto|contatto|contactez|contato", blob):
            contact_page = absu.split("#")[0]
        if same_site and not impressum and re.search(r"impressum|mentions-legales|aviso-legal", blob):
            impressum = absu.split("#")[0]
    for cf in pg.cf:
        emails.append(decode_cfemail(cf))
    emails += EMAIL_RE.findall(htmlmod.unescape(" ".join(pg.text)))
    email = pick_best_email(emails, site_domain)
    phone = ""
    for p in phones:
        phone = normalize_phone(p, country)
        if phone:
            break
    return {"email": email, "phone": phone, **{f"{k}_url": v for k, v in social.items()},
            "contact_page_url": contact_page, "impressum_url": impressum}


# ------------------------------------------------------------------ fetching
class Fetcher:
    def __init__(self, allow_private: bool = False, timeout: int = 10, respect_robots: bool = True):
        self.allow_private = allow_private
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"})
        self.s.max_redirects = 5
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def _is_public(self, url: str) -> bool:
        if self.allow_private:
            return True
        host = urlparse(url).hostname
        if not host:
            return False
        try:
            for info in socket.getaddrinfo(host, None):
                ip = ipaddress.ip_address(info[4][0])
                if not ip.is_global:
                    return False
        except (socket.gaierror, ValueError):
            return False
        return True

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlparse(url)
        key = f"{p.scheme}://{p.netloc}"
        with self._lock:
            known = key in self._robots
            rp = self._robots.get(key)
        if not known:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.s.get(key + "/robots.txt", timeout=6)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException:
                rp = None
            with self._lock:
                self._robots[key] = rp
        return True if rp is None else rp.can_fetch(ROBOT_TOKEN, url)

    def get(self, url: str) -> tuple[str, str] | None:
        """Return (final_url, html) or None if not allowed / unreachable."""
        if not re.match(r"^https?://", url, re.I):
            url = "http://" + url
        if not self._is_public(url) or not self.allowed(url):
            return None
        try:
            r = self.s.get(url, timeout=self.timeout, stream=True, allow_redirects=True)
            if r.status_code >= 400:
                return None
            ctype = r.headers.get("Content-Type", "").lower()
            if ctype and "html" not in ctype and "xml" not in ctype:
                return None
            if not self._is_public(r.url):
                return None
            body = b""
            for chunk in r.iter_content(65536):
                body += chunk
                if len(body) > MAX_BYTES:
                    break
            r.close()
            enc = r.encoding or "utf-8"
            return r.url, body.decode(enc, errors="ignore")
        except (requests.RequestException, LookupError):
            return None


def enrich_from_website(fetcher: Fetcher, website: str, country: str) -> dict:
    """Visit homepage (+ contact/impressum page if needed). Max 3 fetches."""
    res = {"reachable": False, "email": "", "phone": "", "facebook_url": "", "instagram_url": "",
           "linkedin_url": "", "contact_page_url": ""}
    first = fetcher.get(website)
    if not first:
        return res
    res["reachable"] = True
    final_url, html_text = first
    info = extract_contacts(html_text, final_url, country)
    for k in ("email", "phone", "facebook_url", "instagram_url", "linkedin_url", "contact_page_url"):
        res[k] = info.get(k, "")
    extra = []
    if not res["email"] or not res["phone"]:
        if info.get("contact_page_url"):
            extra.append(info["contact_page_url"])
        if info.get("impressum_url") and country.upper() in ("DE", "AT", "CH"):
            extra.append(info["impressum_url"])
    for page_url in extra[:2]:
        got = fetcher.get(page_url)
        if not got:
            continue
        more = extract_contacts(got[1], got[0], country)
        for k in ("email", "phone", "facebook_url", "instagram_url", "linkedin_url"):
            if not res[k] and more.get(k):
                res[k] = more[k]
        if res["email"] and res["phone"]:
            break
    return res
