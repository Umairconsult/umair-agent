"""Outreach email sending for the UmairConsult agent.

Builds the personalized cold-outreach HTML (ported from the Muse pipeline's
build_email.py / prep_lead_json.py) and sends it through Gmail SMTP.

Configuration (environment):
    GMAIL_APP_PASSWORD  - Google App Password for the Gmail account (required
                          to send; when missing, phase_send skips gracefully).
    GMAIL_USER          - Gmail account login, default umairppcmaster@gmail.com
    FROM_EMAIL          - From/Reply-To address, default hi@umairconsult.com

HARD RULE (user instruction 2026-10-03): an email is only ever sent for a lead
with a completed, successful website audit (audit_score present). No audit ->
no email, no exceptions. The scheduler enforces this before calling build().
"""
from __future__ import annotations

import html
import json
import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "email_template.html")
CALENDLY = "https://calendly.com/umairconsult/30min"
FROM_EMAIL = os.environ.get("FROM_EMAIL", "hi@umairconsult.com")
GMAIL_USER = os.environ.get("GMAIL_USER", "umairppcmaster@gmail.com")

NICHE_COPY = {
    "ecommerce":   ("e-commerce brands", "shoppers", "your products", "online stores"),
    "hvac":        ("home service companies", "homeowners", "your services", "home services"),
    "plumbing":    ("home service companies", "homeowners", "your services", "home services"),
    "roofing":     ("home service companies", "homeowners", "your services", "home services"),
    "electrical":  ("home service companies", "homeowners", "your services", "home services"),
    "remodeling":  ("home service companies", "homeowners", "your services", "home services"),
    "pest control":("home service companies", "homeowners", "your services", "home services"),
    "landscaping": ("home service companies", "homeowners", "your services", "home services"),
    "car repair":  ("auto service businesses", "drivers", "your services", "auto shops"),
    "saas":        ("SaaS companies", "buyers", "your platform", "software companies"),
    "leadgen":     ("lead generation businesses", "clients", "your offer", "service businesses"),
    "dental":      ("dental practices", "patients", "your treatments", "clinics"),
    "dentist":     ("dental practices", "patients", "your treatments", "clinics"),
    "clinic":      ("clinics", "patients", "your treatments", "clinics"),
    "med spa":     ("med spas", "clients", "your treatments", "clinics"),
    "chiropractor":("clinics", "patients", "your treatments", "clinics"),
    "yoga studio": ("studios", "members", "your classes", "studios"),
    "hairdresser": ("salons", "clients", "your services", "salons"),
    "florist":     ("local retailers", "customers", "your products", "shops"),
    "restaurant":  ("restaurants", "diners", "your menu", "restaurants"),
    "gym":         ("fitness businesses", "members", "your programs", "gyms"),
    "flooring":    ("home service companies", "homeowners", "your services", "home services"),
    "accounting":  ("professional service firms", "clients", "your services", "firms"),
    "legal":       ("law firms", "clients", "your services", "firms"),
    "consulting":  ("consulting firms", "clients", "your services", "firms"),
}


def niche_words(niche):
    n = (niche or "").lower()
    for k, v in NICHE_COPY.items():
        if k in n:
            return v
    return ("businesses like yours", "customers", "what you offer", "businesses")


