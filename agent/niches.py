"""Business types the agent can find, and how to recognise them in each free data source.

  osm  = OpenStreetMap tags        ov = words that appear in Overture Maps categories
Any niche is allowed EXCEPT the ones in BLOCKED_NICHES (things Google/Meta ads prohibit) - never searched.
"""
import re

_N = {}


def _n(name, osm=(), ov=()):
    _N[name] = {"osm": list(osm), "ov": list(ov)}


_n("dentist", ['["amenity"="dentist"]', '["healthcare"="dentist"]'], ["dentist", "dental", "orthodont"])
_n("lawyer", ['["office"="lawyer"]'], ["lawyer", "attorney", "law_firm", "legal_services"])
_n("real estate", ['["office"="estate_agent"]'], ["real_estate_agent", "real_estate_service", "realtor", "real_estate_broker"])
_n("plumber", ['["craft"="plumber"]'], ["plumb"])
_n("electrician", ['["craft"="electrician"]'], ["electrician", "electrical_service"])
_n("roofing", ['["craft"="roofer"]'], ["roof"])
_n("hvac", ['["craft"="hvac"]'], ["hvac", "heating_and_air", "air_conditioning", "heating_contractor"])
_n("med spa", ['["shop"="beauty"]', '["leisure"="spa"]'], ["med_spa", "medical_spa", "day_spa", "skin_care", "beauty_salon"])
_n("chiropractor", ['["healthcare"="chiropractor"]', '["healthcare:speciality"="chiropractic"]'], ["chiropract"])
_n("physiotherapist", ['["healthcare"="physiotherapist"]'], ["physical_therapy", "physiotherap"])
_n("restaurant", ['["amenity"="restaurant"]'], ["restaurant"])
_n("gym", ['["leisure"="fitness_centre"]'], ["gym", "fitness_center", "fitness_studio"])
_n("cleaning", ['["office"="cleaning"]', '["craft"="cleaning"]'], ["cleaning", "janitorial", "maid_service"])
_n("landscaping", ['["craft"="gardener"]'], ["landscap", "gardener", "lawn_service"])
_n("accountant", ['["office"="accountant"]'], ["accountant", "accounting", "tax_prep"])
_n("architect", ['["office"="architect"]'], ["architect"])
_n("insurance", ['["office"="insurance"]'], ["insurance_agent", "insurance_broker"])
_n("veterinary", ['["amenity"="veterinary"]'], ["veterin", "animal_hospital"])
_n("doctor", ['["amenity"="doctors"]'], ["family_practice", "general_practitioner", "physician", "doctor"])
_n("optician", ['["shop"="optician"]'], ["optometr", "optician", "eye_care"])
_n("hairdresser", ['["shop"="hairdresser"]'], ["hair_salon", "hairdresser", "barber"])
_n("car repair", ['["shop"="car_repair"]'], ["auto_repair", "car_repair", "mechanic", "automotive_repair"])
_n("car dealer", ['["shop"="car"]'], ["car_dealer", "auto_dealer"])
_n("photographer", ['["craft"="photographer"]'], ["photograph"])
_n("painter", ['["craft"="painter"]'], ["painter", "painting_contractor"])
_n("carpenter", ['["craft"="carpenter"]'], ["carpent"])
_n("hotel", ['["tourism"="hotel"]'], ["hotel"])
_n("driving school", ['["amenity"="driving_school"]'], ["driving_school"])
_n("furniture store", ['["shop"="furniture"]'], ["furniture_store"])
_n("florist", ['["shop"="florist"]'], ["florist"])
# extra niches (mainly found through Overture Maps)
_n("locksmith", ['["craft"="locksmith"]'], ["locksmith"])
_n("pest control", [], ["pest_control", "exterminat"])
_n("moving company", [], ["moving_company", "mover"])
_n("auto detailing", [], ["car_wash", "auto_detail"])
_n("tutoring", [], ["tutor", "test_prep", "educational_service"])
_n("daycare", ['["amenity"="childcare"]'], ["day_care", "child_care", "preschool"])
_n("pet grooming", [], ["pet_groom"])
_n("solar installer", [], ["solar"])
_n("home inspector", [], ["home_inspect"])
_n("mortgage broker", [], ["mortgage"])
_n("wedding planner", [], ["wedding_planner", "wedding_service"])
_n("event venue", [], ["event_space", "banquet", "wedding_venue"])
_n("martial arts", [], ["martial_arts", "karate", "taekwondo", "jiu_jitsu"])
_n("yoga studio", [], ["yoga", "pilates"])
_n("tattoo shop", [], ["tattoo"])
_n("dermatologist", [], ["dermatolog"])
_n("plastic surgeon", [], ["plastic_surg", "cosmetic_surg"])
_n("urgent care", [], ["urgent_care"])
_n("general contractor", [], ["general_contractor", "remodel", "home_builder"])
_n("flooring", [], ["flooring"])
_n("garage door", [], ["garage_door"])
_n("pool service", [], ["pool_cleaning", "swimming_pool_contractor"])
_n("tree service", [], ["tree_service", "arborist"])
_n("towing", [], ["towing"])

NICHES = _N   # name -> {"osm": [...], "ov": [...]}

# Niches with no free source yet (agent skips them quietly).
NO_FREE_SOURCE = {"e-commerce", "ecommerce", "saas", "software"}


def osm_filters(niche: str) -> list:
    return NICHES.get(niche, {}).get("osm", [])


def ov_keywords(niche: str) -> list:
    return NICHES.get(niche, {}).get("ov", [])


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def word_match(text: str, words):
    """Return the first blocked/restricted word found in text (whole-word match)."""
    t = " " + _norm(text) + " "
    for w in words:
        w = _norm(w)
        if w and (" " + w + " ") in t:
            return w
    return None


def usable_niches(blocked, priority, overture: bool = False):
    """(all searchable niche names, the priority ones). Blocked niches are removed."""
    names = [n for n, v in NICHES.items()
             if not word_match(n, blocked) and (v["osm"] or (overture and v["ov"]))]
    pri = [n for n in names if any(p in n or n in p for p in priority)] if priority else []
    return names, pri
