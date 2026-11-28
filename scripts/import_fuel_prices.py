"""Import station prices from a local CSV file without an API key."""
import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path

from fuel_stop.boundary import AustriaBoundary
from fuel_stop.models import Quote
from fuel_stop.planner import DependencyUnavailable
from fuel_stop.providers import LOCAL_PRICE_FILENAME, LOCAL_PRICE_SOURCE
from fuel_stop.stations import LocalStations
from scripts.price_snapshot import write_snapshot


DATA = Path(__file__).resolve().parents[1] / "data" / "osrm"
FIELDS = {"station_id", "fuel_type", "eur_per_l", "observed_at"}


def import_prices(csv_path, stations, output, source=LOCAL_PRICE_SOURCE):
    if csv_path.resolve() == output.resolve():
        raise ValueError("The input CSV and output snapshot must be different files")
    station_fuels = {station.id: station.fuel_types for station in stations}
    latest = {}
    seen = {}
    now = datetime.now(timezone.utc)
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        delimiter = ";" if ";" in stream.readline() else ","
        stream.seek(0)
        rows = csv.DictReader(stream, delimiter=delimiter, strict=True)
        headers = rows.fieldnames or []
        if len(headers) != len(FIELDS) or set(headers) != FIELDS:
            raise ValueError("Expected columns station_id, fuel_type, eur_per_l, observed_at")
        for row in rows:
            prefix = f"CSV line {rows.line_num}"
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{prefix}: expected four values")
            try:
                quote = Quote(station_id=row["station_id"].strip(), fuel_type=row["fuel_type"].strip().lower(),
                              eur_per_l=row["eur_per_l"].strip().replace(",", "."),
                              observed_at=datetime.fromisoformat(row["observed_at"].strip()))
            except ValueError:
                raise ValueError(f"{prefix}: enter diesel or petrol95, a valid price and a timestamp with timezone") from None
            if quote.station_id not in station_fuels:
                raise ValueError(f"{prefix}: station ID is not in the local catalogue")
            if quote.fuel_type not in station_fuels[quote.station_id]:
                raise ValueError(f"{prefix}: fuel type is not listed for this station")
            if quote.observed_at > now:
                raise ValueError(f"{prefix}: observation timestamp is in the future")
            key = (quote.station_id, quote.fuel_type)
            observation = (*key, quote.observed_at)
            if observation in seen and seen[observation] != quote.eur_per_l:
                raise ValueError(f"{prefix}: conflicting prices at the same station and timestamp")
            seen[observation] = quote.eur_per_l
            if key not in latest or quote.observed_at > latest[key].observed_at:
                latest[key] = quote
    if not latest:
        raise ValueError("The CSV does not contain any prices")
    write_snapshot(output, source, latest.values())
    return len(latest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("--output", type=Path, help="write a separate snapshot for an explicitly configured source")
    parser.add_argument("--source", default=LOCAL_PRICE_SOURCE, help="label for an approved export or a synthetic test")
    args = parser.parse_args()
    if not args.source.strip() or (args.source != LOCAL_PRICE_SOURCE and args.output is None):
        parser.error("A custom source label requires a separate --output file")
    try:
        country = AustriaBoundary.load(DATA / "austria-boundary.geojson")
        stations = LocalStations(DATA / "stations.json", country).load()
        output = args.output or DATA / LOCAL_PRICE_FILENAME
        count = import_prices(args.csv_file, stations, output, args.source)
    except (ValueError, OSError, csv.Error, DependencyUnavailable) as exc:
        parser.exit(1, f"Import failed: {exc}\n")
    print(f"Imported {count} station and fuel price records into {output}.")


if __name__ == "__main__":
    main()
