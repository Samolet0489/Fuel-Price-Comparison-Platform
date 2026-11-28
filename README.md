# Fuel Price Comparison Platform

This repository implements an **Austrian fuel planner** and a public
trip API backed by **PostgreSQL**. The controller dispatches trip jobs to the
planner, which uses **local OSRM in Docker** for real road routing. No routing
account or API key is required. The map UI submits trips through the public API.
Production carbon-aware dispatch is future work. The optional local placement
lab demonstrates worker relocation using simulated regional conditions.

## Run it on Windows

Start Docker Desktop with Linux containers. Stop any old Uvicorn server on port
8000 with Ctrl+C. Run these commands from PowerShell:

```powershell
cd "C:\0_Univercity\AI Architecture\THE project\Project\Fuel-Price-Comparison-Platform"
uv sync --frozen
uv run --frozen pytest -q
```

Prepare the road data once. This downloads a dated Austria snapshot (roughly
0.8 GB), downloads the OSM country boundary, verifies the road checksum, extracts
a local station catalogue, and runs OSRM extraction, partitioning and customization. Allow substantial disk/RAM and
time; the full-country resource requirements have not been measured here.

```powershell
uv run --frozen python -m scripts.prepare_osrm
if ($LASTEXITCODE -ne 0) { throw "Data preparation failed; do not start the stack" }

docker compose up --build -d --wait --wait-timeout 120
uv run --frozen python -m scripts.check_local_route --wait-seconds 120
```

The route check sends a real Vienna-to-Graz trip with enough fuel for a direct
journey. Expected: `status=direct`, `mode=live`, nonzero road distance and duration,
GeoJSON geometry, and `stations_compared=0`. It requires neither Overpass nor
prices. The updated example coordinates have been verified on the full local map: a real
Vienna-to-Graz route of 192.4779 km. See the station verification below for low-fuel trips.

