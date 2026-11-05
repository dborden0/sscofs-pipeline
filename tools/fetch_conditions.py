#!/usr/bin/env python3
"""Wind, Columbia River Bar guidance and measured water temperature, for the
published forecast file (currents/latest.json, key "conditions").

This is the reference implementation of a change for the sscofs-pipeline repo,
which is where the publish job runs: copy this file into its tools/ and call
`fetch_conditions()` from extract_sscofs.py (see plans/pipeline-conditions.md).
It is here so the app side can be built and tested against real data. The apps
read it from the file they already download, so they contact no new host.

Everything is US federal and public domain:

  * Wind: NWS gridpoint forecasts, api.weather.gov (keyless; asks for an
    identifying User-Agent). Raw grid data, so speeds are numbers, not "5 to
    10 mph" strings, and gusts are included.
  * Bar: NWS Portland's active alerts and zone forecast for PZZ210, Columbia
    River Bar, passed through as NWS wrote them. Nothing here judges whether
    the bar is passable; the apps attribute the text to the NWS and add nothing.
  * Water temperature: NOAA CO-OPS (Longview 9440422, Astoria 9439040) and USGS
    (Columbia River near Dodson, 453630122021400) through the newer
    api.waterdata.usgs.gov OGC API. USGS recent values are provisional and are
    flagged as such. A gauge with nothing in the last MAX_GAUGE_AGE is left out.

Every fetch is best-effort: a source that fails is left out and logged, and
`fetch_conditions()` never raises, so it cannot stop the currents from
publishing.

Usage (standalone, to try it):
  python tools/fetch_conditions.py                     # print the block
  python tools/fetch_conditions.py --into latest.json  # add it to a file in place
"""

import argparse
import datetime as dt
import json
import sys
import urllib.parse
import urllib.request

# A generic app name, not a person: api.weather.gov asks for something that
# identifies the client, and a website is enough.
USER_AGENT = "TidalReach/1.0 (https://tidalreach.app)"
TIMEOUT_S = 30

HOURS = 72
KMH_TO_KT = 1 / 1.852
MAX_GAUGE_AGE = dt.timedelta(hours=6)

# Bar to Bonneville, roughly every 20 river miles. The apps take the nearest
# within a few tens of km. Grid cells were resolved from /points on 2026-11-04;
# they are re-resolved on each run in case NWS re-grids.
WIND_POINTS = [
    ("Columbia River Bar", 46.25, -124.02),
    ("Astoria", 46.19, -123.83),
    ("Cathlamet", 46.20, -123.38),
    ("Longview", 46.11, -122.95),
    ("St. Helens", 45.86, -122.79),
    ("Vancouver", 45.61, -122.67),
    ("Camas", 45.57, -122.40),
    ("Cascade Locks", 45.67, -121.89),
]

BAR_ZONE = "PZZ210"
# The bar zone's centre, for the apps' "near the bar" test.
BAR_LAT, BAR_LON = 46.25, -124.02

COOPS_TEMP = [("9440422", "Longview"), ("9439040", "Astoria (Tongue Point)")]
USGS_TEMP = [("USGS-453630122021400", "Columbia River near Dodson")]


def _get_json(url, accept="application/geo+json"):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.load(r)


def _iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _parse_duration_hours(d):
    """ISO 8601 durations as NWS writes them: PT3H, P1D, P1DT6H."""
    d = d.upper()
    days = hours = 0
    if not d.startswith("P"):
        raise ValueError(d)
    body = d[1:]
    if "T" in body:
        dpart, tpart = body.split("T", 1)
    else:
        dpart, tpart = body, ""
    if dpart:
        days = int(dpart.rstrip("D") or 0)
    if tpart:
        num = ""
        for ch in tpart:
            if ch.isdigit():
                num += ch
            elif ch == "H":
                hours += int(num)
                num = ""
            else:  # minutes/seconds never appear in these layers
                num = ""
    return days * 24 + hours


