"""Tiny helpers shared by several modules."""
import re


def domain_of(url: str) -> str:
    u = re.sub(r"^https?://", "", (url or "").strip(), flags=re.I)
    u = u.split("/")[0].split("?")[0].lower()
    return u[4:] if u.startswith("www.") else u
