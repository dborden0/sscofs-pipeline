#!/usr/bin/env python3
"""Sample the SSCOFS regulargrid surface currents and water temperature along
the committed channel centerline, and surface currents, temperature and
near-bed salinity along an estuary transect (bar to RM ~37; the salinity is for
the salt wedge), and write one small static JSON file.

This is the whole backend. A scheduled job runs it ~3h50m after each NOAA
cycle (f072 reaches S3 at ~3h40m); it writes currents/latest.json (plus a
cycle-stamped copy); a CDN serves that file; the Android app downloads it. There is no request-time compute.

Design constraints (see plans/sscofs-upriver-currents-precompute.md):

  * Host-agnostic. Reads no GITHUB_* env, assumes no checkout layout, writes to
    --out and nothing else. `python tools/extract_sscofs.py --out ./dist` runs
    identically on a laptop and in CI. Upload is a separate workflow step.
  * Read NOAA's S3 mirror by byte range; fall back to OPeNDAP if S3 is down.
  * Probe f072 before trusting a cycle -- it completes last. Walk back a cycle
    if it is missing. Never assume the nominal hour.
  * Cycles are 03/09/15/21 UTC.
  * Mask-drift guard: assert the grid origin/spacing still match and every
    committed centerline cell is still water. Fail loudly rather than publish
    garbage.
  * -99999.0 is the fill value, not a velocity. It must never reach the file;
    missing samples are written as the int16 sentinel -32768.

Usage:
  python tools/extract_sscofs.py --out ./dist
  python tools/extract_sscofs.py --out ./dist --cycle 20260901.t03z --hours 72
"""

import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_POINTS = os.path.join(HERE, "channel_points.json")
DEFAULT_ESTUARY = os.path.join(HERE, "estuary_points.json")

OPENDAP_BASE = os.environ.get(
    "SSCOFS_OPENDAP_BASE",
    "https://opendap.co-ops.nos.noaa.gov/thredds/dodsC/NOAA/SSCOFS/MODELS",
)
# Primary source: the same files on NOAA's open-data bucket (NODD). Public, no
# credentials, no egress charge to the reader. OPENDAP_BASE is the fallback.
S3_BASE = os.environ.get(
    "SSCOFS_S3_BASE",
    "https://noaa-nos-ofs-pds.s3.amazonaws.com/sscofs/netcdf",
)

CYCLE_HOURS = (3, 9, 15, 21)
FILL_LIMIT = -9000.0          # anything below this is _FillValue (-99999)
SPEED_SANITY_MS = 15.0        # a real Columbia surface current is never this fast
TEMP_RANGE_C = (-2.0, 35.0)   # outside this a temperature is a leaked fill value
SALT_RANGE_PSU = (0.0, 42.0)
SALT_MAX_LEVELS = 14          # Depth[0..13] = 0..40 m; the estuary channel is shallower
MISSING = -32768             # int16 sentinel for "no sample here"
SCHEMA = 1

# Columbia slab, bar to Bonneville (channel_points.json and estuary_points.json).
# iy/ix are regulargrid indices; the job reads this rectangle once per forecast
# hour and pulls the sample cells out of it locally.
IY0, IY1 = 236, 390
IX0, IX1 = 1070, 1518

GRID_TOL = 1e-6


def cycle_url(cycle, fhour, base=OPENDAP_BASE):
    """cycle is 'YYYYMMDD.tCCz'; fhour like 'f072'."""
    date, tcc = cycle.split(".")
    y, m, d = date[:4], date[4:6], date[6:8]
    return (f"{base}/{y}/{m}/{d}/"
            f"sscofs.{tcc}.{date}.regulargrid.{fhour}.nc")


def candidate_cycles(now, back=5):
    """Most-recent-first list of cycle ids at or before `now`."""
    out = []
    probe = now
    while len(out) < back:
        for h in reversed(CYCLE_HOURS):
            c = probe.replace(hour=h, minute=0, second=0, microsecond=0)
            if c <= now and (not out or c < _parse_cycle(out[-1])):
                out.append(c.strftime("%Y%m%d.t%Hz"))
        probe -= dt.timedelta(days=1)
    return out


def _parse_cycle(cid):
    date, tcc = cid.split(".")
    return dt.datetime(int(date[:4]), int(date[4:6]), int(date[6:8]),
                       int(tcc[1:3]), tzinfo=dt.timezone.utc)


