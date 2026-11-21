# Fuel Price Comparison Platform

This repository implements the first component: an **Austrian single-stop fuel
planner**. It uses **local OSRM in Docker** for real road routing. No routing
account or API key is required. Forecasting, the public backend, map UI, carbon
controller and database remain outside this component.

## Run it on Windows

Start Docker Desktop with Linux containers. Stop any old Uvicorn server on port
8000 with Ctrl+C. Run these commands from PowerShell:

```powershell
cd "C:\0_Univercity\AI Architecture\THE project\Project\Fuel-Price-Comparison-Platform"
uv sync --frozen
uv run --frozen pytest -q
```

Prepare the road data once. This downloads a dated Austria snapshot (roughly
0.8 GB), downloads the OSM country boundary, verifies the road checksum, and runs
OSRM extraction, partitioning and customization. Allow substantial disk/RAM and
time; the full-country resource requirements have not been measured here.

```powershell
uv run --frozen python -m scripts.prepare_osrm
if ($LASTEXITCODE -ne 0) { throw "Data preparation failed; do not start the stack" }

docker compose up --build -d
uv run --frozen python -m scripts.check_local_route --wait-seconds 120
```

The route check sends a real Vienna-to-Graz trip with enough fuel for a direct
journey. Expected: `status=direct`, `mode=live`, nonzero road distance and duration,
GeoJSON geometry, and `stations_compared=0`. It requires neither Overpass nor
prices. **The full-country Vienna-to-Graz check remains to be run after preparing
the full map; the verified real smoke test used a small Vienna street extract.**

Open [API documentation](http://127.0.0.1:8000/docs). Test manually:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/live
Invoke-RestMethod http://127.0.0.1:8000/health/ready
$trip = Get-Content examples/direct-trip.json -Raw
Invoke-RestMethod -Uri http://127.0.0.1:8000/plan -Method Post -ContentType "application/json" -Body $trip
```

A low-fuel trip tests the additional station/price dependencies:

```powershell
$trip = Get-Content examples/trip.json -Raw
Invoke-RestMethod -Uri http://127.0.0.1:8000/plan -Method Post -ContentType "application/json" -Body $trip
```

This can return `stations_unavailable` if Overpass fails, or `prices_unavailable`
when feasible stations exist without authorised quotes. Local OSRM does not fix
these independent dependencies. No price supplier is configured by default.

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
| Real local routing | Verified through Compose on a small Vienna OSM extract, including a two-leg route |
| Austria-only policy | Local OSM boundary validation of inputs, snapped waypoints and the entire route line |
| Austria-wide map preparation | Reproducible script and pinned image; full-country processing not run here |
| Station lookup | Existing real Overpass adapter; external availability still unresolved |
| Current Austrian prices | Authorised source still required; no private API scraping |
| Monitoring | Existing Prometheus metrics and Grafana queries retained |
| Kubernetes | Worker-only integration template; cluster OSRM/data provisioning still required |
| CI | Offline tests, Compose validation and worker image smoke test; GitHub run pending |

## Understand and present it

- [Component walkthrough](docs/component-guide.md): adapter mapping, formulas, worked example and errors.
- [Windows deployment guide](docs/deployment.md): preparation, Compose, local Python and troubleshooting.
- [Austria-only routing decision](docs/adr/0002-local-osrm.md): why a country extract alone is insufficient.
- [Authorised price dependency](docs/price-source.md): feed contract and access requirement.
- [Verification record](docs/verification.md): measured results and untested items.

Normal routing is OSRM's fastest car route. The worker rejects a returned route
that leaves Austria; OSRM cannot request an Austria-only alternative. Near borders
this can conservatively reject a trip even when another domestic road route exists.
This is explicit, not a claim of complete domestic route search.

Runtime configuration is in `.env.example`. Old `ORS_API_KEY` and `ORS_BASE_URL`
settings are no longer used. Compose fixes the internal URL to `http://osrm:5000`;
local Python defaults to `http://127.0.0.1:5000`. Keep credentials in ignored `.env`.

Dependencies are locked in `uv.lock`; Docker uses the runtime export
`requirements.lock`. After changing dependencies, run `uv lock`, then:

```powershell
uv export --frozen --no-dev --no-hashes --no-emit-project --format requirements-txt --output-file requirements.lock
```

Data: [OpenStreetMap contributors, ODbL](https://www.openstreetmap.org/copyright).
Show the returned attribution in the future map. OSRM is BSD-2-Clause licensed.