def _hourly(layer, start, hours, convert):
    """Expand an NWS grid layer (validTime = start/duration) onto `hours` hourly slots."""
    out = [None] * hours
    for v in layer.get("values", []):
        if v.get("value") is None:
            continue
        t0s, dur = v["validTime"].split("/")
        t0 = _parse_time(t0s)
        n = max(1, _parse_duration_hours(dur))
        for k in range(n):
            i = int(((t0 + dt.timedelta(hours=k)) - start).total_seconds() // 3600)
            if 0 <= i < hours:
                out[i] = convert(v["value"])
    return out


def _speed_kt(layer):
    unit = layer.get("uom", "")
    if unit.endswith("km_h-1"):
        return lambda x: round(x * KMH_TO_KT)
    if unit.endswith("m_s-1"):
        return lambda x: round(x * 3600 / 1852)
    raise ValueError(f"unexpected wind unit {unit}")


def fetch_wind(now):
    start = now.replace(minute=0, second=0, microsecond=0)
    points = []
    for name, lat, lon in WIND_POINTS:
        try:
            meta = _get_json(f"https://api.weather.gov/points/{lat},{lon}")["properties"]
            grid = _get_json(meta["forecastGridData"])["properties"]
            spd, gst, dirn = grid["windSpeed"], grid["windGust"], grid["windDirection"]
            points.append({
                "name": name, "lat": lat, "lon": lon,
                "office": meta["gridId"], "gridX": meta["gridX"], "gridY": meta["gridY"],
                "kt": _hourly(spd, start, HOURS, _speed_kt(spd)),
                "gustKt": _hourly(gst, start, HOURS, _speed_kt(gst)),
                "fromDeg": _hourly(dirn, start, HOURS, lambda x: int(round(x)) % 360),
            })
        except Exception as e:  # noqa: BLE001 - one bad point must not drop the rest
            print(f"  wind {name}: {type(e).__name__}: {e}", file=sys.stderr)
    if not points:
        return None
    return {
        "source": "NWS forecast",
        "start": _iso(start),
        "stepMinutes": 60,
        "note": "kt and gustKt in knots; fromDeg is the direction the wind blows from, degrees true; null = no forecast",
        "points": points,
    }


def fetch_bar(now):
    try:
        alerts = _get_json(f"https://api.weather.gov/alerts/active/zone/{BAR_ZONE}")
        zone = _get_json(f"https://api.weather.gov/zones/forecast/{BAR_ZONE}/forecast")["properties"]
    except Exception as e:  # noqa: BLE001
        print(f"  bar: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    sections = [
        {"name": p["name"].strip(), "text": " ".join(p["detailedForecast"].split())}
        for p in zone.get("periods", [])
        if p.get("detailedForecast", "").strip()
    ]
    events = []
    for f in alerts.get("features", []):
        p = f["properties"]
        if p.get("status") != "Actual" or p.get("messageType") == "Cancel":
            continue
        events.append({
            "event": p["event"],
            "onset": _iso(_parse_time(p["onset"])) if p.get("onset") else None,
            "ends": _iso(_parse_time(p["ends"])) if p.get("ends") else None,
        })
    if not sections and not events:
        return None
    return {
        "zone": BAR_ZONE,
        "name": "Columbia River Bar",
        "lat": BAR_LAT, "lon": BAR_LON,
        "source": "NWS Portland",
        "updated": _iso(_parse_time(zone["updated"])) if zone.get("updated") else _iso(now),
        "alerts": events,
        "sections": sections,
    }


def _coops_temp(sid, name, now):
    q = urllib.parse.urlencode({
        "date": "latest", "station": sid, "product": "water_temperature", "units": "metric",
        "time_zone": "gmt", "format": "json", "application": "TidalReach",
    })
    d = _get_json(f"https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?{q}", accept="application/json")
    if "data" not in d or not d["data"]:
        return None
    md, row = d["metadata"], d["data"][-1]
    at = dt.datetime.strptime(row["t"], "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
    if now - at > MAX_GAUGE_AGE or row.get("v") in (None, ""):
        return None
    return {"id": sid, "name": name, "lat": float(md["lat"]), "lon": float(md["lon"]),
            "source": "NOAA CO-OPS", "at": _iso(at), "c": round(float(row["v"]), 1),
            "provisional": False}


def _usgs_temp(sid, name, now):
    q = urllib.parse.urlencode({"monitoring_location_id": sid, "parameter_code": "00010", "f": "json"})
    d = _get_json(f"https://api.waterdata.usgs.gov/ogcapi/v0/collections/latest-continuous/items?{q}",
                  accept="application/geo+json")
    for f in d.get("features", []):
        p = f["properties"]
        if p.get("unit_of_measure") != "degC" or p.get("value") in (None, ""):
            continue
        at = _parse_time(p["time"])
        if now - at > MAX_GAUGE_AGE:
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        return {"id": sid, "name": name, "lat": round(lat, 5), "lon": round(lon, 5),
                "source": "USGS", "at": _iso(at), "c": round(float(p["value"]), 1),
                "provisional": (p.get("approval_status") or "").lower() != "approved"}
    return None


def fetch_water_temp(now):
    out = []
    for fn, gauges in ((_coops_temp, COOPS_TEMP), (_usgs_temp, USGS_TEMP)):
        for sid, name in gauges:
            try:
                g = fn(sid, name, now)
                if g:
                    out.append(g)
                else:
                    print(f"  temp {sid}: nothing in the last {MAX_GAUGE_AGE}", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"  temp {sid}: {type(e).__name__}: {e}", file=sys.stderr)
    return out


def fetch_conditions(now=None):
    """The "conditions" block, or None if every source failed. Never raises."""
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        block = {
            "fetched": _iso(now),
            "wind": fetch_wind(now),
            "bar": fetch_bar(now),
            "waterTemp": fetch_water_temp(now),
        }
    except Exception as e:  # noqa: BLE001 - belt and braces; the publish must go on
        print(f"  conditions: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    block = {k: v for k, v in block.items() if v}
    return block if len(block) > 1 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--into", help="add the block to this forecast file, in place")
    args = ap.parse_args()
    block = fetch_conditions()
    if args.into:
        with open(args.into, encoding="utf-8") as f:
            doc = json.load(f)
        if block:
            doc["conditions"] = block
        with open(args.into, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, separators=(",", ":"))
            f.write("\n")
        print(f"updated {args.into}", file=sys.stderr)
    else:
        json.dump(block, sys.stdout, indent=1)
        print()


if __name__ == "__main__":
    main()
