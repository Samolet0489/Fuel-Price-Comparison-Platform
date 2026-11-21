# Early dependency: permission to use Austrian prices

Status: **unresolved**. This prevents calling the worker a complete live cost-ranking
service. Route and station adapters are implemented independently so calculation
work and integration-contract work can proceed.

Checked 21 November 2026: [E-Control's official FAQ](https://www.e-control.at/web/guest/spritpreisrechner)
says third-party reuse is not permitted and no officially released API is available.
An endpoint used by its website is not evidence of permission. This component does
not scrape that site or call its private price endpoints.

## Resolve this dependency first

1. Identify a commercial provider, station operator or other rights holder offering
   Austrian diesel and petrol95 prices with permission for your intended use.
2. Obtain written terms covering retrieval, display, caching, test recordings and
   redistribution. University use alone is not treated as permission here.
3. Confirm coverage, price timestamp meaning, update frequency, rate limits,
   station identities, currencies and allowed retention.
4. Record the provider, agreement reference and permitted uses. Keep private
   contracts and credentials outside Git. Set the reference in runtime configuration.
5. Map provider station IDs to OSM `node/123`, `way/456` or `relation/789` IDs.
   Resolve duplicate OSM representations. Implement a provider-specific adapter
   if the supplier does not serve the contract below.
6. Run a real trip, compare the returned arithmetic with the provider quote,
   and preserve a permitted, dated acceptance record.

No particular commercial provider or permission has been assumed. Do not fabricate
current prices to make the live example appear complete.

## Feed contract implemented now

Configure `PRICE_FEED_URL`, `PRICE_SOURCE` and `PRICE_PERMISSION_REFERENCE`.
`PRICE_FEED_TOKEN` is optional and sent as a Bearer header. The permission reference
is an audit/configuration gate, not a technical verification of legal rights.

The adapter sends `GET <PRICE_FEED_URL>?fuel_type=diesel` (or `petrol95`).
The feed must return JSON in EUR/L. Example below is **synthetic contract data**:

```json
{
  "quotes": [
    {
      "station_id": "node/123",
      "fuel_type": "diesel",
      "eur_per_l": "1.599",
      "observed_at": "2026-11-21T13:00:00+01:00"
    }
  ]
}
```

The timestamp must be the supplier's price observation time, not a fabricated
retrieval timestamp. The adapter records retrieval time separately. The price must
be positive; same-station/same-time conflicting quotes reject the feed. Stale,
future, mismatched-fuel or missing-station quotes cannot win. If some feasible
stations lack prices the result explicitly marks the ranking incomplete.

The one-hour freshness limit and 10 EUR/L validation ceiling are conservative
initial policies, not a claim about source guarantees. Agree them with the real
provider. HTTP errors, missing permission configuration and malformed feeds become
`prices_unavailable` when feasible stations exist. Price failure counts remain
visible in Prometheus.