FIX_MAP = [
    (r"no h1 heading", "Add one clear H1 per page describing the page's main topic. Google relies on it to understand what the page is about."),
    (r"no google analytics or google tag manager", "Install Google Tag Manager (free) with GA4 so every visit and conversion is measured. Without it, ad spend cannot be attributed to revenue."),
    (r"mobile page speed score needs improvement", "Compress images, defer unused JavaScript and enable caching. Page speed is a confirmed Google ranking factor and directly affects ad Quality Scores."),
    (r"critical security headers are missing", "Add security headers (CSP, HSTS, X-Frame-Options) at server level. They protect visitors and are a trust signal for Google."),
    (r"no meta description", "Write a unique 120\u2013155 character meta description for key pages. It controls the snippet shown in Google and lifts click-through rate."),
    (r"page title is too short", "Expand the title to 50\u201360 characters with the primary keyword near the front so it earns the click in search results."),
    (r"no page title", "Add a unique, descriptive page title (50\u201360 characters) to every page."),
    (r"no ssl certificate", "Install an SSL certificate (free via Let's Encrypt or Cloudflare). Browsers flag non-HTTPS sites, which kills conversions and ad approvals."),
    (r"no viewport meta tag", "Add the viewport meta tag so the site renders correctly on phones \u2014 most ad traffic is mobile."),
    (r"no cookie consent", "Add a consent banner (CookieYes or Cookiebot). Required under GDPR/CCPA once ad pixels are running."),
    (r"dmarc record missing", "Publish a DMARC record in DNS so your domain cannot be spoofed in phishing emails."),
    (r"dmarc policy is set to .none", "Tighten DMARC from p=none to p=quarantine so spoofed mail using your domain is actively blocked."),
    (r"no cdn detected", "Put Cloudflare's free plan in front of the site for caching and faster loads worldwide."),
    (r"missing alt text", "Add descriptive alt text to images \u2014 it helps accessibility and Google image search."),
    (r"no sitemap", "Generate an XML sitemap and submit it in Search Console so Google finds every page."),
    (r"gsc.*not verified|search console", "Verify the site in Google Search Console to see exactly how Google crawls and ranks it."),
]
DEFAULT_FIX = "Have your developer address this \u2014 it's a standard fix with a direct impact on performance or trust."


def fix_for(issue):
    low = issue.lower()
    for pat, fix in FIX_MAP:
        if re.search(pat, low):
            m = re.search(r"\((\d+)/100\)", issue)
            if "page speed" in pat and m:
                return fix.replace("Page speed is", f"At {m.group(1)}/100, page speed is")
            return fix
    return DEFAULT_FIX


def severity_for(issue):
    low = issue.lower()
    if "no ssl" in low or ("security headers" in low and "0/100" in low):
        return "Critical"
    return "High"


def parse_summary(summary):
    """Extract audit label, positives and issues from the portal audit_summary text."""
    s = summary or ""
    label = ""
    m = re.search(r"Site health \d+/100 \(([^)]+)\)", s)
    if m:
        label = m.group(1)
    mobile = None
    m = re.search(r"[Mm]obile (?:page )?speed[^\d()]*(\d+)/100", s)
    if m:
        mobile = int(m.group(1))
    platform = ""
    m = re.search(r"Platform:\s*([^·]+)", s)
    if m:
        platform = m.group(1).strip()
    gaps = ""
    m = re.search(r"Tracking/SEO gaps:\s*([^·]+)", s)
    if m:
        gaps = m.group(1)
    issues = []
    m = re.search(r"Top issues:\s*(.+)$", s, re.S)
    if m:
        for part in re.split(r"\.\s+", m.group(1).strip()):
            part = part.strip().rstrip(".")
            if part:
                issues.append({"issue": part})
    positives = []
    low = s.lower()
    if "no ssl" not in low:
        positives.append("SSL certificate in place")
    if "no schema markup" not in gaps.lower():
        positives.append("Structured data (schema) detected")
    if "no meta description" not in low:
        positives.append("Meta descriptions in place")
    if "no h1 heading" not in low:
        positives.append("Clear H1 heading structure")
    if "no viewport meta tag" not in low:
        positives.append("Mobile-friendly viewport configured")
    if "page title is too short" not in low and "no page title" not in low:
        positives.append("Page titles properly set")
    if "critical security headers are missing" not in low:
        positives.append("Security headers configured")
    if mobile is not None and mobile >= 80:
        positives.append(f"Strong mobile performance ({mobile}/100)")
    if platform and "unknown" not in platform.lower():
        positives.append(f"Built on {platform}")
    return {"audit_label": label or "Needs Attention", "positives": positives, "issues": issues}