def _retry(desc, fn, tries=4, base_sleep=4):
    """NOAA's THREDDS/OPeNDAP endpoint intermittently drops a response
    mid-stream (ChunkedEncodingError / 'Response ended prematurely') even for
    small requests. Retry with backoff before giving up on a read."""
    import time

    last = None
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - transport flakiness, retry then surface
            last = e
            if attempt < tries:
                sleep = base_sleep * attempt
                print(f"  {desc}: {type(e).__name__} (attempt {attempt}/{tries}), "
                      f"retrying in {sleep}s", file=sys.stderr)
                time.sleep(sleep)
    raise last


def _open(url):
    import xarray as xr

    return xr.open_dataset(url, engine="pydap")


def _http_status(url, method="GET"):
    """HTTP status of a request, or None if the server can't be reached."""
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001 - DNS, TLS, timeout: all mean unreachable
        return None


def _probe_opendap(cid, fhour):
    """'ok', 'missing' (not published yet) or 'down' (server itself is out)."""
    url = cycle_url(cid, fhour)
    try:
        # No retry on the probe -- a not-yet-published file just 404s, and a
        # transient flake here only costs one cycle of walk-back (the data
        # from the previous cycle is still valid). The real reads retry.
        _open(url).close()
        return "ok", None
    except Exception as e:  # noqa: BLE001 - probing, any failure = not ready
        # Tell "not published yet" (404) apart from the server itself being
        # down (5xx / unreachable). Walking back can't help with the latter.
        status = _http_status(url + ".dds")
        if status is None or status >= 500:
            return "down", "unreachable" if status is None else f"HTTP {status}"
        return "missing", f"{type(e).__name__}, HTTP {status}"


def resolve_cycle(explicit, hours):
    """-> (cycle id, source). Prefer NOAA's S3 mirror: faster, more reliable,
    and it is where f072 lands ~3h40m after the cycle. Fall back to the OPeNDAP
    server if S3 is unreachable, has no complete cycle, or its file layout no
    longer matches what the byte-range reader expects."""
    last = f"f{hours:03d}"
    cycles = [explicit] if explicit else candidate_cycles(dt.datetime.now(dt.timezone.utc))
    picked = _resolve_s3(cycles, last)
    if picked:
        return picked
    print(f"falling back to OPeNDAP at {OPENDAP_BASE}", file=sys.stderr)
    return _resolve_opendap(cycles, last, explicit)


def _resolve_s3(cycles, last):
    """(cycle, S3Source) or None if S3 can't serve any of `cycles`."""
    for cid in cycles:
        status = _http_status(cycle_url(cid, last, S3_BASE), method="HEAD")
        if status == 200:
            source = S3Source(cid)
            try:
                source.check_layout()
            except (Exception, SystemExit) as e:  # noqa: BLE001 - any failure -> OPeNDAP
                print(f"cycle {cid}: S3 layout check failed ({e}); not using S3",
                      file=sys.stderr)
                return None
            print(f"cycle {cid}: {last} present on S3, using it", file=sys.stderr)
            return cid, source
        if status in (403, 404):          # S3 says 403 or 404 for a missing key
            print(f"cycle {cid}: {last} not on S3 yet (HTTP {status}), walking back",
                  file=sys.stderr)
            continue
        why = "unreachable" if status is None else f"HTTP {status}"
        print(f"NOAA S3 mirror unavailable ({why}) at {S3_BASE}", file=sys.stderr)
        return None
    print(f"no complete cycle on S3 in the last {len(cycles)} cycles", file=sys.stderr)
    return None


def _resolve_opendap(cycles, last, explicit):
    for cid in cycles:
        state, why = _probe_opendap(cid, last)
        if state == "ok":
            print(f"cycle {cid}: {last} present on OPeNDAP, using it", file=sys.stderr)
            return cid, OpendapSource(cid)
        if state == "down":
            raise SystemExit(f"NOAA S3 mirror and OPeNDAP server ({why}) both "
                             f"unavailable -- not a code problem; the next "
                             f"scheduled run will retry")
        if explicit:
            return cid, OpendapSource(cid)   # forced: let the reads report it
        print(f"cycle {cid}: {last} not ready ({why}), walking back", file=sys.stderr)
    raise SystemExit(f"no cycle with a complete forecast found in the last {len(cycles)} cycles")


