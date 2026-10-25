#!/usr/bin/env python3
"""Score past SSCOFS cycles against what actually happened, and append the
scores to a log so a trend builds up.

The published file (tools/extract_sscofs.py) is a 72-hour forecast. Once those
72 hours have passed, this script fetches the cycle-stamped copy back from the
CDN and compares it with:

  * Surface water temperature: NOAA CO-OPS Astoria (9439040) and Longview
    (9440422), USGS Columbia near Dodson (453630122021400). Hourly model value
    vs the mean of the gauge's readings within half an hour of it.
  * Currents: NOAA's current predictions (slack and max events) at the CO-OPS
    stations that sit on the model's sample line. There are no observed
    currents on the Columbia, so the predictions are the reference. They are
    for a bin a few metres down; the model value is the surface, which on its
    own makes model speeds read high.

A one-off check on 2026-10-08 found temperature 1.5-2 degC cool and Walker
Island slack/max times 10 min-2 h early with ~1.7x the speed. This turns that
into a series: steady errors can be corrected, noisy ones can only be caveated.

Each cycle becomes one JSON line in --log; cycles already in the log are
skipped, so the job can run as often as it likes. Errors are model minus
reference: a negative temperature bias means the model is cool, a negative
time error means the model's event comes early.

Usage:
  python tools/check_sscofs.py --log ./accuracy/sscofs.jsonl
  python tools/check_sscofs.py --cycle 20261005T0300Z --print
"""

import argparse
import datetime as dt
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
_LOCAL_STATIONS = os.path.join(HERE, "noaa_stations.json")
_APP_STATIONS = os.path.join(os.path.dirname(HERE), "app", "src", "main", "assets", "noaa_stations.json")
STATIONS = _LOCAL_STATIONS if os.path.exists(_LOCAL_STATIONS) else _APP_STATIONS

CURRENTS_BASE = os.environ.get("SSCOFS_CURRENTS_BASE", "https://data.nextbestoutdoors.com/currents")
COOPS = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
USGS = "https://waterservices.usgs.gov/nwis/iv/"
APP = "tidalreach"

CYCLE_HOURS = (3, 9, 15, 21)
MISSING = -32768
CM_S_TO_KT = 0.0194384
# A reference point counts only if a model sample point is this close. The
# sample points are ~1 river mile apart, so a station on the line can be ~0.8 km
# from the nearest one; much further and it is a different reach.
MAX_MATCH_KM = 1.5
# NOAA's event and the model's are the same event only if this close in time.
MAX_EVENT_SHIFT_H = 3.0
# Lead-time buckets (hours after the cycle), so drift with lead time shows.
LEADS = ((0, 24), (24, 48), (48, 73))
# Waiting this long after f072 gives gauges time to post their readings.
SETTLE_H = 6

