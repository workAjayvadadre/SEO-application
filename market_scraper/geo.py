"""Territories for campaign planning: India > zone > state > district > sub-district (tehsil/taluka) > village.

Zones, states, districts and sub-districts come from ui/geo.json (built from the Local Government
Directory by scripts/build_geo.py). Villages are free text: they are validated only for characters and
researched by name together with their sub-district, district and state.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from .regional import resolve_place

GEO_FILE = Path(__file__).resolve().parent.parent / "ui" / "geo.json"
LEVELS = ("india", "zone", "state", "district", "subdistrict", "village")
NAME_OK = re.compile(r"^[A-Za-z][A-Za-z0-9 .,&'()/-]{0,79}$")


@lru_cache(maxsize=1)
def geo() -> dict[str, Any]:
    return json.loads(GEO_FILE.read_text(encoding="utf-8"))


class TerritoryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Territory:
    zone: str | None = None
    state: str | None = None
    district: str | None = None
    subdistrict: str | None = None
    village: str | None = None
    states: tuple[str, ...] = field(default_factory=tuple)  # every state the territory covers

    @property
    def level(self) -> str:
        for level, value in (("village", self.village), ("subdistrict", self.subdistrict),
                             ("district", self.district), ("state", self.state), ("zone", self.zone)):
            if value:
                return level
        return "india"

    @property
    def label(self) -> str:
        """Most specific first: 'Saket, South Delhi, Delhi'."""
        parts = [self.village, self.subdistrict, self.district, self.state] if self.state else [self.zone or "India"]
        out: list[str] = []
        for p in parts:
            if p and (not out or p.lower() not in out[-1].lower()):
                out.append(p)
        return ", ".join(out) or "India"

    @property
    def short_name(self) -> str:
        return self.village or self.subdistrict or self.district or self.state or self.zone or "India"

    @property
    def google_location(self) -> str | None:
        """Canonical Google Ads / SERP location. City when we know one, else the state."""
        if not self.state:
            return None
        for candidate in (self.subdistrict, self.district):
            place = resolve_place(candidate) if candidate else {}
            if place.get("city") and place.get("state") == self.state:
                return f"{place['city']},{self.state},India"
        return f"{self.state},India"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "states": list(self.states), "level": self.level, "label": self.label,
                "short_name": self.short_name, "google_location": self.google_location}


def _match(name: str | None, options: list[str] | dict[str, Any], what: str) -> str | None:
    if not name:
        return None
    if not NAME_OK.match(name):
        raise TerritoryError(f"{what} has unsupported characters")
    lookup = {o.lower(): o for o in options}
    found = lookup.get(name.strip().lower())
    if not found:
        raise TerritoryError(f"Unknown {what}: {name}")
    return found


def territory_from(payload: dict[str, Any] | None) -> Territory:
    """Validate a {zone, state, district, subdistrict, village} selection against the LGD hierarchy."""
    payload = payload or {}
    g = geo()
    zone = _match(payload.get("zone"), g["zones"], "zone")
    state = _match(payload.get("state"), g["states"], "state")
    if state and zone and state not in g["zones"][zone]:
        raise TerritoryError(f"{state} is not in {zone}")
    if state and not zone:
        zone = next((z for z, states in g["zones"].items() if state in states), None)
    district = _match(payload.get("district"), g["districts"].get(state or "", []), "district") \
        if payload.get("district") else None
    if district and not state:
        raise TerritoryError("pick a state before a district")
    sub = _match(payload.get("subdistrict"), g["subdistricts"].get(f"{state}|{district}", []), "sub-district") \
        if payload.get("subdistrict") else None
    if sub and not district:
        raise TerritoryError("pick a district before a sub-district")
    village = (payload.get("village") or "").strip() or None
    if village:
        if not NAME_OK.match(village):
            raise TerritoryError("village name has unsupported characters")
        if not district:
            raise TerritoryError("pick at least a district for a village")
    states = (state,) if state else tuple(g["zones"][zone]) if zone else tuple(g["states"])
    return Territory(zone, state, district, sub, village, states)