def load_points(path):
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if doc.get("schema") != 1:
        raise SystemExit(f"{path}: unexpected schema {doc.get('schema')}")
    return doc["grid"], doc["points"]


def _isel(var, levels, rect):
    """isel indexers for var[time 0, levels, rect] (levels ignored for 2-D)."""
    y0, y1, x0, x1 = rect
    idx = {}
    for dim in var.dims:
        low = dim.lower()
        if low.startswith("time"):
            idx[dim] = 0
        elif low.startswith(("dep", "sig", "lev", "s_")):
            idx[dim] = slice(levels.start, levels.stop)
        elif "y" in low or "lat" in low or low == "ny":
            idx[dim] = slice(y0, y1 + 1)
        elif "x" in low or "lon" in low or low == "nx":
            idx[dim] = slice(x0, x1 + 1)
    return idx


class OpendapSource:
    """NOAA's THREDDS/OPeNDAP server, the fallback source."""
    label = "OPeNDAP"

    def __init__(self, cycle):
        self.cycle = cycle

    def read(self, fhour, name, levels, rect):
        """name[time 0, levels, rect] as float32 numpy (nlev, ny, nx), with retry.

        Only the asked-for rectangle crosses the wire, never the full
        1553x1519 grid -- pulling whole arrays over DAP2 is what was dropping
        mid-stream.
        """
        import numpy as np

        url = cycle_url(self.cycle, fhour)
        y0, y1, x0, x1 = rect

        def once():
            ds = _open(url)
            try:
                var = ds[name]
                vals = np.asarray(var.isel(_isel(var, levels, rect)).values, np.float32)
                return vals.reshape(len(levels), y1 - y0 + 1, x1 - x0 + 1)
            finally:
                ds.close()

        return _retry(f"read {name}", once)

    def read_static(self, name, rect):
        """A 2-D field (mask) from f000 as numpy (ny, nx)."""
        url = cycle_url(self.cycle, "f000")

        def once():
            ds = _open(url)
            try:
                if name not in ds:
                    raise KeyError(name)
                var = ds[name]
                return var.isel(_isel(var, range(1), rect)).values
            finally:
                ds.close()

        return _retry(f"read {name}", once)

    def grid_corners(self, fhour):
        """Grid origin and spacing from a 2-element corner read, not the whole
        coordinate array."""
        url = cycle_url(self.cycle, fhour)

        def once():
            ds = _open(url)
            try:
                lon = ds["Longitude"] if "Longitude" in ds else ds["lon"]
                lat = ds["Latitude"] if "Latitude" in ds else ds["lat"]
                # Longitude varies along nx, Latitude along ny; two values of each.
                lon_row = lon.isel({d: (slice(0, 2) if "x" in d.lower() or d.lower() == "nx" else 0)
                                    for d in lon.dims}).values.ravel()
                lat_col = lat.isel({d: (slice(0, 2) if "y" in d.lower() or d.lower() == "ny" else 0)
                                    for d in lat.dims}).values.ravel()
                return float(lon_row[0]), float(lat_col[0]), \
                    float(abs(lon_row[1] - lon_row[0])), float(abs(lat_col[1] - lat_col[0]))
            finally:
                ds.close()

        return _retry("read grid corners", once)


