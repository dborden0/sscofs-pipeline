# SSCOFS Pipeline

Automated precompute pipeline for Columbia River surface currents from NOAA's Sea Surface Current Operational Forecast System (SSCOFS).

## Architecture

1. **Scheduled Extraction** (`.github/workflows/sscofs.yml`):
   - Runs 4 times daily (00:50, 06:50, 12:50, 18:50 UTC).
   - Samples surface currents, temperature, and estuary salinity across centerline points (`tools/channel_points.json` and `tools/estuary_points.json`) using byte-range reads on NOAA's public S3 mirror (or OPeNDAP fallback).
   - Formats a compact JSON file (`latest.json` and `<timestamp>.json`).
   - Publishes directly to Cloudflare R2 / CDN.
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