def build_findings_block(domain, score, label, positives, issues):
    n = len(issues)
    if score >= 80:
        headline = f"Your site scores {score}/100 \u2014 here's how we scale it."
        intro = (f"I audited <strong style=\"color:#c0d0e0;font-weight:500;\">{domain}</strong> and it scored "
                 f"<strong style=\"color:#c99733;font-weight:600;\">{score}/100</strong> \u2014 solid. "
                 f"The {n} items below are what's standing between you and the next level of growth.")
    else:
        headline = f"I audited {domain} \u2014 here's the honest score."
        intro = (f"I ran a technical audit of <strong style=\"color:#c0d0e0;font-weight:500;\">{domain}</strong>: "
                 f"<strong style=\"color:#c99733;font-weight:600;\">{score}/100 ({html.escape(label)})</strong>. "
                 f"Below are the {n} highest-impact fixes \u2014 worth handling before you scale ad spend.")
    score_color = "#c99733" if score < 80 else "#7fd08a"

    pos_items = "".join(
        f"<tr><td style=\"padding:7px 0;font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;font-size:13.5px;color:#6a7f96;\">"
        f"<span style=\"color:#7fd08a;font-weight:600;\">\u2713</span>&nbsp;&nbsp;{html.escape(p)}</td></tr>"
        for p in positives[:4])

    issue_cards = ""
    for it in issues:
        sev = it.get("severity", "High")
        badge = "#e05252" if sev == "Critical" else "#c99733"
        issue_cards += (
            "<table role=\"presentation\" cellspacing=\"0\" cellpadding=\"0\" border=\"0\" width=\"100%\" style=\"margin-bottom:10px;\"><tr>"
            "<td style=\"background-color:#0f1219;border:1px solid #252d3d;border-radius:10px;padding:16px 18px;\">"
            f"<div style=\"display:inline-block;font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;font-size:10px;font-weight:600;"
            f"color:{badge};letter-spacing:1.5px;text-transform:uppercase;margin-bottom:8px;border:1px solid {badge}40;"
            f"border-radius:100px;padding:3px 10px;\">{sev} priority</div>"
            f"<div style=\"font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;font-size:13.5px;font-weight:600;color:#c8d8e8;margin-bottom:6px;\">{html.escape(it['issue'])}</div>"
            f"<div style=\"font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;font-size:12.5px;color:#5a7088;line-height:1.7;\"><span style=\"color:#b89040;font-weight:600;\">Fix: </span>{html.escape(it.get('fix', ''))}</div>"
            "</td></tr></table>")

    return f"""
  <!-- == YOUR WEBSITE AUDIT == -->
  <tr>
    <td style="background-color:#131720;padding:42px 48px;border-left:1px solid #252d3d;border-right:1px solid #252d3d;border-top:1px solid #1e2738;" class="padded">
      <div style="font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;font-size:10px;font-weight:600;color:#c99733;letter-spacing:2.5px;text-transform:uppercase;margin-bottom:12px;">Your Website Audit</div>
      <h2 style="font-family:'Playfair Display',Georgia,serif;font-size:23px;font-weight:700;color:#d4dde8;margin:0 0 14px;letter-spacing:-0.3px;">{headline}</h2>
      <p style="font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;font-size:14.5px;line-height:1.9;color:#6a7f96;margin:0 0 26px;">{intro}</p>

      <!-- Score hero -->
      <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="margin-bottom:26px;">
        <tr>
          <td align="center" style="background-color:#0f1219;border:1px solid #252d3d;border-radius:12px;padding:30px 20px;">
            <div style="font-family:'Playfair Display',Georgia,serif;font-size:64px;font-weight:800;color:{score_color};line-height:1;">{score}<span style="font-size:26px;color:#5a7088;">/100</span></div>
            <div style="font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;font-size:11px;color:#5a7088;letter-spacing:2px;text-transform:uppercase;margin-top:10px;">{html.escape(domain)} \u00b7 {html.escape(label)}</div>
          </td>
        </tr>
      </table>

      <!-- What's working -->
      <div style="font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;font-size:11px;font-weight:600;color:#7fd08a;letter-spacing:2px;text-transform:uppercase;margin-bottom:10px;">What&apos;s working</div>
      <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="margin-bottom:26px;">
        {pos_items if pos_items else '<tr><td style="font-family:\'IBM Plex Sans\',Helvetica,Arial,sans-serif;font-size:13px;color:#5a7088;">Site is live and reachable \u2014 a workable foundation to build on.</td></tr>'}
      </table>

      <!-- Priority fixes -->
      <div style="font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;font-size:11px;font-weight:600;color:#c99733;letter-spacing:2px;text-transform:uppercase;margin-bottom:10px;">Priority fixes ({n})</div>
      {issue_cards}
      <p style="font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;font-size:12.5px;line-height:1.8;color:#5a7088;margin:18px 0 0;">
        Fixing these is step one. Step two is making sure every dollar of ad spend lands on pages built to convert \u2014 that&apos;s what I do.
      </p>
    </td>
  </tr>
"""