class S3Source:
    """The same regulargrid files on NOAA's S3 mirror, read by byte range.

    Each file is ~1.7 GB, so we never download one. The 3-D variables are
    stored as uncompressed HDF5 chunks of (time 1, depth 7, y 311, x 304) in C
    order, so one depth level of one chunk is a contiguous plane, and the rows
    a rectangle needs are one contiguous run inside it: a level over the
    Columbia slab is 4 range reads, the estuary's thin strip 2. The chunk
    offsets come from f000's HDF5 index; every other hour is checked to be the
    same file size (the writer is deterministic) before its bytes are trusted.
    """
    label = "S3"
    VARS = ("u_eastward", "v_northward", "temp", "salt")

    def __init__(self, cycle):
        self.cycle = cycle
        self._chunks = None   # {var: (cz, cy, cx)}
        self._offsets = {}    # {(var, z0, y0, x0): byte offset}
        self._size = None
        self._h5 = None

    def _f000(self):
        if self._h5 is None:
            import fsspec
            import h5py

            url = cycle_url(self.cycle, "f000", S3_BASE)
            self._h5 = _retry("open f000 on S3", lambda: h5py.File(
                fsspec.open(url, "rb", block_size=2**20).open(), "r"))
        return self._h5

    def check_layout(self):
        """Check every variable's storage on f000; SystemExit if it changed."""
        if self._chunks is not None:
            return
        h = self._f000()
        chunks = {}
        for name in self.VARS:
            d = h[name]
            if (d.dtype.str != "<f4" or d.compression or d.chunks is None or d.ndim != 4
                    or d.chunks[0] != 1 or d.id.get_create_plist().get_nfilters()):
                raise SystemExit(f"S3 {name}: storage layout changed (dtype {d.dtype.str}, "
                                 f"chunks {d.chunks}, compression {d.compression}) -- "
                                 f"byte-range reader needs updating")
            chunks[name] = d.chunks[1:]
        self._chunks = chunks
        self._size = h.id.get_filesize()

    def _offset(self, name, z0, y0, x0):
        key = (name, z0, y0, x0)
        if key not in self._offsets:
            off = self._f000()[name].id.get_chunk_info_by_coord((0, z0, y0, x0)).byte_offset
            if off is None:
                raise SystemExit(f"S3 {name}: chunk at ({z0},{y0},{x0}) not written")
            self._offsets[key] = off
        return self._offsets[key]

    def _range(self, url, start, n):
        import urllib.request

        req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{start + n - 1}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            total = int(r.headers["Content-Range"].rsplit("/", 1)[1])
            data = r.read()
        if total != self._size:
            raise SystemExit(f"{url}: size {total} != f000's {self._size}; chunk "
                             f"offsets can't be reused")
        if len(data) != n:
            raise IOError(f"short read {len(data)}/{n}")
        return data

    def read(self, fhour, name, levels, rect):
        """name[time 0, levels, rect] as float32 numpy (nlev, ny, nx), with retry."""
        import numpy as np
        from concurrent.futures import ThreadPoolExecutor

        self.check_layout()
        cz, cy, cx = self._chunks[name]
        y0, y1, x0, x1 = rect
        # One read per (level, chunk): its rows ys..ye, full chunk width.
        # Offsets come from f000's index, so they are resolved here, before
        # the thread pool, not inside it.
        reads = []
        for k in levels:
            zc = k // cz * cz
            for yc in range(y0 // cy * cy, y1 + 1, cy):
                ys, ye = max(y0, yc), min(y1 + 1, yc + cy)
                for xc in range(x0 // cx * cx, x1 + 1, cx):
                    start = self._offset(name, zc, yc, xc) + ((k - zc) * cy + (ys - yc)) * cx * 4
                    reads.append((k, xc, ys, ye, start))
        url = cycle_url(self.cycle, fhour, S3_BASE)

        def once():
            with ThreadPoolExecutor(min(16, len(reads))) as ex:
                blobs = list(ex.map(lambda r: self._range(url, r[4], (r[3] - r[2]) * cx * 4), reads))
            out = np.empty((len(levels), y1 - y0 + 1, x1 - x0 + 1), np.float32)
            for (k, xc, ys, ye, _), raw in zip(reads, blobs):
                rows = np.frombuffer(raw, "<f4").reshape(ye - ys, cx)
                xs, xe = max(x0, xc), min(x1 + 1, xc + cx)
                out[k - levels.start, ys - y0:ye - y0, xs - x0:xe - x0] = rows[:, xs - xc:xe - xc]
            return out

        return _retry(f"read {name}", once)

    def read_static(self, name, rect):
        """A 2-D field (mask) from f000; small and contiguous, so via h5py."""
        y0, y1, x0, x1 = rect
        if name not in self._f000():
            raise KeyError(name)
        return _retry(f"read {name}", lambda: self._f000()[name][y0:y1 + 1, x0:x1 + 1])

    def grid_corners(self, fhour):
        h = self._f000()

        def once():
            lon = h["Longitude"][0, 0:2]
            lat = h["Latitude"][0:2, 0]
            return float(lon[0]), float(lat[0]), \
                float(abs(lon[1] - lon[0])), float(abs(lat[1] - lat[0]))

        return _retry("read grid corners", once)


def assert_grid(source, grid):
    """Fail if the grid origin/spacing no longer match the committed centerline."""
    lon0, lat0, dlon, dlat = source.grid_corners("f000")
    problems = []
    if abs(lon0 - grid["lon0"]) > 1e-3:
        problems.append(f"lon0 {lon0} != {grid['lon0']}")
    if abs(lat0 - grid["lat0"]) > 1e-3:
        problems.append(f"lat0 {lat0} != {grid['lat0']}")
    for label, dd in (("lon", dlon), ("lat", dlat)):
        if dd and abs(dd - grid["d"]) > GRID_TOL:
            problems.append(f"{label} spacing {dd} != {grid['d']}")
    if problems:
        raise SystemExit("SSCOFS grid drifted from the committed centerline: "
                         + "; ".join(problems) + " -- rebuild tools/channel_points.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--cycle", default=None, help="YYYYMMDD.tCCz (default: probe latest complete)")
    ap.add_argument("--hours", type=int, default=72, help="forecast hours f000..fNNN")
    ap.add_argument("--channel-points", default=DEFAULT_POINTS)
    ap.add_argument("--estuary-points", default=DEFAULT_ESTUARY,
                    help="salinity transect below the centerline; '' to skip")
    args = ap.parse_args()

    import numpy as np

    grid, points = load_points(args.channel_points)
    estuary = []
    if args.estuary_points:
        egrid, estuary = load_points(args.estuary_points)
        if egrid != grid:
            raise SystemExit(f"{args.estuary_points}: grid {egrid} != centerline grid {grid}")
    cycle, source = resolve_cycle(args.cycle, args.hours)
    cycle_time = _parse_cycle(cycle)

    slab = (IY0, IY1, IX0, IX1)
    local = _local(points, "centerline")
    elocal = _local(estuary, "estuary")

    # Grid + mask-drift guard on f000.
    assert_grid(source, grid)
    try:
        mask = source.read_static("mask", slab)
        for label, path, cells in (("centerline", args.channel_points, local),
                                   ("estuary", args.estuary_points, elocal)):
            bad = [p["rm"] for p, ly, lx in cells if mask[ly, lx] != 1]
            if bad:
                raise SystemExit(f"committed {label} cells are no longer water "
                                 f"(mask != 1) at RM {bad} -- rebuild {path}")
    except KeyError:
        print("f000: no 'mask' variable; skipping mask-drift guard", file=sys.stderr)

    # The salt wedge lies along the bed, so salinity is read through the water
    # column, but only over the estuary's thin strip and only as deep as its
    # deepest cell goes on f000. Which level is the bed varies by cell (and a
    # little with the tide), so it is picked per cell, per hour, below.
    if estuary:
        erect = (min(p["iy"] for p in estuary), max(p["iy"] for p in estuary),
                 min(p["ix"] for p in estuary), max(p["ix"] for p in estuary))
        eloc = [(p["iy"] - erect[0], p["ix"] - erect[2]) for p in estuary]
        col = source.read("f000", "salt", range(SALT_MAX_LEVELS), erect)
        wet = [int(np.nonzero(col[:, y, x] > FILL_LIMIT)[0].max(initial=-1)) for y, x in eloc]
        # One level of headroom: the bed cell can gain a level at high water.
        salt_levels = range(min(max(wet) + 2, SALT_MAX_LEVELS))
        print(f"estuary: salinity levels 0..{salt_levels.stop - 1}", file=sys.stderr)

    us = [[] for _ in points]
    vs = [[] for _ in points]
    ts = [[] for _ in points]
    ets = [[] for _ in estuary]
    sbs = [[] for _ in estuary]
    eus = [[] for _ in estuary]
    evs = [[] for _ in estuary]
    hours_iso = []
    for h in range(args.hours + 1):
        fh = f"f{h:03d}"
        hours_iso.append((cycle_time + dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        # Each variable is its own gap: a failed temperature read must not cost
        # the hour's currents.
        uv = _read_or_gap(fh, "currents", lambda: (source.read(fh, "u_eastward", range(1), slab)[0],
                                                   source.read(fh, "v_northward", range(1), slab)[0]))
        temp = _read_or_gap(fh, "temperature", lambda: source.read(fh, "temp", range(1), slab)[0])
        salt = (_read_or_gap(fh, "salinity", lambda: source.read(fh, "salt", salt_levels, erect))
                if estuary else None)

        def put_uv(ly, lx, u_out, v_out):
            uu, vv = (float(uv[0][ly, lx]), float(uv[1][ly, lx])) if uv else (np.nan, np.nan)
            if (uu < FILL_LIMIT or vv < FILL_LIMIT
                    or abs(uu) > SPEED_SANITY_MS or abs(vv) > SPEED_SANITY_MS
                    or np.isnan(uu) or np.isnan(vv)):
                u_out.append(MISSING)
                v_out.append(MISSING)
            else:
                u_out.append(_q(uu))
                v_out.append(_q(vv))

        for i, (p, ly, lx) in enumerate(local):
            put_uv(ly, lx, us[i], vs[i])
            ts[i].append(_q10(temp[ly, lx], TEMP_RANGE_C) if temp is not None else MISSING)
        for i, (p, ly, lx) in enumerate(elocal):
            # The estuary strip lies inside the slab, so its currents cost no extra read.
            put_uv(ly, lx, eus[i], evs[i])
            ets[i].append(_q10(temp[ly, lx], TEMP_RANGE_C) if temp is not None else MISSING)
            if salt is None:
                sbs[i].append(MISSING)
                continue
            y, x = eloc[i]
            wet = np.nonzero(salt[:, y, x] > FILL_LIMIT)[0]
            sbs[i].append(_q10(salt[wet[-1], y, x], SALT_RANGE_PSU) if wet.size else MISSING)
        if h % 12 == 0:
            print(f"  {fh} ok", file=sys.stderr)

    doc = {
        "schema": SCHEMA,
        "cycle": cycle_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "units": {
            "uv": "cm/s", "t": "0.1 degC", "sb": "0.1 PSU",
            "note": "int16; -32768 = no data. u/v: eastward/northward surface current. "
                    "t: surface water temperature. sb: salinity at the deepest model "
                    "level, i.e. near the bed",
        },
        "hours": hours_iso,
        "points": [
            {"rm": p["rm"], "name": p["name"], "lat": p["lat"], "lon": p["lon"],
             "u": us[i], "v": vs[i], "t": ts[i]}
            for i, p in enumerate(points)
        ],
        # Bar to RM ~37, for the salt wedge, plus surface currents (added
        # under schema 1, so older readers ignore them). Tidal Reach still
        # uses NOAA's station predictions here; Next Best Outdoors reads these.
        "estuary": [
            {"rm": p["rm"], "lat": p["lat"], "lon": p["lon"], "t": ets[i], "sb": sbs[i],
             "u": eus[i], "v": evs[i]}
            for i, p in enumerate(estuary)
        ],
    }

    outdir = os.path.join(args.out, "currents")
    os.makedirs(outdir, exist_ok=True)
    stamp = cycle_time.strftime("%Y%m%dT%H%MZ")
    for name in ("latest.json", f"{stamp}.json"):
        path = os.path.join(outdir, name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, separators=(",", ":"))
            f.write("\n")
        print(f"wrote {path} ({os.path.getsize(path)} bytes)", file=sys.stderr)
    # Committed back by the workflow to keep the Actions schedule from being
    # disabled after 60 days of no commits.
    with open(os.path.join(outdir, "last_cycle.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(stamp + "\n")


def _local(points, label):
    """[(point, iy - IY0, ix - IX0)], or SystemExit if one falls off the slab."""
    out = [(p, p["iy"] - IY0, p["ix"] - IX0) for p in points]
    for p, ly, lx in out:
        if not (0 <= ly <= IY1 - IY0 and 0 <= lx <= IX1 - IX0):
            raise SystemExit(f"{label} point RM {p['rm']} ({p['iy']},{p['ix']}) "
                             f"is outside the extraction slab")
    return out


def _read_or_gap(fh, what, fn):
    """fn(), or None (an hour of gaps) if it still fails after its retries."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - one bad hour is a gap, not a failed run
        print(f"{fh}: {what} unreadable after retries ({type(e).__name__}); writing gaps",
              file=sys.stderr)
        return None


def _q10(x, valid):
    """A scalar -> int16 tenths, or MISSING if it is fill or outside `valid`."""
    x = float(x)
    if x != x or not (valid[0] <= x <= valid[1]):
        return MISSING
    return int(round(x * 10.0))


def _q(v_ms):
    """m/s -> int16 cm/s, clamped clear of the missing sentinel."""
    cm = int(round(v_ms * 100.0))
    return max(-32767, min(32767, cm))


if __name__ == "__main__":
    main()
