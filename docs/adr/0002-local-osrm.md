# ADR 0002: Local OSRM and conservative Austria-only route acceptance

- Date: 21 November 2026
- Status: implemented; real small-area routing verified, full Austria preparation pending
- Supersedes: ADR 0001's hosted routing choice and routing-key requirement

## Context and decision

The user needs real car routing without registration, keys or public-demo bulk
requests. Run OSRM locally with the same pinned Docker image for extraction,
partitioning, customization and MLD serving. The worker consumes its normal HTTP
API and returns the unchanged planner Route model. Fuel arithmetic and cost
ranking are unchanged. No hosted or synthetic fallback exists.

Download a dated Geofabrik Austria road PBF, verify its source checksum, and
record SHA-256 hashes. Download OSM country relation 16239 separately, assemble
its actual outer and inner border ways, reject incomplete rings and store the
validated polygon locally. The extract's geographical extent is not substituted
for the administrative border. Source data and graphs remain outside Git.

## Border policy: what is preserved and what differs

Both trip endpoints must be inside or on the local Austrian boundary. Every
candidate waypoint, OSRM-snapped waypoint and the **entire** returned full-resolution
LineString must be covered by it. Checking only vertices would miss an excursion
across a narrow foreign area between vertices. Shapely tests the entire line.
No outward buffer or tolerance silently permits foreign transit. Snapping is
limited to 100 m and snapped points must independently pass the boundary check.

This is **route rejection**, rather than a claim that OSRM avoids borders while
searching. Standard OSRM has no equivalent of the previous provider's
`avoid_borders=all`. It finds the fastest car route on its loaded graph. Country
extracts can include complete ways crossing outside the nominal extraction area.
Therefore an Austria extract alone would silently weaken the old requirement.

If the returned direct route crosses a border, the API returns HTTP 422
`border_restricted_route`, explicitly stating that a different domestic route may
exist. For a station route, the candidate is excluded through the existing
NoRoute feasibility path. This does not prove that no domestic alternative exists.
No cross-border route is returned as an accepted plan.

OSM boundary precision, road precision, GeoJSON coordinate rounding, border roads,
enclaves and discrepancies between the dated road data and boundary snapshot can
cause conservative rejection near borders. Boundary-covering geometry includes
the exact boundary line, but offers no claim of surveyed legal precision. A future
graph explicitly clipped to the verified boundary could recover domestic
alternatives; that preprocessing change has not been implemented here.

Missing or invalid boundary data returns `boundary_unavailable` and makes local
readiness fail. It never disables the restriction. The country boundary is cached
after loading; restarting the worker is required after a boundary update.

## Separate dependencies and consequences

Direct trips with sufficient fuel need neither Overpass nor a price feed. A trip
requiring a stop still needs the existing public Austrian OSM station query and
authorised current price quotes. Their errors remain separate from routing errors.
The initial public/explicit-fuel/24-7 station scope is unchanged.

Compose is the supported local setup. The existing Kubernetes file is only a
worker integration template requiring separately provisioned OSRM and boundary
storage. This change does not create the rest of the platform, deploy the cluster,
commit or push source, or establish complete live price ranking.

## Alternatives and evidence

- Continue hosted routing: requires the account/key the user explicitly excludes.
- Use a public OSRM demo: inappropriate for normal station comparisons; no such
  fallback is implemented and the known demo hostname is refused.
- Trust country endpoints or extract coverage only: does not prevent border transit.
- Return synthetic routes when OSRM fails: would misrepresent real road feasibility.

Offline tests cover mapping, malformed responses, missing local data and border
exit/re-entry (including between vertices). A real small Vienna OSM graph has
been prepared and routed through Compose. See [verification](../verification.md)
for measured results and the full-country work still untested.

Sources: [OSRM API](https://project-osrm.org/docs/v5.24.0/api/),
[Geofabrik Austria](https://download.geofabrik.de/europe/austria.html),
[OSM full relation API](https://wiki.openstreetmap.org/wiki/API_v0.6).
