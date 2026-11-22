# Windows deployment and real-route testing

## Requirements

Use Python 3.14, uv and Docker Desktop with Linux containers. No OSRM account or
key is needed. Keep ports 5000 (OSRM) and 8000 (worker) available. Stop an existing
local Uvicorn server with Ctrl+C. A usable Docker daemon is required for preprocessing.
The Austria PBF is roughly 0.8 GB and generated graphs add multiple files. Keep
several GB of disk space free and allocate enough Docker memory; full-country
peak RAM/time has not been measured by this change.

## One-time preparation

From PowerShell:

```powershell
cd "C:\0_Univercity\AI Architecture\THE project\Project\Fuel-Price-Comparison-Platform"
uv sync --frozen
uv run --frozen pytest -q
uv run --frozen python -m scripts.prepare_osrm
if ($LASTEXITCODE -ne 0) { throw "Preparation failed; inspect the error before continuing" }
```

The script uses the following sources and stages:

1. OSM country relation **16239**, the administrative boundary of Austria. Assemble
   its outer/inner member ways into validated GeoJSON. Reject missing members,
   unclosed rings, nested border relations or invalid geometry; never repair or
   expand the border silently. Cache the XML and record relation version.
2. Geofabrik's dated **austria-261101.osm.pbf** road snapshot, dated 1 November 2026.
   Verify its published MD5 before preprocessing; record SHA-256 hashes locally.
   This is a fixed historical snapshot, not a claim of live road updates.
3. Use the same pinned OSRM v5.27.1 image digest for all stages:
   `osrm-extract -p /opt/car.lua`, `osrm-partition`, `osrm-customize`, each with
   two threads. The algorithm is **MLD** for both preparation and serving.

Compose is used for preparation containers as well, avoiding Windows bind-mount
quoting problems with the spaces in this repository path. Commands stop on failure.
`data/osrm/sources.json` records source hashes and the boundary source/version;
`prepared.json` additionally records the image, profile, algorithm and preparation
completion time. Preserve this directory privately to reproduce the same dataset.
The first boundary download is current at download time; later runs reuse it.
It is independent of the dated road snapshot and may cause conservative border
rejections if the sources differ.

Optional stages for debugging or resuming:

```powershell
# Boundary only, no road download or Docker preprocessing:
uv run --frozen python -m scripts.prepare_osrm --boundary-only
# Download and verify data, without preparing the graph:
uv run --frozen python -m scripts.prepare_osrm --download-only
# Use already verified downloads and sources.json:
uv run --frozen python -m scripts.prepare_osrm --prepare-only
```

Completed downloads are reused. Interrupted downloads use `.part` files and restart
on the next run. Checksum mismatch is an error, not permission to use a partial map.
Stop the stack before changing any data. Do not mix graphs built by different
OSRM versions/profiles. A map update is an explicit new snapshot and preparation;
there is no background download job.

## Start and test the real services

```powershell
docker compose config --quiet
docker compose up --build -d
docker compose ps
uv run --frozen python -m scripts.check_local_route --wait-seconds 120
```

Compose's worker uses `http://osrm:5000`, Docker's internal service DNS name.
The host uses `http://127.0.0.1:5000`. OSRM reads graph files read-only; the worker
reads the boundary read-only. Ports are bound to localhost. The preparation
service runs only when explicitly selected and is not a persistent third worker.

`depends_on` orders startup, but does not mean that OSRM has finished loading.
The route-check command retries actual planning requests for startup readiness;
no fixed sleep or fake route substitutes for a successful response.

Normal station retrieval is now local. Before testing a low-fuel trip on an existing
installation, run `uv run --frozen python -m scripts.prepare_stations`, then
`docker compose up -d --build worker`. This uses a Linux preparation container to
avoid Windows native DLL restrictions. Full map preparation includes this step.
See [station retrieval](station-retrieval.md) for complete details.

