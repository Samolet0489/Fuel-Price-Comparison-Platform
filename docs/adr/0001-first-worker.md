# ADR 0001: Build the Austrian fuel-stop planner as the first worker

- Date: 21 November 2026
- Status: accepted for the first-component implementation; live data acceptance pending

Routing update: the historical hosted-routing choice and key dependency below
are superseded by [ADR 0002](0002-local-osrm.md). Current operation uses local
OSRM with local country checks. The single-worker scope and price-source decision
remain applicable.

## Context

The supplied execution-plan PDF is a planning draft for a broader platform. It
proposes French historical data, prediction workers, a persistent job API,
carbon-aware placement, monitoring and a two-laptop K3s cluster. Its defaults are
explicitly provisional. The user's current implementation scope is one component:
an Austrian single-stop trip planner with real routing and permissioned prices.

The current request determines scope. The PDF is background architecture evidence,
not an instruction to implement its entire task list or meet its milestones in
this change. This worker does not establish a claimed "50% completed" checkpoint.

## Decision

Use Python/FastAPI for a stateless synchronous `/plan` worker. Separate typed
contracts, pure calculation logic and HTTP providers so tests use saved cases
without replacing production adapters. Use OpenRouteService driving routes and
real Austrian OpenStreetMap station candidates via Overpass. Restrict this version
to explicitly tagged public 24/7 stations; expose that scope in results.

Treat permissioned Austrian prices as an early unresolved dependency. E-Control's
FAQ disallows third-party reuse and provides no officially released API. Provide
a feed contract and fail explicitly until a permitted supplier is integrated.

Provide Docker, two-replica Kubernetes configuration, Prometheus metrics, basic CI
and explanation documents for this component. Do not add a database, forecast
model, public UI, carbon controller or automatic deployment now.

## Alternatives

- Implement the PDF's Lyon forecast worker first: inconsistent with the user's
  current Austrian trip-planning priority.
- Use straight-line distances as driving distances: cannot establish road
  reachability or detour fuel use.
- Scrape private price endpoints: cannot establish permission for the final service.
- Embed prices and routes as production data: useful for tests, misleading for
  live decisions unless explicitly labelled replay.
- Build all platform services now: exceeds the requested first-component scope.

## Consequences

The arithmetic is explainable and repeatable before external access is complete.
The future API can call the same HTTP contract on either Linux node. A valid live
key and licensed price feed are still required for live acceptance. External
provider quotas, stale OSM tags, station identity mapping and sequential routing
are known integration constraints.

For this initial stateless component one Deployment has two replicas, forced onto
different nodes. The PDF's separately addressable worker Deployments are still
needed if a later controller must drain and relocate individual workers. That
controller's placement rules will need to replace the initial anti-affinity policy;
they have not been implemented here.

## Evidence to collect next

1. A permitted supplier and recorded terms for Austrian prices.
2. A successful small real-station lookup and ORS route using an actual key.
3. A complete priced trip with timestamps, station mapping and checked arithmetic.
4. One request on each physical laptop with worker/node identity recorded.
5. Prometheus scrapes and Grafana panels showing plan latency and induced upstream failure.

Reference: [E-Control FAQ](https://www.e-control.at/web/guest/spritpreisrechner).
