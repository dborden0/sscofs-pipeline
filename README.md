# SSCOFS Pipeline

Automated precompute pipeline for Columbia River surface currents from NOAA's Sea Surface Current Operational Forecast System (SSCOFS).

## Architecture

1. **Scheduled Extraction** (`.github/workflows/sscofs.yml`):
   - Runs 4 times daily (00:50, 06:50, 12:50, 18:50 UTC).
   - Samples surface currents, temperature, and estuary salinity across centerline points (`tools/channel_points.json` and `tools/estuary_points.json`) using byte-range reads on NOAA's public S3 mirror (or OPeNDAP fallback).
   - Adds a `conditions` block (`tools/fetch_conditions.py`; `--no-conditions` skips it): 72 h of NWS wind at 8 river points and the Columbia River Bar (PZZ210) alerts and forecast, both from NWS `api.weather.gov`, plus water temperature from NOAA CO-OPS and USGS (`api.waterdata.usgs.gov`) gauges. Best-effort: a failed fetch just leaves the key out and never blocks a publish.
   - Formats a compact JSON file (`latest.json` and `<timestamp>.json`). `conditions` is additive under schema 1, so older apps ignore it.
   - Publishes directly to Cloudflare R2 / CDN. `latest.json` gets a 5-minute edge cache (`s-maxage=300`) so a new bar alert reaches phones within minutes; the cycle-stamped files keep 6 h (`s-maxage=21600`).
   - Updates the `sscofs-cycle-stamp` branch to reset GitHub's 60-day scheduled workflow inactivity timer.

2. **Daily Accuracy Check** (`.github/workflows/sscofs-accuracy.yml`):
   - Runs daily at 13:30 UTC.
   - Evaluates expired forecast cycles against USGS and NOAA observation gauges.
   - Logs performance to the `sscofs-accuracy` branch.

## Secrets Required

Configure the following secrets in GitHub (**Settings > Secrets and variables > Actions**):

- `R2_ENDPOINT`: Cloudflare R2 S3 endpoint URL (e.g. `https://<account_id>.r2.cloudflarestorage.com`)
- `R2_ACCESS_KEY_ID`: Cloudflare R2 Access Key ID
- `R2_SECRET_ACCESS_KEY`: Cloudflare R2 Secret Access Key
- `R2_BUCKET`: Name of your Cloudflare R2 bucket