Open [API documentation](http://127.0.0.1:8000/docs). Test manually:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/live
Invoke-RestMethod http://127.0.0.1:8000/health/ready
$trip = Get-Content examples/direct-trip.json -Raw
Invoke-RestMethod -Uri http://127.0.0.1:8000/plan -Method Post -ContentType "application/json" -Body $trip
```

### Submit trips through the public API

Open the [trip map](http://127.0.0.1:8001/) after Compose starts. It automatically
plans the low-fuel Vienna-to-Graz example. Choose from 20 Austrian city presets
or use **Pick on map** next to either endpoint and click a road to place a pin.
With the map focused, you can pan with the arrow keys, press Enter to choose
its center, or press Escape to cancel. Select **All stations** to show the full local
catalogue and click a station for its fuel types and opening hours. The map
background uses online OpenStreetMap tiles, while route calculation and station
lookup run locally.

Open [public API documentation](http://127.0.0.1:8001/docs). A
`POST /trip-plans` accepts the same inputs as the worker and returns HTTP
202, a job ID, and a `Location` header. A `GET /trip-plans/{id}` returns
`QUEUED`, `RUNNING`, `SUCCEEDED`, or `FAILED`, with timestamps and the
finished result or error. `prices_unavailable` is a successful planning
outcome when the worker found a feasible stop.

Refueling results include an ordered `stops` array with each station, arrival
fuel, amount to buy, and departure fuel. The map numbers the stops on the route.
Trips with one stop still include the top-level `station` field. Multi-stop
search uses the local OSM catalogue and checks road reachability, the fuel buffer
at each stop, the destination reserve, and the trip's total detour limit.
With usable quotes, it compares the cost of buying across multiple stations
against any priced single stop. The response flags incomplete price comparisons.

To try a trip that needs two stops, select Vienna to Graz and enter diesel,
8 L of current fuel, an 8.5 L tank, consumption of 10 L/100 km, a 2 L arrival
reserve, and a 15 km maximum detour. With the prepared Austrian catalogue, expect
at least two numbered stations and a planned purchase at each one.

```powershell
uv run --frozen python -m scripts.check_trip_jobs
```

This sends both example trips through the API and controller. With the Austrian
map and station catalogue prepared, the low-fuel trip should return a priced
plan. Select **Regenerate prices** in the map toolbar to replace the generated
prices for every station and recalculate the trip.

Jobs have a 90-second deadline including queue time. An attempt can run for up
to 30 seconds. A timeout or transient failure can retry once on a different
ready worker. The default Compose stack has one worker, so a failed attempt can
only retry if a different ready worker becomes available before the deadline.
The same job ID remains valid across attempts. PostgreSQL uses a named volume
that persists across `docker compose down`. The API listens on localhost port
8001, the worker on port 8000, and PostgreSQL has no published host port.

A low-fuel trip tests the additional station/price dependencies:

```powershell
$trip = Get-Content examples/trip.json -Raw
Invoke-RestMethod -Uri http://127.0.0.1:8000/plan -Method Post -ContentType "application/json" -Body $trip
```

Normal station lookup now uses a local OSM catalogue, avoiding public Overpass
capacity failures. The generated prices are the default data source. The app
supports optional observed prices through the configuration in the price guide.

For an existing map installation, build the catalogue without rebuilding OSRM:

```powershell
uv run --frozen python -m scripts.prepare_stations
docker compose up -d --build worker
```

[Station retrieval diagnosis and fix](docs/station-retrieval.md) explains the source,
map timestamp, eligibility rules and Docker-based preparation.

```powershell
docker compose logs --tail 100 osrm worker
docker compose down
```

Downloaded maps and generated routing files live under ignored `data/`; stopping
the containers does not remove them. Maps never enter the worker image.

## Test without downloads or Docker

```powershell
uv run --frozen pytest -q
uv run --frozen python -m tests.test_planner
```

The replay is explicitly synthetic: it buys 7 L at EUR 1.60/L for EUR 11.20.
Production never falls back to replay routes or a public demo routing server.

## What is ready and what remains

| Area | State |
|---|---|
| Fuel calculation, feasibility and cost ranking | Preserved; saved regression tests |
| OSRM adapter | Implemented; distances, durations, legs, full GeoJSON and errors tested |
| Real local routing | Verified small-area two-leg routing and full-map Vienna-to-Graz routing |
| Austria-only policy | Local OSM boundary validation of inputs, snapped waypoints and the entire route line |
| Austria-wide map preparation | Existing full map and real route verified; station extraction now tested on that PBF |
| Station lookup | Real local OSM catalogue; 465 eligible Austrian stations, no runtime HTTP lookup |
| Fuel prices | Generated prices for every station with a regeneration button, plus optional observed prices |
| Monitoring | Existing Prometheus metrics and Grafana queries retained |
| Placement lab | Separate virtual-region cluster with simulated price/carbon controls, real worker scheduling, Prometheus and Grafana |
| Kubernetes | Full single-node kind deployment and live verification scripts; see the Kubernetes guide for verification status and multi-node limits |
| CI | Offline tests, Compose validation and worker image smoke test; GitHub run pending |

## Understand and present it

- [Component walkthrough](docs/component-guide.md): adapter mapping, formulas, worked example and errors.
- [Windows deployment guide](docs/deployment.md): preparation, Compose, local Python and troubleshooting.
- [Kubernetes guide](docs/kubernetes.md): isolated local cluster, startup, storage and recovery checks.
- [Regional placement lab](docs/simulation.md): simulated conditions, real worker relocation, Prometheus and Grafana.
- [Austria-only routing decision](docs/adr/0002-local-osrm.md): why a country extract alone is insufficient.
- [Authorised price dependency](docs/price-source.md): feed contract and access requirement.
- [Verification record](docs/verification.md): measured results and untested items.
- [Station retrieval](docs/station-retrieval.md): local catalogue, source date and known limits.

Normal routing is OSRM's fastest car route. The worker rejects a returned route
that leaves Austria; OSRM cannot request an Austria-only alternative. Near borders
this can conservatively reject a trip even when another domestic road route exists.
This is explicit, not a claim of complete domestic route search.

Runtime configuration is in `.env.example`. Old `ORS_API_KEY` and `ORS_BASE_URL`
settings are no longer used. Compose fixes the internal URL to `http://osrm:5000`;
local Python defaults to `http://127.0.0.1:5000`. Keep credentials in ignored `.env`.

Dependencies are locked in `uv.lock`; Docker uses the runtime export
`requirements.lock`. Data preparation has its own `requirements-data.lock` and
Linux Docker image. After changing dependencies, run `uv lock`, then:

```powershell
uv export --frozen --no-dev --no-hashes --no-emit-project --format requirements-txt --output-file requirements.lock
uv export --frozen --no-dev --group data --no-hashes --no-emit-project --format requirements-txt --output-file requirements-data.lock
```

Data: [OpenStreetMap contributors, ODbL](https://www.openstreetmap.org/copyright).
Show the returned attribution in the future map. OSRM is BSD-2-Clause licensed.