def build_ads_block(domain, google_ads_count):
    """Build the 'Your Ad Activity' email section from Transparency Center data.

    google_ads_count: int or None (None = not checked yet; section is skipped).
    """
    if google_ads_count is None:
        return ""
    domain_esc = html.escape(domain)
    if google_ads_count > 0:
        count_txt = f"{google_ads_count}+" if google_ads_count >= 40 else str(google_ads_count)
        headline = f"You're running {count_txt} Google ads right now."
        body = (f"I took a look at your ads \u2014 you have <strong style=\"color:#c99733;font-weight:600;\">"
                f"{count_txt} active ads</strong> running right now. The question isn't whether you're spending, "
                f"it's whether every dollar is pulling its weight. Most businesses I audit are leaking 20-30% of ad "
                f"spend to pages that don't convert \u2014 the fixes above are where I'd start.")
    else:
        headline = "You're not running any Google ads right now."
        body = (f"I took a look at your ads \u2014 <strong style=\"color:#c99733;font-weight:600;\">nothing running</strong> "
                f"right now. Your competitors are buying the clicks you're not. The good news: starting from a clean slate "
                f"means we build it right the first time \u2014 no bad habits to undo, no wasted spend to unwind.")
    return f"""  <!-- == YOUR AD ACTIVITY == -->
  <tr>
    <td style="background-color:#131720;padding:42px 48px;border-left:1px solid #252d3d;border-right:1px solid #252d3d;" class="padded">
      <div style="font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;font-size:10px;font-weight:600;color:#c99733;letter-spacing:2.5px;text-transform:uppercase;margin-bottom:12px;">Your Ad Activity</div>
      <h2 style="font-family:'Playfair Display',Georgia,serif;font-size:23px;font-weight:700;color:#d4dde8;margin:0 0 14px;letter-spacing:-0.3px;">{headline}</h2>
      <p style="font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;font-size:14.5px;line-height:1.9;color:#6a7f96;margin:0;">{body}</p>
      <p style="font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;font-size:12.5px;line-height:1.8;color:#5a7088;margin:18px 0 0;">
        That's exactly what my Revenue Engine does \u2014 turns ad spend into predictable, scalable revenue. Worth a conversation?
      </p>
    </td>
  </tr>"""


