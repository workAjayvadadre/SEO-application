"""Regional context for content planning: state lookup, language, festivals and NMC claim checks.

* `resolve_place("Salt Lake Kolkata")` -> {"area": "Salt Lake", "city": "Kolkata", "state": "West Bengal"}
* `events_for(month, state)` -> festivals / observances / health days in that month for that state.
  Lunar-calendar dates carry `verify: True`: confirm them against the local panchang or
  the state's holiday list before publishing (they can shift by a day between regions).
* `nmc_flags(text)` -> claim patterns the NMC advertising guidelines (Public Notice
  R-13014/01/2024-Ethics, 06-10-2026) prohibit, with the clause each one breaks.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_REGIONS_FILE = Path(__file__).resolve().parent.parent / "ui" / "regions.json"

# ---------------------------------------------------------------- places


@lru_cache(maxsize=1)
def _regions() -> dict[str, Any]:
    try:
        return json.loads(_REGIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"states": [], "cities": {}, "areas": {}}


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


@lru_cache(maxsize=512)
def resolve_place(region: str | None) -> dict[str, str | None]:
    """Map a free-text region (state, city or "Area City") to area/city/state. Unknown -> all None."""
    out: dict[str, str | None] = {"area": None, "city": None, "state": None}
    if not region:
        return out
    data, needle = _regions(), _key(region)
    city_state = {_key(c): (c, s) for s, cities in data["cities"].items() for c in cities}
    city_state.setdefault("delhi", ("Delhi", "Delhi"))
    city_state.setdefault("chandigarh", ("Chandigarh", "Chandigarh"))
    for state in data["states"]:
        if _key(state) == needle:
            return {**out, "state": state}
    if needle in city_state:
        city, state = city_state[needle]
        return {**out, "city": city, "state": state}
    if needle in {"delhi ncr", "ncr"}:
        return {**out, "city": "Delhi NCR", "state": "Delhi"}
    for city, areas in data["areas"].items():
        ck = _key(city)
        for area in areas:
            ak = _key(area)
            if needle in {ak, f"{ak} {ck}"} or (ak.endswith(ck) and needle == ak):
                _, state = city_state.get(ck, (city, None))
                return {"area": area, "city": city, "state": state}
    # "ivf in Andheri Mumbai"-style input: last word(s) may be a known city.
    words = needle.split()
    for n in (2, 1):
        tail = " ".join(words[-n:])
        if len(words) > n and tail in city_state:
            city, state = city_state[tail]
            return {"area": " ".join(region.split()[:-n]), "city": city, "state": state}
    return out


# ---------------------------------------------------------------- languages

STATE_LANGUAGES: dict[str, list[str]] = {
    "Andhra Pradesh": ["Telugu"], "Arunachal Pradesh": ["English", "Hindi"], "Assam": ["Assamese", "Bengali"],
    "Bihar": ["Hindi", "Maithili", "Bhojpuri"], "Chhattisgarh": ["Hindi", "Chhattisgarhi"], "Goa": ["Konkani", "Marathi"],
    "Gujarat": ["Gujarati"], "Haryana": ["Hindi", "Haryanvi"], "Himachal Pradesh": ["Hindi"],
    "Jharkhand": ["Hindi", "Santali"], "Karnataka": ["Kannada"], "Kerala": ["Malayalam"],
    "Madhya Pradesh": ["Hindi"], "Maharashtra": ["Marathi", "Hindi"], "Manipur": ["Meitei (Manipuri)"],
    "Meghalaya": ["English", "Khasi", "Garo"], "Mizoram": ["Mizo"], "Nagaland": ["English", "Nagamese"],
    "Odisha": ["Odia"], "Punjab": ["Punjabi"], "Rajasthan": ["Hindi", "Rajasthani"], "Sikkim": ["Nepali"],
    "Tamil Nadu": ["Tamil"], "Telangana": ["Telugu", "Urdu"], "Tripura": ["Bengali", "Kokborok"],
    "Uttar Pradesh": ["Hindi", "Urdu"], "Uttarakhand": ["Hindi", "Garhwali", "Kumaoni"],
    "West Bengal": ["Bengali"], "Delhi": ["Hindi", "Punjabi", "Urdu"], "Jammu and Kashmir": ["Urdu", "Kashmiri", "Dogri"],
    "Ladakh": ["Ladakhi", "Hindi"], "Puducherry": ["Tamil", "French"], "Chandigarh": ["Hindi", "Punjabi"],
    "Andaman and Nicobar Islands": ["Hindi", "Bengali"], "Dadra and Nagar Haveli and Daman and Diu": ["Gujarati"],
    "Lakshadweep": ["Malayalam"],
}


def languages_for(state: str | None) -> list[str]:
    return STATE_LANGUAGES.get(state or "", ["Hindi"])


# ---------------------------------------------------------------- festivals & observances
# kind: "festival" (Class C greeting only), "national" (Class C), "health" (Class A awareness).
# states: None = all India. Fixed-date entries recur every year ("MM-DD"); lunar ones are per year.

_ALL = None
_N = ("Punjab", "Haryana", "Delhi", "Chandigarh", "Himachal Pradesh", "Jammu and Kashmir")
_HINDI_BELT = ("Uttar Pradesh", "Bihar", "Madhya Pradesh", "Rajasthan", "Delhi", "Haryana", "Uttarakhand",
               "Jharkhand", "Chhattisgarh", "Himachal Pradesh", "Chandigarh", "Gujarat", "Maharashtra", "Punjab")
_NE = ("Assam", "Arunachal Pradesh", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Sikkim", "Tripura")

FIXED: list[dict[str, Any]] = [
    {"md": "01-01", "name": "New Year's Day", "kind": "national", "states": _ALL},
    {"md": "01-13", "name": "Lohri", "kind": "festival", "states": _N},
    {"md": "01-14", "name": "Makar Sankranti", "kind": "festival", "states": _ALL, "verify": True},
    {"md": "01-14", "name": "Magh Bihu", "kind": "festival", "states": ("Assam",), "verify": True},
    {"md": "01-24", "name": "National Girl Child Day", "kind": "health", "states": _ALL},
    {"md": "01-26", "name": "Republic Day", "kind": "national", "states": _ALL},
    {"md": "02-04", "name": "World Cancer Day", "kind": "health", "states": _ALL},
    {"md": "03-08", "name": "International Women's Day", "kind": "health", "states": _ALL},
    {"md": "04-07", "name": "World Health Day", "kind": "health", "states": _ALL},
    {"md": "04-14", "name": "Baisakhi", "kind": "festival", "states": _N + ("Uttarakhand",)},
    {"md": "04-14", "name": "Rongali Bihu", "kind": "festival", "states": ("Assam",)},
    {"md": "04-14", "name": "Puthandu (Tamil New Year)", "kind": "festival", "states": ("Tamil Nadu", "Puducherry")},
    {"md": "04-14", "name": "Ambedkar Jayanti", "kind": "national", "states": _ALL},
    {"md": "04-15", "name": "Vishu", "kind": "festival", "states": ("Kerala",), "verify": True},
    {"md": "04-15", "name": "Pohela Boishakh", "kind": "festival", "states": ("West Bengal", "Tripura"), "verify": True},
    {"md": "05-01", "name": "Maharashtra Day", "kind": "national", "states": ("Maharashtra",)},
    {"md": "05-01", "name": "Gujarat Day", "kind": "national", "states": ("Gujarat",)},
    {"md": "05-28", "name": "Menstrual Hygiene Day", "kind": "health", "states": _ALL},
    {"md": "06-05", "name": "World Environment Day", "kind": "health", "states": _ALL},
    {"md": "06-21", "name": "International Day of Yoga", "kind": "health", "states": _ALL},
    {"md": "07-01", "name": "National Doctors' Day", "kind": "health", "states": _ALL},
    {"md": "07-11", "name": "World Population Day", "kind": "health", "states": _ALL},
    {"md": "07-25", "name": "World Embryologist Day", "kind": "health", "states": _ALL},
    {"md": "08-15", "name": "Independence Day", "kind": "national", "states": _ALL},
    {"md": "09-01", "name": "PCOS Awareness Month begins", "kind": "health", "states": _ALL},
    {"md": "10-02", "name": "Gandhi Jayanti", "kind": "national", "states": _ALL},
    {"md": "10-10", "name": "World Mental Health Day", "kind": "health", "states": _ALL},
    {"md": "11-01", "name": "Kannada Rajyotsava", "kind": "national", "states": ("Karnataka",)},
    {"md": "11-01", "name": "Kerala Piravi", "kind": "national", "states": ("Kerala",)},
    {"md": "11-14", "name": "World Diabetes Day", "kind": "health", "states": _ALL},
    {"md": "12-01", "name": "World AIDS Day", "kind": "health", "states": _ALL},
    {"md": "12-01", "name": "Hornbill Festival begins", "kind": "festival", "states": ("Nagaland",)},
    {"md": "12-12", "name": "Universal Health Coverage Day", "kind": "health", "states": _ALL},
    {"md": "12-19", "name": "Goa Liberation Day", "kind": "national", "states": ("Goa",)},
    {"md": "12-25", "name": "Christmas", "kind": "festival", "states": _ALL},
    {"md": "12-31", "name": "New Year's Eve", "kind": "national", "states": _ALL},
]

# Lunar / variable dates. Always `verify` (regional panchang and moon sighting can move them a day).
LUNAR: dict[str, list[dict[str, Any]]] = {
    "2026": [
        {"date": "2026-03-04", "name": "Holi", "states": _HINDI_BELT + ("West Bengal", "Odisha")},
        {"date": "2026-03-19", "name": "Gudi Padwa", "states": ("Maharashtra", "Goa")},
        {"date": "2026-03-19", "name": "Ugadi", "states": ("Karnataka", "Andhra Pradesh", "Telangana")},
        {"date": "2026-03-20", "name": "Eid al-Fitr", "states": _ALL},
        {"date": "2026-03-26", "name": "Ram Navami", "states": _HINDI_BELT},
        {"date": "2026-04-03", "name": "Good Friday", "states": _ALL},
        {"date": "2026-05-01", "name": "Buddha Purnima", "states": _ALL},
        {"date": "2026-05-27", "name": "Eid al-Adha (Bakrid)", "states": _ALL},
        {"date": "2026-07-16", "name": "Rath Yatra", "states": ("Odisha", "West Bengal", "Gujarat")},
        {"date": "2026-08-26", "name": "Onam (Thiruvonam)", "states": ("Kerala",)},
        {"date": "2026-08-28", "name": "Raksha Bandhan", "states": _HINDI_BELT},
        {"date": "2026-09-04", "name": "Janmashtami", "states": _ALL},
        {"date": "2026-09-14", "name": "Ganesh Chaturthi", "states": ("Maharashtra", "Goa", "Karnataka", "Telangana", "Andhra Pradesh", "Gujarat")},
        {"date": "2026-10-11", "name": "Navratri begins", "states": _HINDI_BELT},
        {"date": "2026-10-17", "name": "Durga Puja (Saptami)", "states": ("West Bengal", "Tripura", "Assam", "Odisha", "Jharkhand", "Bihar")},
        {"date": "2026-10-20", "name": "Dussehra / Vijaya Dashami", "states": _ALL},
        {"date": "2026-10-29", "name": "Karwa Chauth", "states": _N + ("Rajasthan", "Uttar Pradesh", "Madhya Pradesh")},
        {"date": "2026-11-06", "name": "Dhanteras", "states": _HINDI_BELT},
        {"date": "2026-11-08", "name": "Diwali", "states": _ALL},
        {"date": "2026-11-08", "name": "Kali Puja", "states": ("West Bengal", "Assam", "Tripura", "Odisha")},
        {"date": "2026-11-11", "name": "Bhai Dooj / Bhai Phonta", "states": _HINDI_BELT + ("West Bengal",)},
        {"date": "2026-11-15", "name": "Chhath Puja", "states": ("Bihar", "Jharkhand", "Uttar Pradesh", "Delhi")},
        {"date": "2026-11-24", "name": "Guru Nanak Jayanti", "states": _ALL},
    ],
    "2027": [
        {"date": "2027-01-15", "name": "Pongal", "states": ("Tamil Nadu", "Puducherry")},
        {"date": "2027-03-10", "name": "Eid al-Fitr", "states": _ALL},
        {"date": "2027-03-22", "name": "Holi", "states": _HINDI_BELT + ("West Bengal", "Odisha")},
        {"date": "2027-03-26", "name": "Good Friday", "states": _ALL},
    ],
}


def events_for(month: str, state: str | None) -> list[dict[str, Any]]:
    """Events in `month` ("YYYY-MM") relevant to `state` (None = all-India events only)."""
    year, mon = month.split("-")
    out = []

    def applies(states: tuple[str, ...] | None) -> bool:
        return states is None or (state is not None and state in states)

    for ev in FIXED:
        if ev["md"].startswith(mon) and applies(ev["states"]):
            out.append({"date": f"{year}-{ev['md']}", "name": ev["name"], "kind": ev["kind"],
                        "verify": bool(ev.get("verify")), "regional": ev["states"] is not None})
    for ev in LUNAR.get(year, []):
        if ev["date"][5:7] == mon and applies(ev["states"]):
            out.append({"date": ev["date"], "name": ev["name"], "kind": "festival", "verify": True,
                        "regional": ev["states"] is not None})
    out.sort(key=lambda e: (e["date"], not e["regional"]))
    return out


# ---------------------------------------------------------------- NMC claim checks
# Pattern -> (problem, clause). Keyword research may surface these searches; the content may not
# make the claim. The calendar keeps the keyword for targeting but rewrites the angle.

NMC_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\b(best|top|no\.?\s?1|number one|leading|most trusted|famous|unmatched|highest|lowest|cheapest)\b", re.I),
     "superlative / comparative claim", "8.1 iii, ix"),
    (re.compile(r"\bsuccess (rate|rates|ratio|percentage|stories|story)\b|\b\d+\s?%", re.I),
     "success-rate / outcome claim", "8.1 iii, vi; 7.2 a"),
    (re.compile(r"\b(guarantee[d]?|100 ?%|sure ?shot|miracle|painless|risk[- ]free)\b", re.I),
     "guarantee / unrealistic expectation", "8.1 iii, vi"),
    (re.compile(r"\b(testimonial|review|reviews|happy (families|couples)|before and after|celebrity)\b", re.I),
     "testimonial / endorsement", "6.2; 8.1 v, xi"),
    (re.compile(r"\b(free|offer|discount|cashback|coupon|emi|package deal|limited period)\b", re.I),
     "inducement / offer", "8.1 x"),
    (re.compile(r"\b(biological clock|before it'?s too late|last chance)\b", re.I),
     "fear-based messaging", "8.1 i"),
    (re.compile(r"\b(boy|girl|gender|son)\b.*\b(ivf|baby|pregnan|select)|\bsex selection\b", re.I),
     "sex-selection cue", "PC-PNDT Act"),
    (re.compile(r"\b(egg|sperm|embryo) donor (wanted|needed|required)|\bsurrogate (mother )?(wanted|needed|available)\b", re.I),
     "gamete / surrogate solicitation", "ART Act 2021; Surrogacy Act 2021"),
]


def nmc_flags(text: str) -> list[dict[str, str]]:
    return [{"issue": issue, "clause": clause, "match": m.group(0)}
            for pattern, issue, clause in NMC_RULES if (m := pattern.search(text or ""))]


_SUPERLATIVE = re.compile(r"\b(best|top|no\.?\s?1|number one|leading|most trusted|famous|unmatched|highest|"
                          r"lowest|cheapest|good|great)\b", re.I)
_OUTCOME = re.compile(r"\b(success (rates|rate|ratio|percentage|stories|story)\b|\d+\s?%)", re.I)
_OUTCOME_ANGLES = (
    "What Affects IVF Outcomes? Factors to Discuss With Your Doctor",
    "Age, Health and IVF: What Influences Results",
    "Questions to Ask Before Starting IVF",
    "Understanding IVF Results: Why Outcomes Vary From Person to Person",
    "Preparing for IVF: Tests, Timelines and What to Expect",
)
_SPECIALIST = re.compile(r"\b(doctor|specialist|gynecologist|gynaecologist)\b", re.I)
_PLACE_TYPE = re.compile(r"\b(centre|center|clinic|hospital)\b", re.I)


def _strip_claims(keyword: str) -> str:
    text = _OUTCOME.sub(" ", _SUPERLATIVE.sub(" ", keyword))
    text = re.sub(r"\b(which|who|is|the|of|for|in|with)\s*$", "", re.sub(r"\s+", " ", text).strip(), flags=re.I)
    return re.sub(r"^\s*(which|who|is|the)\b\s*", "", text, flags=re.I).strip()


def safe_angle(keyword: str, region: str | None = None, salt: str = "") -> str | None:
    """A compliant working title when the keyword carries a prohibited claim, else None.

    The claim word is removed and the rest of the keyword kept, so titles stay specific
    ("best ivf treatment in kolkata" -> "IVF Treatment in Kolkata: What to Expect").
    """
    if not nmc_flags(keyword):
        return None
    where = f" in {region}" if region and region.lower() not in keyword.lower() else ""
    vs = re.split(r"\s+(?:vs|versus)\s+", _strip_claims(keyword), maxsplit=1)
    if len(vs) == 2 and all(p.strip() for p in vs):
        a, b = (_title(p) for p in vs)
        return f"{a} vs {b}: How They Differ and Who Each May Suit"
    if _SPECIALIST.search(keyword) and _SUPERLATIVE.search(keyword):
        return f"How to Choose a Fertility Specialist{where}: Questions to Ask"
    if _PLACE_TYPE.search(keyword) and (_SUPERLATIVE.search(keyword) or _OUTCOME.search(keyword)):
        return f"Choosing a Fertility Clinic{where}: Registration, Facilities and Counselling to Check"
    if _OUTCOME.search(keyword):
        return _OUTCOME_ANGLES[sum(map(ord, keyword + salt)) % len(_OUTCOME_ANGLES)]
    if re.search(r"\b(free|offer|discount|emi|cheapest|lowest)\b", keyword, re.I):
        return f"Understanding IVF Costs{where}: What the Charges Include"
    if re.search(r"\bpainless\b", keyword, re.I):
        return "What Happens During Each IVF Step, and How Discomfort Is Managed"
    rest = _strip_claims(keyword)
    if len(rest.split()) >= 2 and not nmc_flags(rest):
        return f"{_title(rest)}: What to Expect"
    return f"IVF{where}: Reliable Answers to Common Questions"


def _title(text: str) -> str:
    small = {"in", "of", "for", "to", "and", "or", "vs", "with", "a", "an", "the", "at", "on"}
    words = text.strip().split()
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        if lw in {"ivf", "iui", "icsi", "pcos", "pcod", "amh", "hsg", "fsh", "imsi"}:
            out.append(lw.upper())
        elif i and lw in small:
            out.append(lw)
        else:
            out.append(lw[:1].upper() + lw[1:])
    return " ".join(out)
