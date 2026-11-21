# Latest verification: station retrieval fix, 21 November 2026

## CI container startup check update

The GitHub run supplied by the user exited with curl 56 immediately after
`docker run -d`. The original probe retried connection refusal, but not connection
resets or empty replies while the application was still starting.

Local reproduction against a fresh CI image returned curl 52 (empty reply) while
the container was running. On the same container, the corrected bounded probe
retried those startup errors and returned HTTP 200 with `status=ok`. Logs confirmed
normal Uvicorn startup on `0.0.0.0:8000`; no application code or binding change
was needed. An intentionally exited test container still failed the corrected
probe with a nonzero curl exit after about 30.5 seconds, preserving failure detection.
Both isolated test containers were removed; the existing application was untouched.

The workflow now uses `--retry-all-errors`, connection/request/retry timeouts and
a two-minute step timeout. Separate diagnostic and unconditional cleanup steps
retain logs and container state before removal, including when the probe fails.
The next GitHub Actions run must still be observed after this workflow change is
pushed. Local reproduction used Windows curl, so GitHub's Ubuntu runner has not
yet executed the revised command. No commit or push was made by the agent.

Reference: [curl retry options](https://curl.se/docs/manpage.html#--retry-all-errors).

## Station retrieval verification

Normal station lookup now uses the existing local Austria PBF rather than public
Overpass. The previous migration record below is historical; its pending station
lookup and Vienna-to-Graz checks are superseded by these results.

- Captured the actual Overpass HTTP 502 response: query workers busy and queue full.
- Prepared a real local catalogue from the full Austria PBF in Linux Docker:
  2,965 fuel objects, 465 eligible Austrian stations, 3 outside-country exclusions,
  and 1 invalid/incomplete geometry exclusion. Map time: 2026-11-01T21:21:36Z.
- Windows blocks the native osmium DLL. The preparation image includes its XML
  runtime libraries; normal Windows dependencies and the worker do not include it.
- Rebuilt the actual worker against the existing local OSRM. The direct example
  returned HTTP 200 `direct` in 0.73 s. The low-fuel example returned HTTP 200
  `prices_unavailable`, 178 compared stations, 50 feasible stations and zero priced
  stations in 6.93 s. These are one-run timings, not performance guarantees.
- The current examples use tested road-accessible Vienna/Graz points. Full-map
  routing works; the prior unsuccessful sample coordinates have been replaced.
- Linux Docker suite: 80 passed. Windows suite: 79 passed, 1 native extraction
  test skipped. One existing Starlette/httpx deprecation warning remains.
- Catalogue metadata is exposed as station_source/station_data_timestamp when
  stations are evaluated. Runtime catalogue access makes no external HTTP call.
- Authorised Austrian prices, two-laptop Kubernetes execution, deployed Grafana
  and a successful GitHub Actions run remain pending. No commit or push was made.

See [station retrieval](station-retrieval.md) for commands and known source limits.

---

# Earlier record: local OSRM migration, 21 November 2026

## Verified

- Inspected repository source, tests, configuration, deployment files and ancestor
  instruction locations. No applicable local AGENTS.md was found.
- `uv run --frozen pytest -q`: **65 passed**. This includes the existing saved fuel
  cases and new OSRM, local-boundary, malformed-response, timeout and API tests.
  One upstream Starlette/httpx test-client deprecation warning remains.
- The saved synthetic replay still chooses 7.000 L at EUR 1.60/L = EUR 11.20,
  arriving with 3.000 L and comparing two candidates. Fuel formulas/ranking are
  unchanged; only endpoint validation moved to the router's local country check.
- Pulled OSRM v5.27.1 and pinned Compose to the resolved image digest:
  `sha256:855614a38f464b0558a2ad6eaa7cb8c139f39887da9b38b485ce453c6e6e6124`.
  Inspected extraction/partition/customization command support in that image.
- Downloaded and assembled the actual Austrian OSM country relation 16239,
  version **1493**. It is a valid MultiPolygon with bounds
  `(9.5307327, 46.3722987, 17.160776, 49.0205249)`. Vienna is inside; Munich is
  outside. The country file remains under ignored local data, not in Git.
- Downloaded one small real Vienna OSM street extract from the official OSM map
  API for bbox `16.365,48.201,16.375,48.205` (4,709,910 XML bytes). The larger first
  map request was rejected, so the smoke test used the smaller area explicitly.
- Ran `osrm-extract`, `osrm-partition`, `osrm-customize` with the pinned image and
  car profile on that small real extract. All three stages succeeded.
- Built the worker and started an isolated stack using the actual `compose.yaml`
  plus an ignored override for the smoke graph and ports 15000/18081. This avoided
  interfering with any existing server on port 8000.
- A real request through worker -> local OSRM on Vienna's Opernring returned:
  `status=direct`, `mode=live`, **0.2694 km**, **26.3 seconds**, **16 geometry points**,
  arrival fuel **34.983836 L** from 35 L at 6 L/100 km, and **zero station lookups**.
  Points: `(16.3692842,48.2024484)` -> `(16.3658369,48.2031961)`.
- A real three-waypoint request through the adapter returned two legs:
  **0.078 km + 0.1914 km**, with **17 geometry points**. The via point was
  `(16.3682888,48.202671)`, a road waypoint used to test routing, not a claimed
  real fuel station. The full country-geometry check passed both real routes.
- Saved that raw OSRM response with OSM attribution and provenance in
  `tests/fixtures/osrm-vienna.json`; an offline regression test consumes it.
- A disconnected route in the tiny extract returned a genuine no-route outcome;
  it was not replaced with a straight line or a fabricated success.
- Final real-container checks passed readiness and metrics HTTP 200. A Munich
  origin returned HTTP 422 `outside_austria` through the real local boundary.
- `docker compose config --quiet` and `git diff --check` passed.
- Git ignore checks confirm the country XML/GeoJSON, road downloads, graph files
  and smoke-test files under `data/` are excluded. Worker image context is still
  restricted to source and runtime dependencies.

Windows sandbox restrictions block network/DNS and the test client's internal
socket in some commands. Network, Docker and HTTP checks were run with the
required execution permission; no application fallback was introduced.

## Not verified or still external

- The full **austria-261101.osm.pbf** has not been downloaded or processed here.
  The documented full-country preparation workflow and Vienna-to-Graz route must
  be run on the target machine. Full-map disk/RAM/time and country-wide coverage
  are unmeasured. A small Vienna route does not establish those results.
- Automated border tests use explicit synthetic polygons to check foreign holes,
  exit/re-entry and snapped points. The actual boundary passed city checks and
  real Vienna route checks. Real border-region travel acceptance and rejection
  have not been surveyed or tested comprehensively.
- Overpass station lookup availability remains a separate unresolved external
  dependency. Direct sufficient-fuel trips no longer require it at all.
- An authorised Austrian price supplier is still required for current cost ranking.
  No prices were fabricated or scraped to claim live end-to-end completion.
- The Kubernetes worker template now requires a separately provisioned OSRM Service
  and boundary PVC. Two-laptop deployment and Grafana ingestion are still untested.
- GitHub Actions execution remains pending. No commit or push has been performed.

The temporary Compose smoke stack is stopped after final verification. Local ignored
smoke data is retained for inspection and cannot be mistaken for the full-country
`data/osrm/austria.osm.pbf` dataset.