def build_email(lead):
    """Build (subject, html) for a portal lead dict.

    The lead MUST carry a completed audit (audit_score + audit_summary) —
    callers enforce this; this function raises ValueError otherwise so an
    empty/generic email can never be built by accident.
    """
    score = lead.get("audit_score")
    summary = lead.get("audit_summary") or ""
    email = (lead.get("email") or "").strip()
    if not email:
        raise ValueError("refusing to build email: lead has no email address")
    if score is None or not summary.strip():
        raise ValueError("refusing to build email: lead has no completed audit")
    try:
        score = int(score)
    except (TypeError, ValueError):
        raise ValueError("refusing to build email: audit score is not a number")

    parsed = parse_summary(summary)
    label = parsed["audit_label"]
    positives = parsed["positives"]
    issues = parsed["issues"]
    for it in issues:
        it.setdefault("severity", severity_for(it["issue"]))
        it.setdefault("fix", fix_for(it["issue"]))
    if not issues:
        raise ValueError("refusing to build email: audit has no usable findings")

    name = lead.get("business_name") or ""
    website = lead.get("website") or ""
    domain = re.sub(r"^https?://(www\.)?", "", website).rstrip("/") or website
    domain_esc = html.escape(domain)

    with open(TEMPLATE_PATH, encoding="utf-8") as fh:
        tpl = fh.read()

    biz, buyers, offer, niche_plural = niche_words(lead.get("niche"))

    tpl = tpl.replace("HELLO", html.escape(name))

    hero_new = (f"I ran a technical audit of <strong style=\"color:#d0dcea;font-weight:500;\">{domain_esc}</strong> "
                f"\u2014 it scored <strong style=\"color:#c99733;font-weight:600;\">{score}/100</strong>, "
                f"and I spotted a few things worth fixing before you scale your ad spend. "
                f"I&apos;m <strong style=\"color:#d0dcea;font-weight:500;\">Muhammad Umair</strong>, a Certified Google Partner "
                f"and Performance Marketing Specialist. I help {biz} turn ad spend into predictable, scalable revenue.")
    # Stable placeholder {{HERO}} (template 2026-10-06+); fall back to the old
    # regex for templates that still carry the legacy sentence.
    if "{{HERO}}" in tpl:
        tpl = tpl.replace("{{HERO}}", hero_new)
    else:
        tpl = re.sub(r"I noticed you visited my website.*?predictable, scalable revenue\.",
                     lambda m: hero_new, tpl, flags=re.S)

    why_start = tpl.find("<!-- \u2550\u2550 WHY I'M REACHING OUT \u2550\u2550 -->")
    svc_start = tpl.find("<!-- \u2550\u2550 SERVICES \u2550\u2550 -->")
    if why_start != -1 and svc_start != -1 and svc_start > why_start:
        ads_html = build_ads_block(domain, lead.get("google_ads_count"))
        tpl = tpl[:why_start] + build_findings_block(domain_esc, score, label, positives, issues) + "\n" + ads_html + "\n" + tpl[svc_start:]

    tpl = tpl.replace("Performance marketing built for creative businesses.",
                      f"Performance marketing built for {biz}.")
    tpl = tpl.replace("Search, Display &amp; Performance Max targeting B2B clients searching for 3D animation.",
                      f"Search, Display &amp; Performance Max targeting {buyers} actively searching for {offer}.")
    tpl = tpl.replace("Visually-led campaigns showcasing your reel to brands that need your craft.",
                      f"Visually-led campaigns putting {offer} in front of {buyers} ready to buy.")
    tpl = tpl.replace("Re-engage warm visitors who've seen your portfolio but haven't reached out yet.",
                      f"Re-engage warm visitors who&apos;ve seen {offer} but haven&apos;t reached out yet.")
    tpl = tpl.replace("I Understand Creative Agencies",
                      f"I Understand {niche_plural.title()}")
    tpl = tpl.replace("I craft messaging that speaks to the real buyers of 3D animation \u2014 product brands, tech companies, e-commerce operators \u2014 in the language they respond to.",
                      f"I craft messaging that speaks to the real buyers of {niche_plural} \u2014 {buyers} with intent \u2014 in the language they respond to.")

    tpl = tpl.replace("https://umairconsult.com/contact/?source=email", CALENDLY)
    tpl = tpl.replace("Book Your Free Strategy Call \u2192", "Book Your Time Directly \u2192")
    tpl = tpl.replace("No pitch decks. No pressure. Just a real conversation about what's possible.",
                      "Pick a time that suits you \u2014 you&apos;ll get the calendar invite instantly.<br/>No pitch decks. No pressure. Just a real conversation about what&apos;s possible.")

    tpl = tpl.replace("You're receiving this because you visited umairconsult.com. This is a one-time personal outreach.",
                      f"This is a one-time personal outreach about {domain_esc}. If you&apos;d rather not hear from me, just reply and I won&apos;t contact you again.")

    n_high = sum(1 for it in issues if it.get("severity") in ("High", "Critical"))
    if score >= 80:
        subject = f"{domain}: your site scores {score}/100 \u2014 here's how we scale it"
    else:
        subject = f"{domain} audit: {score}/100 \u2014 {n_high} high-priority fixes inside"
    return subject, tpl


def send_email(to_email, subject, html_body, app_password, gmail_user=None, from_email=None, timeout=30):
    """Send one HTML email through Gmail SMTP. Returns True on success.

    app_password comes from the repo's SETTINGS_ENV (GMAIL_APP_PASSWORD) via
    the scheduler — never from chat, never logged.
    """
    gmail_user = gmail_user or GMAIL_USER
    from_email = from_email or FROM_EMAIL
    if not (app_password or "").strip():
        raise RuntimeError("GMAIL_APP_PASSWORD is not set — cannot send")
    msg = MIMEMultipart("alternative")
    msg["From"] = f"UmairConsult <{from_email}>"
    msg["To"] = to_email
    msg["Reply-To"] = from_email
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=timeout) as s:
        s.starttls()
        s.login(gmail_user, app_password.strip())
        s.sendmail(from_email, [to_email], msg.as_string())
    return True
