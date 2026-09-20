"""Short, friendly follow-up drafts for leads you contacted a few days ago and who have not replied."""
from __future__ import annotations
import re

from .logutil import log, safe_exc
from .portal import Portal
from .writer import Gemini, _clean, compliance_footer, domain_of


def _prompt(lead: dict, cfg) -> str:
    n = int(lead.get("followup_count") or 0) + 1
    via = lead.get("contacted_via") or "email"
    return f"""You write brief, friendly follow-up messages for {cfg.your_name} of UmairConsult ({cfg.pitch}).
Context: the first message went to {_clean(lead.get('business_name'), 80)} ({_clean(lead.get('niche'), 40)}, website {_clean(domain_of(lead.get('website', '')), 60)}) by {via}, a few days ago. No reply yet. This is follow-up number {n} of at most 2.
FACTS you may use (from an automated website check): {_clean(lead.get('audit_summary'), 500)}
RULES: Do not repeat the first message. Add ONE new, useful angle taken from the facts. Never invent numbers or claim past contact beyond "my earlier message". Be polite, no pressure, no guilt, make it easy to say no. Write in {cfg.language}. No links.
email_body: at most 70 words, ends with "{cfg.your_name}" then "UmairConsult". whatsapp_message: at most 35 words.
Return ONLY JSON: {{"email_body": "...", "whatsapp_message": "..."}}"""


def template(lead: dict, cfg) -> dict:
    name = _clean(lead.get("business_name"), 60) or "there"
    dom = domain_of(lead.get("website", ""))
    return {
        "email_body": (f"Hi {name} team,\n\nJust a quick follow-up on my note about {dom}. I'm happy to send the short free report "
                       f"if it's useful, and no problem at all if the timing isn't right.\n\n{cfg.your_name}\nUmairConsult"),
        "whatsapp_message": f"Hi {name} team, quick follow-up on my earlier message about {dom}. Happy to send the short free report if useful.",
    }


def run_followups(cfg, portal: Portal, gemini: Gemini | None, deadline) -> dict:
    out = {"written": 0, "ai": 0}
    while not deadline.over():
        leads = portal.leads_to_followup(limit=10)
        if not leads:
            break
        for lead in leads:
            if deadline.over():
                break
            msgs, ai = None, False
            if gemini and not gemini.exhausted:
                try:
                    c = gemini.generate_json(_prompt(lead, cfg), max_tokens=600)
                    if isinstance(c.get("email_body"), str) and isinstance(c.get("whatsapp_message"), str) \
                            and "http" not in (c["email_body"] + c["whatsapp_message"]).lower():
                        msgs, ai = {"email_body": c["email_body"].strip(), "whatsapp_message": c["whatsapp_message"].strip()}, True
                except Exception as e:  # noqa: BLE001
                    log(f"Follow-up AI skipped: {safe_exc(e, 100)}")
            msgs = msgs or template(lead, cfg)
            msgs["email_body"] = msgs["email_body"].rstrip() + "\n" + compliance_footer(cfg)
            msgs["whatsapp_message"] = msgs["whatsapp_message"].rstrip() + " (Reply STOP and I won't message again.)"
            portal.save_followup(id=int(lead["id"]), **msgs)
            out["written"] += 1
            out["ai"] += 1 if ai else 0
    if out["written"]:
        portal.log("followup", f"Prepared {out['written']} follow-up messages for leads who haven't replied")
    return out
