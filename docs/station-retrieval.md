# Station retrieval: diagnosis and fix

## Cause observed on 21 November 2026

The real query returned **HTTP 502** from the public Overpass endpoint. Its response
said all query workers were busy and its queue was full. The old adapter collapsed
that upstream error into `stations_unavailable`, hiding the reason. This was an
external capacity failure, not a fuel arithmetic failure.

## Normal operation now uses the local map

Extract a small JSON station catalogue from the same downloaded Austria PBF used
for road routing. The worker reads it directly; it needs no network request for
station lookup. Missing/corrupt files fail explicitly. There is no automatic
fallback to Overpass, synthetic stations or fabricated prices.

Windows Application Control blocked the native OSM reader DLL. Therefore data
extraction runs in the `station-prepare` Linux Docker container. It runs only when
requested, writes the catalogue, then exits. The worker does not install osmium.
Its existing HTTP/routing dependencies are unchanged.

`scripts/extract_stations.py` streams OSM objects and resolves only the node
coordinates needed by eligible station ways/relations, rather than keeping every
road node in memory. Nodes retain their location. Ways and multipolygon relations
use a representative point on their geometry. Incomplete geometry is counted in
the extraction report. Every retained point must lie in the local Austrian boundary.

The eligibility policy is unchanged: `amenity=fuel`, explicit `fuel:diesel=yes`
or `fuel:octane_95=yes`, `opening_hours=24/7`, and no known restricted access,
disused or abandoned tags. Unknown hours/fuel are excluded. OSM tags do not prove
current pump service, payment compatibility or absence of a temporary closure.
One physical station may have multiple OSM representations; provider ID mapping
remains important before live price ranking.

The catalogue records map time, extraction time and source/boundary hashes. Results
that compare stations include `station_source` and `station_data_timestamp`.
The current snapshot is **2026-11-01T21:21:36Z**, not a claim of current station
observations. `mode=live` continues to mean real routing/data rather than synthetic
replay; inspect the separate map timestamp for station freshness.

## Commands for your existing installation

From the repository in PowerShell, with Docker Desktop running:

```powershell
uv sync --frozen
uv run --frozen python -m scripts.prepare_stations
if ($LASTEXITCODE -ne 0) { throw "Station preparation failed" }
docker compose up -d --build worker
uv run --frozen python -m scripts.check_live_stations --full-range
```

This reuses `data/osrm/austria.osm.pbf` and the boundary. It does not download the
map or rebuild OSRM. First use builds the data-tools image; later runs reuse its
layers. The catalogue is replaced atomically, so the running worker never reads
a partially written JSON file. Normal full preparation (`scripts.prepare_osrm`)
now includes this extraction step too.

`data/osrm/stations.json` is ignored by Git and mounted read-only by the worker.
Configuration is `STATION_SOURCE=local`, `STATION_CATALOGUE_FILE=/data/stations.json`
in Compose, or `data/osrm/stations.json` for local Python. Kubernetes's existing
worker template expects that file on its routing-data PVC as well.

The examples now use the tested road-accessible Vienna and Graz coordinates:

```powershell
$trip = Get-Content examples/trip.json -Raw
$result = Invoke-RestMethod -Uri http://127.0.0.1:8000/plan -Method Post -ContentType "application/json" -Body $trip
$result | Select-Object status, stations_compared, feasible_stations, priced_stations, station_source, station_data_timestamp
```

The verified result is HTTP 200, `prices_unavailable`, **178 compared**, **50
feasible**, and zero priced stations. This establishes working station retrieval
and actual road feasibility evaluation; it does not establish a cheapest priced
recommendation. Authorised Austrian prices are still required.

## Optional Overpass diagnostic

```powershell
uv run --frozen python -m scripts.check_live_stations --source overpass
```

This explicitly uses the online source and can still fail while the server is
overloaded. Errors now include a safe reason such as `upstream_http_502`,
`upstream_timeout` or `upstream_incomplete_response`; logs omit credentials,
full URLs and query bodies. Local catalogue errors use
`local_catalogue_missing_or_unreadable` or `local_catalogue_invalid`.
An empty valid catalogue is not treated as a service outage.

Local Python may explicitly select `STATION_SOURCE=overpass` for diagnostics.
Compose deliberately fixes normal operation to `local`. No endpoint-hopping or
retry loop disguises the capacity problem.

## Verification

- Real source: 2,965 fuel objects examined; **465 eligible Austrian stations**.
- Extraction excluded 3 points outside Austria and 1 object with incomplete/invalid
  geometry. Most other fuel objects fail the existing conservative tag policy.
- Updated real worker: direct trip succeeded in about 0.73 s; low-fuel trip compared
  178 stations and found 50 feasible stops in about 6.93 s. These are one-run timings.
- **80 tests passed in Linux Docker**, including the native PBF extraction test for
  nodes, ways and relations. Windows: 79 passed, 1 native test skipped. The native
  reader belongs in Docker on this machine; do not disable Application Control.
- The prior fuel calculations and ranking cases still pass.

OSM data remains subject to [ODbL attribution](https://www.openstreetmap.org/copyright).
Technical references: [pyosmium streaming](https://docs.osmcode.org/pyosmium/latest/user_manual/01-First-Steps/),
[public Overpass capacity guidance](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html).
