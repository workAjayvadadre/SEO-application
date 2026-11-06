"""Google Trends without an API key: interest over time, rising / top related searches, interest by region.

This uses the same JSON endpoints the trends.google.com page calls. It is unofficial: Google rate-limits
it (HTTP 429) and can change it without notice, so every failure becomes a readable TrendsError.
Values are relative (0-100, peak = 100 within the chosen place and period), not search counts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time as clock
from pathlib import Path
from typing import Any

import httpx

BASE = "https://trends.google.com"
TIMEFRAMES = {"day": "now 1-d", "week": "now 7-d", "month": "today 1-m", "quarter": "today 3-m",
              "year": "today 12-m", "5years": "today 5-y"}
STATE_CODES = {
    "Andaman and Nicobar Islands": "AN", "Andhra Pradesh": "AP", "Arunachal Pradesh": "AR", "Assam": "AS",
    "Bihar": "BR", "Chandigarh": "CH", "Chhattisgarh": "CT", "Delhi": "DL", "Goa": "GA", "Gujarat": "GJ",
    "Haryana": "HR", "Himachal Pradesh": "HP", "Jammu and Kashmir": "JK", "Jharkhand": "JH", "Karnataka": "KA",
    "Kerala": "KL", "Ladakh": "LA", "Lakshadweep": "LD", "Madhya Pradesh": "MP", "Maharashtra": "MH",
    "Manipur": "MN", "Meghalaya": "ML", "Mizoram": "MZ", "Nagaland": "NL", "Odisha": "OR", "Puducherry": "PY",
    "Punjab": "PB", "Rajasthan": "RJ", "Sikkim": "SK", "Tamil Nadu": "TN", "Telangana": "TG", "Tripura": "TR",
    "Uttar Pradesh": "UP", "Uttarakhand": "UT", "West Bengal": "WB",
}
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/130.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}


CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "trends"
CACHE_SECONDS = 12 * 3600
RETRY_DELAYS = (5.0, 20.0)  # polite waits after HTTP 429, unless Google sends Retry-After


class TrendsError(RuntimeError):
    pass


def geo_code(state: str | None) -> str:
    return f"IN-{STATE_CODES[state]}" if state in STATE_CODES else "IN"


def _json(text: str) -> Any:
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise TrendsError("Google Trends returned an unexpected page (likely blocked or rate-limited).")
    return json.loads(text[start:])


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> Any:
    for delay in (*RETRY_DELAYS, None):
        r = await client.get(f"{BASE}{path}", params=params)
        if r.status_code != 429:
            break
        if delay is None:
            raise TrendsError("Google Trends is rate-limiting requests right now. Wait a few minutes and try again.")
        retry_after = r.headers.get("Retry-After", "")
        await asyncio.sleep(min(float(retry_after), 60.0) if retry_after.isdigit() else delay)
    if r.status_code >= 400:
        raise TrendsError(f"Google Trends returned HTTP {r.status_code}.")
    return _json(r.text)


async def google_trends(keywords: list[str], state: str | None = None, timeframe: str = "year",
                        transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Up to 5 keywords compared in one place (India or one state) over one period."""
    keywords = [k.strip() for k in keywords if k.strip()][:5]
    if not keywords:
        raise TrendsError("Give at least one keyword.")
    geo, time = geo_code(state), TIMEFRAMES.get(timeframe, TIMEFRAMES["year"])
    cache = CACHE_DIR / (hashlib.sha256(json.dumps([keywords, geo, time]).encode()).hexdigest()[:20] + ".json")
    if transport is None and cache.is_file() and clock.time() - cache.stat().st_mtime < CACHE_SECONDS:
        return {**json.loads(cache.read_text(encoding="utf-8")), "cached": True}
    common = {"hl": "en-US", "tz": "-330"}
    async with httpx.AsyncClient(headers=_HEADERS, timeout=20, follow_redirects=True, transport=transport) as client:
        try:
            await client.get(f"{BASE}/trends/?geo={geo}")  # sets the NID cookie the API expects
            explore = await _get(client, "/trends/api/explore", {**common, "req": json.dumps({
                "comparisonItem": [{"keyword": k, "geo": geo, "time": time} for k in keywords],
                "category": 0, "property": ""})})
            widgets = {w["id"]: w for w in explore.get("widgets", [])}
            series = await _widget(client, widgets, "TIMESERIES", "/trends/api/widgetdata/multiline", common)
            by_region = await _widget(client, widgets, "GEO_MAP", "/trends/api/widgetdata/comparedgeo", common)
            related = {}
            for wid, w in widgets.items():
                if w.get("id", "").startswith("RELATED_QUERIES"):
                    kw = w.get("request", {}).get("restriction", {}).get("complexKeywordsRestriction", {}) \
                        .get("keyword", [{}])[0].get("value") or keywords[0]
                    related[kw] = await _related(client, w, common)
        except httpx.HTTPError as exc:
            raise TrendsError(f"Could not reach Google Trends ({exc.__class__.__name__}).") from exc

    timeline = (series or {}).get("default", {}).get("timelineData", [])
    points = [{"time": p.get("formattedAxisTime") or p.get("formattedTime"), "values": p.get("value", [])}
              for p in timeline]
    regions = [{"region": g.get("geoName"), "values": g.get("value", [])}
               for g in (by_region or {}).get("default", {}).get("geoMapData", []) if any(g.get("value", []))]
    result = {"keywords": keywords, "geo": geo, "state": state, "timeframe": timeframe, "points": points,
            "summary": [_summary(k, [p["values"][i] for p in points if len(p["values"]) > i])
                        for i, k in enumerate(keywords)],
            "by_region": sorted(regions, key=lambda r: -max(r["values"]))[:25], "related": related,
            "note": "Values are relative interest (100 = peak in this place and period), not search counts.",
            "fetched_at": int(clock.time())}
    if transport is None and points:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return {**result, "cached": False}


async def _widget(client, widgets, wid, path, common):
    w = widgets.get(wid)
    if not w:
        return None
    return await _get(client, path, {**common, "req": json.dumps(w["request"]), "token": w["token"]})


async def _related(client, widget, common) -> dict[str, list[dict[str, Any]]]:
    data = await _get(client, "/trends/api/widgetdata/relatedsearches",
                      {**common, "req": json.dumps(widget["request"]), "token": widget["token"]})
    lists = data.get("default", {}).get("rankedList", [])
    pick = lambda i: [{"query": q.get("query"), "value": q.get("formattedValue")}  # noqa: E731
                      for q in (lists[i].get("rankedKeyword", []) if len(lists) > i else [])][:20]
    return {"top": pick(0), "rising": pick(1)}


def _summary(keyword: str, values: list[int]) -> dict[str, Any]:
    if not values:
        return {"keyword": keyword, "direction": "no data"}
    third = max(1, len(values) // 3)
    early, late = sum(values[:third]) / third, sum(values[-third:]) / third
    change = (late - early) / early * 100 if early else (100.0 if late else 0.0)
    direction = "rising" if change > 15 else "falling" if change < -15 else "steady"
    return {"keyword": keyword, "average": round(sum(values) / len(values), 1), "peak": max(values),
            "change_pct": round(change, 1), "direction": direction}