TEMP_GAUGES = (
    {"id": "9439040", "src": "coops", "name": "Astoria (Tongue Point)", "lat": 46.2073, "lon": -123.7683},
    {"id": "9440422", "src": "coops", "name": "Longview", "lat": 46.1061, "lon": -122.9542},
    {"id": "453630122021400", "src": "usgs", "name": "Columbia near Dodson", "lat": 45.60817, "lon": -122.03842},
)
# CO-OPS current stations in the channel the model samples (positions come from
# the bundled station list). The other Columbia stations are in side channels --
# Baker Bay, Youngs Bay, Woody Island, Prairie, Clifton, Cathlamet -- which are
# different water from the ship-channel line even where they are close to it.
CURRENT_STATIONS = (
    "PCT1106",  # Sand Island Tower, 1nm SE of (midchannel) -- the bar
    "PCT1121",  # Clatsop Spit, NNE of -- entrance channel
    "PCT1151",  # Hammond, northeast of ship channel
    "PCT1176",  # Tongue Point, northwest of
    "PCT1251",  # Walker Island, south of
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", help="JSONL file to append to; cycles already in it are skipped")
    ap.add_argument("--cycle", action="append", default=[],
                    help="YYYYMMDDTHHMMZ (repeatable); default: every finished cycle in --days")
    ap.add_argument("--days", type=int, default=7, help="how far back to look for unscored cycles")
    ap.add_argument("--print", action="store_true", help="print a readable summary of each cycle")
    args = ap.parse_args()

    done = set()
    if args.log and os.path.exists(args.log):
        with open(args.log, encoding="utf-8") as f:
            done = {json.loads(line)["cycle"] for line in f if line.strip()}

    now = dt.datetime.now(dt.timezone.utc)
    cycles = [_parse_stamp(c) for c in args.cycle] or finished_cycles(now, args.days)
    todo = [c for c in cycles if _iso(c) not in done]
    if not todo:
        print("nothing to score", file=sys.stderr)
        return

    stations = current_stations()
    records = []
    for c in todo:
        doc = fetch_cycle(c)
        if doc is None:
            print(f"{_stamp(c)}: not on the CDN; skipping", file=sys.stderr)
            continue
        rec = score_cycle(doc, stations)
        records.append(rec)
        if args.print:
            print_record(rec)

    if args.log and records:
        os.makedirs(os.path.dirname(os.path.abspath(args.log)), exist_ok=True)
        with open(args.log, "a", encoding="utf-8", newline="\n") as f:
            for rec in sorted(records, key=lambda r: r["cycle"]):
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        print(f"appended {len(records)} cycle(s) to {args.log}", file=sys.stderr)


# --- cycles ----------------------------------------------------------------

def finished_cycles(now, days):
    """Cycles in the last `days` whose 72 forecast hours (plus SETTLE_H) are over."""
    latest = now - dt.timedelta(hours=72 + SETTLE_H)
    out = []
    day = (now - dt.timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= latest:
        for h in CYCLE_HOURS:
            c = day.replace(hour=h)
            if now - dt.timedelta(days=days) <= c <= latest:
                out.append(c)
        day += dt.timedelta(days=1)
    return out


def fetch_cycle(c):
    try:
        return _get_json(f"{CURRENTS_BASE}/{_stamp(c)}.json")
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return None
        raise


# --- scoring ---------------------------------------------------------------

def score_cycle(doc, stations):
    cycle = _parse_iso(doc["cycle"])
    hours = [_parse_iso(h) for h in doc["hours"]]
    samples = [dict(p, line="centerline") for p in doc["points"]] + \
              [dict(p, line="estuary") for p in doc.get("estuary", [])]
    start, end = hours[0], hours[-1]

    temps = []
    for g in TEMP_GAUGES:
        p, km = nearest(samples, g, need="t")
        if p is None or km > MAX_MATCH_KM * 2:
            # Gauges sit at the bank, a little further from the channel line.
            continue
        try:
            obs = (coops_temperature if g["src"] == "coops" else usgs_temperature)(g["id"], start, end)
        except Exception as e:  # noqa: BLE001 - one gauge down must not lose the cycle
            print(f"{g['id']}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        pairs = []
        for h, t in zip(hours, p["t"]):
            if t == MISSING:
                continue
            near = [v for (ot, v) in obs if abs((ot - h).total_seconds()) <= 1800]
            if near:
                pairs.append((h, t / 10.0 - statistics.fmean(near)))
        temps.append({
            "id": g["id"], "name": g["name"], "model_rm": p["rm"], "km": round(km, 2),
            "leads": _by_lead(cycle, pairs, lambda errs: {
                "n": len(errs), "bias_c": _r(statistics.fmean(errs)),
                "mae_c": _r(statistics.fmean(abs(e) for e in errs))}),
        })

    currents = []
    for s in stations:
        p, km = nearest(samples, s, need="u")
        if p is None or km > MAX_MATCH_KM:
            continue
        try:
            events = coops_current_events(s["id"], start, end)
        except Exception as e:  # noqa: BLE001
            print(f"{s['id']}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        if not events:
            continue
        flood_dir = events[0]["flood_dir"]
        series = along_channel(hours, p["u"], p["v"], flood_dir)
        model = model_events(series)
        slack, peak = [], []
        for ev in events:
            if not (start + dt.timedelta(hours=1) <= ev["t"] <= end - dt.timedelta(hours=1)):
                continue  # an event at the window's edge has no model neighbour to match
            m = _closest(model, ev)
            if m is None:
                continue
            shift_min = (m["t"] - ev["t"]).total_seconds() / 60.0
            if ev["kind"] == "slack":
                slack.append((ev["t"], shift_min))
            elif ev["kt"]:
                peak.append((ev["t"], (shift_min, abs(m["kt"]) / abs(ev["kt"]))))
        currents.append({
            "id": s["id"], "name": s["name"], "model_rm": p["rm"], "line": p["line"],
            "km": round(km, 2), "flood_dir": flood_dir,
            "slack": _by_lead(cycle, slack, _timing),
            "max": _by_lead(cycle, peak, lambda xs: dict(
                _timing([x[0] for x in xs]),
                speed_ratio=_r(statistics.median(x[1] for x in xs)))),
        })

    return {
        "cycle": doc["cycle"],
        "checked": _iso(dt.datetime.now(dt.timezone.utc)),
        "temperature": temps,
        "currents": currents,
    }


def along_channel(hours, us, vs, flood_dir):
    """[(t, knots)] of the current along NOAA's flood axis; + is flood."""
    rad = math.radians(flood_dir)
    out = []
    for t, u, v in zip(hours, us, vs):
        if u == MISSING or v == MISSING:
            out.append((t, None))
        else:
            out.append((t, (u * math.sin(rad) + v * math.cos(rad)) * CM_S_TO_KT))
    return out


def model_events(series):
    """Slack (zero crossings, linearly interpolated) and max flood/ebb (the
    biggest hourly sample between two slacks, refined by a parabola through it
    and its neighbours) from an hourly along-channel series."""
    events = []
    for (t0, a), (t1, b) in zip(series, series[1:]):
        if a is None or b is None or (a > 0) == (b > 0):
            continue
        frac = a / (a - b)
        events.append({"kind": "slack", "t": t0 + (t1 - t0) * frac, "kt": 0.0})
    slacks = [e["t"] for e in events]
    for lo, hi in zip(slacks, slacks[1:]):
        idx = [i for i, (t, x) in enumerate(series) if lo < t < hi and x is not None]
        if not idx:
            continue
        i = max(idx, key=lambda k: abs(series[k][1]))
        t, x = series[i]
        if 0 < i < len(series) - 1 and series[i - 1][1] is not None and series[i + 1][1] is not None:
            y0, y1, y2 = series[i - 1][1], x, series[i + 1][1]
            denom = y0 - 2 * y1 + y2
            if denom:
                off = 0.5 * (y0 - y2) / denom          # in hours, within (-1, 1)
                if abs(off) < 1:
                    t = t + dt.timedelta(hours=off)
                    x = y1 - 0.25 * (y0 - y2) * off
        events.append({"kind": "flood" if x > 0 else "ebb", "t": t, "kt": x})
    return sorted(events, key=lambda e: e["t"])


def _closest(model, ev):
    same = [m for m in model if m["kind"] == ev["kind"]]
    if not same:
        return None
    m = min(same, key=lambda m: abs((m["t"] - ev["t"]).total_seconds()))
    return m if abs((m["t"] - ev["t"]).total_seconds()) <= MAX_EVENT_SHIFT_H * 3600 else None


def _by_lead(cycle, pairs, summarize):
    """{"0-24": summary, ...} over (time, value) pairs, skipping empty buckets."""
    out = {}
    for lo, hi in LEADS:
        vals = [v for t, v in pairs if lo <= (t - cycle).total_seconds() / 3600 < hi]
        if vals:
            out[f"{lo}-{min(hi, 72)}"] = summarize(vals)
    return out


def _timing(shifts):
    return {"n": len(shifts), "mean_min": round(statistics.fmean(shifts)),
            "mae_min": round(statistics.fmean(abs(s) for s in shifts))}


# --- reference data --------------------------------------------------------

def current_stations():
    with open(STATIONS, encoding="utf-8") as f:
        stations = json.load(f)["stations"]
    by_id = {x["id"]: x for x in stations if x["kind"] == "current"}
    return [by_id[i] for i in CURRENT_STATIONS]


def coops_temperature(station, start, end):
    d = _coops(station, "water_temperature", start, end, units="metric")
    return [(_parse_coops(r["t"]), float(r["v"])) for r in d.get("data", []) if r.get("v")]


def usgs_temperature(site, start, end):
    url = (f"{USGS}?format=json&sites={site}&parameterCd=00010"
           f"&startDT={start:%Y-%m-%dT%H:%MZ}&endDT={end:%Y-%m-%dT%H:%MZ}")
    out = []
    for ts in _get_json(url)["value"]["timeSeries"]:
        for v in ts["values"][0]["value"]:
            x = float(v["value"])
            if x > -999:
                out.append((dt.datetime.fromisoformat(v["dateTime"]).astimezone(dt.timezone.utc), x))
    return out


def coops_current_events(station, start, end):
    d = _coops(station, "currents_predictions", start, end, units="english", interval="MAX_SLACK")
    out = []
    for r in d.get("current_predictions", {}).get("cp", []):
        kind = r["Type"].lower()
        if kind not in ("slack", "flood", "ebb"):
            continue
        out.append({"kind": kind, "t": _parse_coops(r["Time"]), "kt": float(r["Velocity_Major"]),
                    "flood_dir": float(r["meanFloodDir"])})
    return out


def _coops(station, product, start, end, **extra):
    q = {"product": product, "station": station, "time_zone": "gmt", "format": "json",
         "application": APP,
         "begin_date": f"{start - dt.timedelta(hours=1):%Y%m%d %H:%M}",
         "end_date": f"{end + dt.timedelta(hours=1):%Y%m%d %H:%M}", **extra}
    d = _get_json(COOPS + "?" + urllib.parse.urlencode(q))
    if "error" in d:
        msg = d["error"].get("message", "")
        if "No data was found" in msg:
            return {}
        raise RuntimeError(msg)
    return d


# --- helpers ---------------------------------------------------------------

def nearest(samples, ref, need):
    best, best_km = None, math.inf
    for p in samples:
        if need not in p:
            continue
        km = _haversine_km(p["lat"], p["lon"], ref["lat"], ref["lon"])
        if km < best_km:
            best, best_km = p, km
    return best, best_km


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def _get_json(url, tries=4):
    last = None
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"{APP} accuracy check"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        if attempt < tries:
            time.sleep(4 * attempt)
    raise last


def _parse_stamp(s):
    return dt.datetime.strptime(s, "%Y%m%dT%H%MZ").replace(tzinfo=dt.timezone.utc)


def _stamp(c):
    return c.strftime("%Y%m%dT%H%MZ")


def _parse_iso(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_coops(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)


def _r(x):
    return round(x, 2)


def print_record(rec):
    print(f"\n== cycle {rec['cycle']}")
    for g in rec["temperature"]:
        leads = ("  ".join(f"{k}h {v['bias_c']:+.1f}C (n={v['n']})" for k, v in g["leads"].items())
                 or "no gauge readings")
        print(f"  temp  {g['name']:<26} RM {g['model_rm']:>5}  {leads}")
    for s in rec["currents"]:
        sl = s["slack"].get("0-24")
        mx = s["max"].get("0-24")
        parts = []
        if sl:
            parts.append(f"slack {sl['mean_min']:+d} min (n={sl['n']})")
        if mx:
            parts.append(f"max {mx['mean_min']:+d} min x{mx['speed_ratio']:.2f} (n={mx['n']})")
        print(f"  curr  {s['id']} {s['name'][:32]:<32} RM {s['model_rm']:>5} {s['km']:.2f} km  "
              + ("  ".join(parts) or "no matched events in 0-24h"))


if __name__ == "__main__":
    main()