The direct example has 35 L in a 40 L tank, consumption 6 L/100 km and a 3 L
arrival reserve. Once the Austria graph is ready it should return `direct`, real
geometry, litres used through the arrival-fuel calculation, and zero stations
compared. This is independent of Overpass and prices.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/live
Invoke-RestMethod http://127.0.0.1:8000/health/ready
$trip = Get-Content examples/direct-trip.json -Raw
$result = Invoke-RestMethod -Uri http://127.0.0.1:8000/plan -Method Post -ContentType "application/json" -Body $trip
$result | ConvertTo-Json -Depth 20
```

The raw OSRM endpoint can be inspected independently. This bypasses the worker's
country policy; accepted application routes must always go through `/plan`:

```powershell
$url = 'http://127.0.0.1:5000/route/v1/driving/16.3692842,48.2024484;15.4392,47.0667?overview=full&geometries=geojson&steps=false&radiuses=100;100'
Invoke-RestMethod $url | ConvertTo-Json -Depth 20
```

In the interactive [API docs](http://127.0.0.1:8000/docs), use POST /plan and
paste either example JSON. Test a low-fuel trip separately:

```powershell
$trip = Get-Content examples/trip.json -Raw
Invoke-RestMethod -Uri http://127.0.0.1:8000/plan -Method Post -ContentType "application/json" -Body $trip
```

Missing/invalid local station catalogues return `stations_unavailable` with a safe reason. If feasible candidates exist
but no authorised fresh quotes are available, expect `prices_unavailable`. A trip
with no feasible returned stop produces `no_feasible_stop`. None of these outcomes
is replaced by synthetic routes. The station-only diagnostic remains:

```powershell
uv run --frozen python -m scripts.check_live_stations
```

This validates endpoints and reads the local station catalogue. Add
`--source overpass` only to explicitly diagnose the optional online source.

## Local Python instead of a worker container

After preparing the graph:

```powershell
docker compose up -d osrm
$env:OSRM_BASE_URL = 'http://127.0.0.1:5000'
$env:AUSTRIA_BOUNDARY_FILE = 'data/osrm/austria-boundary.geojson'
uv run --frozen python -m uvicorn fuel_stop.app:app --host 127.0.0.1 --port 8000
```

Run only one worker listener on port 8000. The `python -m` form avoids the Windows
Application Control issue with the Uvicorn executable launcher. Virtual-environment
activation is optional when using uv. Local Python does not automatically load
`.env`; set environment variables in the server's terminal before starting it.

Compose automatically reads an optional `.env` for station/price settings. Leave
prices blank until permission is obtained. Compose overrides routing URL/boundary
path to its internal values, so stale hosted-routing settings do not change routing.
No `.env` file is required for default local routing.

## Troubleshooting and stopping

```powershell
docker compose logs --tail 100 osrm worker
docker compose down
```

| Symptom | Meaning and action |
|---|---|
| Port already in use | Stop the old listener; do not start a second Uvicorn on port 8000 |
| OSRM exits with missing `.osrm.*` | Preparation did not finish or the wrong directory is mounted; rerun preparation after stopping the stack |
| `boundary_unavailable` | Local country file missing/invalid; run boundary preparation and restart the worker |
| `routing_unavailable` | Local OSRM cannot be reached, is still loading, or returned malformed data; inspect its logs |
| `outside_austria` | An input endpoint is outside the saved country boundary |
| `border_restricted_route` | The returned direct road geometry leaves Austria; no foreign transit is accepted |
| `no_route` | OSRM found no route or no drivable segment within 100 m of an endpoint |
| `stations_unavailable` | Check the reason: local catalogue missing/invalid, or an explicitly selected online provider failure |
| `prices_unavailable` | Feasible stations lack usable authorised price quotes |

`/health/live` checks the process. `/health/ready` validates the local boundary
and configured station source, then probes the OSRM graph. It returns 503 when
any of those checks fails. A price feed is optional for readiness. The boundary
is cached after loading, so restart the worker after replacing it.

## Monitoring, CI and Kubernetes boundary

The existing `/metrics` endpoint and provider failure/latency metrics remain.
Grafana queries are in [monitoring](monitoring.md). No monitoring stack is installed.
CI runs offline tests, validates Compose, checks the dependency export, builds the
worker and checks container liveness. It does not download the country map.

The [Kubernetes guide](kubernetes.md) describes the full single-node kind setup
in `deploy/kubernetes/local`, including OSRM, routing storage, PostgreSQL, the
public API and controller. The reusable worker manifest has moved to
`deploy/kubernetes/base/worker.yaml`. The local overlay uses one worker; two-laptop
execution still requires a separate storage/network/image distribution setup.

## Sources

- [OSRM preparation pipeline](https://github.com/Project-OSRM/osrm-backend)
- [Geofabrik Austria snapshots](https://download.geofabrik.de/europe/austria.html)
- [OSM full-relation endpoint](https://wiki.openstreetmap.org/wiki/API_v0.6#Full:_GET_/api/0.6/%5Bway%7Crelation%5D/%23id/full)
- [OSM attribution and ODbL](https://www.openstreetmap.org/copyright)
