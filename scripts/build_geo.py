"""Build ui/geo.json (zone -> state -> district -> sub-district) from the Local Government Directory.

Source: LGD open-data mirror https://ramseraph.github.io/opendata/lgd (metadata.json lists the current
parquet files and their sha256). Sub-districts are tehsils / talukas / mandals / circles depending on the
state. Villages are not bundled (~6.8 lakh rows); the planner accepts any typed village name.

Usage (pyarrow is only needed for this build step, not by the app):
    pip install pyarrow
    python scripts/build_geo.py states.parquet districts.parquet subdistricts.parquet ui/geo.json
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict

import pyarrow.parquet as pq

# Conventional zonal grouping used in Indian marketing plans.
ZONES = {
    "North India": ["Jammu and Kashmir", "Ladakh", "Himachal Pradesh", "Punjab", "Chandigarh", "Haryana", "Delhi",
                    "Uttarakhand", "Uttar Pradesh", "Rajasthan"],
    "South India": ["Andhra Pradesh", "Telangana", "Karnataka", "Kerala", "Tamil Nadu", "Puducherry", "Lakshadweep"],
    "East India": ["Bihar", "Jharkhand", "Odisha", "West Bengal", "Andaman and Nicobar Islands"],
    "North-East India": ["Assam", "Arunachal Pradesh", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Sikkim",
                         "Tripura"],
    "West India": ["Gujarat", "Maharashtra", "Goa", "Dadra and Nagar Haveli and Daman and Diu"],
    "Central India": ["Madhya Pradesh", "Chhattisgarh"],
}
_SMALL = {"and", "of", "the"}


def tidy(name: str) -> str:
    """'Jammu And Kashmir' -> 'Jammu and Kashmir'; 'NCT OF DELHI' style names are mapped below."""
    words = name.strip().split()
    return " ".join(w.lower() if i and w.lower() in _SMALL else w for i, w in enumerate(words))


_ALIASES = {"The Dadra and Nagar Haveli and Daman and Diu": "Dadra and Nagar Haveli and Daman and Diu",
            "NCT of Delhi": "Delhi", "National Capital Territory of Delhi": "Delhi"}


def state_name(raw: str) -> str:
    name = tidy(raw)
    return _ALIASES.get(name, name)


def main(states_path: str, districts_path: str, subdistricts_path: str, out_path: str) -> None:
    states = {r["state_code"]: (state_name(r["state_name"]), r["state_or_ut"])
              for r in pq.read_table(states_path).to_pylist()}
    districts: dict[str, list[str]] = defaultdict(list)
    for r in pq.read_table(districts_path).to_pylist():
        districts[states[r["state_code"]][0]].append(tidy(r["district_name"]))
    subs: dict[str, list[str]] = defaultdict(list)
    for r in pq.read_table(subdistricts_path).to_pylist():
        subs[f"{states[r['state_code']][0]}|{tidy(r['district_name'])}"].append(tidy(r["subdistrict_name"]))

    names = {n for n, _ in states.values()}
    zoned = {s for z in ZONES.values() for s in z}
    missing = sorted(names - zoned)
    unknown = sorted(zoned - names)
    if missing or unknown:
        raise SystemExit(f"zone map out of date. not zoned: {missing}; not in LGD: {unknown}")

    out = {
        "source": "Local Government Directory (LGD), via ramseraph.github.io/opendata/lgd",
        "zones": ZONES,
        "states": {n: {"type": "UT" if t == "U" else "State"} for n, t in sorted(states.values())},
        "districts": {s: sorted(set(d)) for s, d in sorted(districts.items())},
        "subdistricts": {k: sorted(set(v)) for k, v in sorted(subs.items())},
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(out['states'])} states/UTs, {sum(map(len, out['districts'].values()))} districts, "
          f"{sum(map(len, out['subdistricts'].values()))} sub-districts -> {out_path}")


if __name__ == "__main__":
    main(*sys.argv[1:5])
