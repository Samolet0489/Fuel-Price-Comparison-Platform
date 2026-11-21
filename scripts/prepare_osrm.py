"""Download a fixed Austria road snapshot and prepare local OSRM with Docker.

Run from PowerShell: uv run --frozen python -m scripts.prepare_osrm
All downloaded/generated data stays under the ignored data/osrm directory.
"""
import argparse
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import httpx
from shapely.geometry import LineString, mapping, shape
from shapely.ops import polygonize_full, unary_union

from fuel_stop.boundary import AustriaBoundary
from fuel_stop.models import Point

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/osrm"
PBF_URL = "https://download.geofabrik.de/europe/austria-261101.osm.pbf"
BOUNDARY_URL = "https://api.openstreetmap.org/api/0.6/relation/16239/full"


def digest(path, algorithm="sha256"):
    with path.open("rb") as file:
        return hashlib.file_digest(file, algorithm).hexdigest()


def download(client, url, destination):
    if destination.exists():
        print(f"Reusing {destination.name}", flush=True)
        return
    partial = destination.with_suffix(destination.suffix + ".part")
    print(f"Downloading {url} -> {destination}", flush=True)
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with partial.open("wb") as output:
            written, reported = 0, 0
            for chunk in response.iter_bytes():
                output.write(chunk)
                written += len(chunk)
                if written - reported >= 64 * 1024 * 1024:
                    print(f"Downloaded {written // (1024 * 1024)} MiB", flush=True)
                    reported = written
    partial.replace(destination)


def country_feature(xml: bytes):
    """Assemble the actual OSM country relation; reject incomplete rings."""
    root = ET.fromstring(xml)
    if root.find("error") is not None:
        raise ValueError("OSM returned incomplete data")
    relation = root.find("relation[@id='16239']")
    if relation is None:
        raise ValueError("Austria relation 16239 missing")
    tags = {t.attrib["k"]: t.attrib["v"] for t in relation.findall("tag")}
    if tags.get("ISO3166-1") != "AT" or tags.get("admin_level") != "2":
        raise ValueError("Expected Austrian country relation")
    nodes = {n.attrib["id"]: (float(n.attrib["lon"]), float(n.attrib["lat"])) for n in root.findall("node")}
    ways = {w.attrib["id"]: [n.attrib["ref"] for n in w.findall("nd")] for w in root.findall("way")}
    rings = {"outer": [], "inner": []}
    for member in relation.findall("member"):
        role = member.attrib.get("role")
        if role not in rings:
            continue  # e.g. labels and subareas do not define the border
        if member.attrib["type"] != "way":
            raise ValueError("Nested boundary relation unsupported; refusing incomplete border")
        rings[role].append(LineString([nodes[n] for n in ways[member.attrib["ref"]]]))

    def polygons(lines):
        if not lines:
            return None
        polygons, cuts, dangles, invalid = polygonize_full(lines)
        if not all(g.is_empty for g in (cuts, dangles, invalid)) or polygons.is_empty:
            raise ValueError("Country boundary contains unclosed or invalid rings")
        return unary_union(polygons)

    outer, inner = polygons(rings["outer"]), polygons(rings["inner"])
    if outer is None:
        raise ValueError("Country has no outer boundary")
    geometry = outer if inner is None else outer.difference(inner)
    AustriaBoundary(geometry)  # Validate without repairing or expanding geometry.
    return {"type": "Feature", "properties": {"osm_relation_id": 16239,
            "relation_version": relation.attrib.get("version"), "source": BOUNDARY_URL,
            "attribution": "© OpenStreetMap contributors, ODbL"}, "geometry": mapping(geometry)}


def prepare_boundary(client):
    xml = DATA / "austria-boundary.osm"
    download(client, BOUNDARY_URL, xml)
    feature = country_feature(xml.read_bytes())
    boundary = AustriaBoundary(shape(feature["geometry"]))
    for point in (Point(lon=16.3738, lat=48.2082), Point(lon=15.4395, lat=47.0707), Point(lon=11.4041, lat=47.2692)):
        if not boundary.covers_point(point):
            raise ValueError("Downloaded country boundary failed Austrian city sanity checks")
    (DATA / "austria-boundary.geojson").write_text(json.dumps(feature), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--boundary-only", action="store_true")
    group.add_argument("--download-only", action="store_true")
    group.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    if not args.prepare_only:
        with httpx.Client(timeout=httpx.Timeout(120, connect=15), follow_redirects=True,
                          headers={"User-Agent": "FuelStopPlanner/0.1 (one-time local data preparation)"}) as client:
            prepare_boundary(client)
            if args.boundary_only:
                print("Local Austrian boundary ready", flush=True)
                return
            pbf = DATA / "austria.osm.pbf"
            checksum = DATA / "austria.osm.pbf.md5"
            download(client, PBF_URL + ".md5", checksum)
            download(client, PBF_URL, pbf)
            expected = checksum.read_text().split()[0].lower()
            if len(expected) != 32 or digest(pbf, "md5") != expected:
                raise ValueError("Road download checksum mismatch; do not prepare corrupted data")
        manifest = {"pbf_url": PBF_URL, "pbf_sha256": digest(pbf), "boundary_url": BOUNDARY_URL,
                    "boundary_xml_sha256": digest(DATA / "austria-boundary.osm"),
                    "boundary_geojson_sha256": digest(DATA / "austria-boundary.geojson"),
                    "recorded_at": datetime.now(timezone.utc).isoformat()}
        (DATA / "sources.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if args.download_only:
        return
    manifest = json.loads((DATA / "sources.json").read_text(encoding="utf-8"))
    if (digest(DATA / "austria.osm.pbf") != manifest["pbf_sha256"] or
        digest(DATA / "austria-boundary.geojson") != manifest["boundary_geojson_sha256"]):
        raise ValueError("Data changed since download; refusing mismatched inputs")
    from scripts.prepare_stations import prepare
    prepare()
    config = subprocess.run(["docker", "compose", "config", "--format", "json"], cwd=ROOT,
                            check=True, capture_output=True, text=True)
    image = json.loads(config.stdout)["services"]["osrm"]["image"]
    running = subprocess.run(["docker", "compose", "ps", "--status", "running", "--services"],
                             cwd=ROOT, check=True, capture_output=True, text=True)
    if "osrm" in running.stdout.splitlines():
        raise RuntimeError("Stop the stack with docker compose down before rebuilding graph files")
    for command in (
        ["osrm-extract", "--threads", "2", "-p", "/opt/car.lua", "/data/austria.osm.pbf"],
        ["osrm-partition", "--threads", "2", "/data/austria.osrm"],
        ["osrm-customize", "--threads", "2", "/data/austria.osrm"],
    ):
        subprocess.run(["docker", "compose", "--profile", "prepare", "run", "--rm", "--no-deps",
                        "osrm-prepare", *command], cwd=ROOT, check=True)
    (DATA / "prepared.json").write_text(json.dumps({**manifest, "osrm_image": image,
        "algorithm": "mld", "profile": "/opt/car.lua", "threads": 2,
        "prepared_at": datetime.now(timezone.utc).isoformat()}, indent=2), encoding="utf-8")
    print("Austria MLD routing files prepared. Start with: docker compose up --build -d", flush=True)


if __name__ == "__main__":
    main()
