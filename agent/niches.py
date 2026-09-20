"""Business types the agent can find for free on OpenStreetMap.

Each niche maps to OpenStreetMap tags. Any niche is allowed EXCEPT the ones in
BLOCKED_NICHES (things Google/Meta ads prohibit) - those are never searched.
"""
import re

NICHES = {
 "dentist": ['["amenity"="dentist"]', '["healthcare"="dentist"]'],
 "lawyer": ['["office"="lawyer"]'],
 "real estate": ['["office"="estate_agent"]'],
 "plumber": ['["craft"="plumber"]'],
 "electrician": ['["craft"="electrician"]'],
 "roofing": ['["craft"="roofer"]'],
 "hvac": ['["craft"="hvac"]'],
 "med spa": ['["shop"="beauty"]', '["leisure"="spa"]'],
 "chiropractor": ['["healthcare"="chiropractor"]', '["healthcare:speciality"="chiropractic"]'],
 "physiotherapist": ['["healthcare"="physiotherapist"]'],
 "restaurant": ['["amenity"="restaurant"]'],
 "gym": ['["leisure"="fitness_centre"]'],
 "cleaning": ['["office"="cleaning"]', '["craft"="cleaning"]'],
 "landscaping": ['["craft"="gardener"]'],
 "accountant": ['["office"="accountant"]'],
 "architect": ['["office"="architect"]'],
 "insurance": ['["office"="insurance"]'],
 "veterinary": ['["amenity"="veterinary"]'],
 "doctor": ['["amenity"="doctors"]'],
 "optician": ['["shop"="optician"]'],
 "hairdresser": ['["shop"="hairdresser"]'],
 "car repair": ['["shop"="car_repair"]'],
 "car dealer": ['["shop"="car"]'],
 "photographer": ['["craft"="photographer"]'],
 "painter": ['["craft"="painter"]'],
 "carpenter": ['["craft"="carpenter"]'],
 "hotel": ['["tourism"="hotel"]'],
 "driving school": ['["amenity"="driving_school"]'],
 "furniture store": ['["shop"="furniture"]'],
 "florist": ['["shop"="florist"]'],
}

# Niches with no free source yet (agent skips them quietly).
NO_FREE_SOURCE = {"e-commerce", "ecommerce", "saas", "software"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def word_match(text: str, words) -> str | None:
    """Return the first blocked/restricted word found in text (whole-word match)."""
    t = " " + _norm(text) + " "
    for w in words:
        w = _norm(w)
        if w and (" " + w + " ") in t:
            return w
    return None


def usable_niches(blocked, priority):
    """Niche names to search: everything mappable minus blocked. Priority first."""
    names = [n for n in NICHES if not word_match(n, blocked)]
    pri = [n for n in names if any(p in n or n in p for p in priority)] if priority else []
    return names, pri
