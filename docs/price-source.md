# Austrian fuel-price sources

FuelRoute generates the prices it uses for its Austrian station catalogue.
It also supports local pump observations and authorized supplier feeds.

E-Control's official FAQ states that third-party reuse of its Spritpreisrechner
data is prohibited. [E-Control FAQ](https://www.e-control.at/web/guest/spritpreisrechner)

## Generated station prices

Generated prices are active by default in Docker Compose and Kubernetes.
Click **Regenerate prices** in the map toolbar to replace the prices for every
station and supported fuel. All workers use the revision stored in PostgreSQL.
Refreshing the page or restarting the app keeps those prices. A completed
trip retains the estimate it was calculated with. The trip shown in the map
is recalculated after regeneration.

When running the worker and public API directly in Python, set
`PRICE_REVISION_URL=http://127.0.0.1:8001/fuel-prices/revision` on the worker.

To use the optional sources below, set `PRICE_MODE=observed` and recreate the
API and worker. When using Kubernetes, set the variable on both deployments.
To run the example check with observed prices and no configured quotes, pass
`--expect-unpriced` to `scripts.check_trip_jobs`.

## Free local price import

1. Start the app with `docker compose up -d --build --wait --wait-timeout 120`.
   On the map, click **All stations**, select the outlet, and copy its station ID.
2. Record each directly observed pump price in `data/osrm/observed-prices.csv`.
   For a price you just observed, use the commands below. To add another
   observation, repeat the block with its station ID and fuel type.

```powershell
[pscustomobject]@{
    station_id = Read-Host "Station ID from the map"
    fuel_type = Read-Host "Fuel type (diesel or petrol95)"
    eur_per_l = Read-Host "Observed price per litre in EUR"
    observed_at = [DateTimeOffset]::Now.ToString("o")
} | Export-Csv data/osrm/observed-prices.csv -Append -NoTypeInformation -Encoding utf8

uv run --frozen python -m scripts.import_fuel_prices data/osrm/observed-prices.csv
```

For an earlier observation, enter its actual ISO 8601 timestamp with UTC offset
in the CSV. The importer accepts comma-separated CSV and semicolon-separated
spreadsheet exports with decimal commas. It verifies station IDs and fuel types
against the catalogue and keeps the latest observation per station and fuel.

Plan a new trip. The result should show **Price source: Local pump observations**
when it uses a fresh observation at a reachable stop. To compare prices, record
two or more feasible stations for the same trip. The planner uses prices up to
one hour old. The worker picks up the new file without a restart. An explicitly
configured `PRICE_SNAPSHOT_FILE` or `PRICE_FEED_URL` takes precedence.

Preisrunter offers a free non-commercial research plan. Austrian station-price
access or an export has to be requested separately, and its research terms
require linked attribution. Once approved, normalize an export to the same CSV
columns, use `--source` and `--output` for a separate snapshot, and configure
it with `PRICE_SOURCE`, `PRICE_SNAPSHOT_FILE`, and your permission reference.
[Preisrunter research and fuel access](https://preisrunter.at/en/enterprise/)

## TomTom setup

1. Request Fuel Prices access. Confirm that your agreement covers your OSM map,
   station matching, local snapshots, display through the app and its trip API,
   retention, and request limits. The station lookup helper also uses Search
   API. Fuel Prices covers Austria and requires approved access. [Access and API details](https://docs.tomtom.com/fuel-prices-api/documentation/product-information/introduction) · [Market coverage](https://docs.tomtom.com/fuel-prices-api/documentation/product-information/market-coverage)
2. Prepare the Austrian map and station catalogue using the repository's normal
   startup instructions. Set `TOMTOM_API_KEY` and `PRICE_PERMISSION_REFERENCE`
   in your PowerShell session. Use the permission reference in the worker
   configuration too. Store the key outside Git.
3. Find OSM IDs via the local worker's `GET /stations`. For each station you want
   to price, run the lookup below with its OSM ID. Check the returned name,
   address, and distance against the map before recording a match. Use the
   returned `fuel_price_id` for the matching outlet. [TomTom Search response](https://docs.tomtom.com/search-api/documentation/search-service/fuzzy-search)

```powershell
$env:TOMTOM_API_KEY = Read-Host "TomTom API key" -MaskInput
$env:PRICE_PERMISSION_REFERENCE = Read-Host "Approved agreement reference"
(Invoke-RestMethod "http://127.0.0.1:8000/stations").stations |
    Select-Object id, name
uv run --frozen python -m scripts.refresh_tomtom_prices --find "node/123"
```

Save the verified mapping as `data/osrm/tomtom-station-map.json`, under the
ignored data directory. This example uses placeholder IDs.

```json
{
  "node/123": "TOMTOM_FUEL_PRICE_ID_FOR_THIS_STATION",
  "way/456": "TOMTOM_FUEL_PRICE_ID_FOR_ANOTHER_STATION"
}
```

Run a refresh to write `data/osrm/tomtom-prices.json`. For recurring refreshes,
run the command with `--watch-seconds 900` in its own terminal if that interval
fits your agreement. Each refresh makes one Fuel Prices request per mapped
station. A failed request other than a missing station preserves the previous
snapshot. [TomTom Fuel Prices endpoint](https://docs.tomtom.com/fuel-prices-api/documentation/fuel-prices-api/fuel-price)

```powershell
uv run --frozen python -m scripts.refresh_tomtom_prices
```

Add these values to your ignored Compose `.env` and rebuild the app.

```dotenv
PRICE_SOURCE=TomTom
PRICE_PERMISSION_REFERENCE=your-approved-agreement-reference
PRICE_SNAPSHOT_FILE=/data/tomtom-prices.json
```

```powershell
docker compose up -d --build --wait --wait-timeout 120
```

The worker can read new snapshots without restarting. In local Python, set
`PRICE_SNAPSHOT_FILE=data/osrm/tomtom-prices.json`. In Kubernetes, set the three
worker variables through `fuel-stop-provider-secrets` and provide the file on the
routing-data volume mounted at `/data`. The worker only needs the snapshot.

TomTom's `updatedAt` is used as the quote time. Prices must be in EUR/litre.
Petrol 95 uses the freshest of standard `sp95` or `sp95_e10`. If both are
equally fresh, `sp95` takes precedence. For the first live check, map at least
two feasible stops for the same trip. Compare the shown total with the sum
of litres multiplied by the quote at each selected station. Partial matches
and expired quotes can leave the comparison incomplete.

## Compatible HTTP feed

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
