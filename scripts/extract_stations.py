"""Extract real public 24/7 fuel stations from the existing Austria PBF.

Container entry point; use scripts.prepare_stations from Windows.
No map download or OSRM rebuild is needed. Generated catalogues stay under data/.
"""
import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import osmium
from shapely.geometry import LineString, Point as GeoPoint, Polygon
from shapely.ops import polygonize_full, unary_union

from fuel_stop.boundary import AustriaBoundary
from fuel_stop.models import Point
from fuel_stop.stations import SOURCE, from_tags
from scripts.prepare_osrm import digest

ROOT = Path(__file__).resolve().parents[1]


class Collect(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.nodes, self.ways, self.relations = {}, {}, {}
        self.fuel_objects = 0

    def selected(self, obj):
        if obj.tags.get("amenity") != "fuel":
            return None
        self.fuel_objects += 1
        tags = dict(obj.tags)
        # Geometry is computed only for objects that can qualify.
        if from_tags("probe", tags, Point(lon=0, lat=0)) is None:
            return None
        return tags

    def node(self, node):
        tags = self.selected(node)
        if tags is not None:
            self.nodes[node.id] = (tags, (node.location.lon, node.location.lat))

    def way(self, way):
        tags = self.selected(way)
        if tags is not None:
            self.ways[way.id] = (tags, [n.ref for n in way.nodes])

    def relation(self, relation):
        tags = self.selected(relation)
        if tags is not None:
            self.relations[relation.id] = (tags, [(m.type, m.ref, m.role) for m in relation.members
                                                if m.role in ("outer", "inner")])


class MemberWays(osmium.SimpleHandler):
    def __init__(self, needed):
        super().__init__()
        self.needed, self.ways = needed, {}

    def way(self, way):
        if way.id in self.needed:
            self.ways[way.id] = [n.ref for n in way.nodes]


class Locations(osmium.SimpleHandler):
    def __init__(self, needed):
        super().__init__()
        self.needed, self.points = needed, {}

    def node(self, node):
        if node.id in self.needed:
            self.points[node.id] = (node.location.lon, node.location.lat)


def extract(pbf: Path, boundary_path: Path, output: Path):
    boundary = AustriaBoundary.load(str(boundary_path))
    with osmium.io.Reader(str(pbf)) as reader:
        timestamp = reader.header().get("osmosis_replication_timestamp")
    if not timestamp:
        raise ValueError("Source map has no timestamp; refusing to invent a fresh observation time")
    print("Reading OSM station tags...", flush=True)
    collected = Collect()
    collected.apply_file(str(pbf))
    needed_ways = {ref for _, members in collected.relations.values() for kind, ref, _ in members if kind == "w"}
    member_ways = MemberWays(needed_ways)
    if needed_ways:
        print("Reading station relation members...", flush=True)
        member_ways.apply_file(str(pbf))
    node_ids = {n for _, refs in collected.ways.values() for n in refs}
    node_ids.update(n for refs in member_ways.ways.values() for n in refs)
    print(f"Resolving {len(node_ids)} station geometry nodes...", flush=True)
    locations = Locations(node_ids)
    locations.apply_file(str(pbf))
    stations, counts = {}, Counter()

    def add(osm_id, tags, geometry):
        if geometry.is_empty or not geometry.is_valid:
            raise ValueError("Invalid station geometry")
        p = geometry.representative_point()
        point = Point(lon=p.x, lat=p.y)
        if not boundary.covers_point(point):
            counts["outside_austria"] += 1
            return
        station = from_tags(osm_id, tags, point)
        stations[osm_id] = station.model_dump(mode="json")

    for ident, (tags, coordinates) in collected.nodes.items():
        add(f"node/{ident}", tags, GeoPoint(coordinates))
    for ident, (tags, refs) in collected.ways.items():
        try:
            coordinates = [locations.points[n] for n in refs]
            geometry = Polygon(coordinates) if len(refs) >= 4 and refs[0] == refs[-1] else LineString(coordinates)
            add(f"way/{ident}", tags, geometry)
        except (KeyError, ValueError):
            counts["invalid_or_incomplete_geometry"] += 1
    for ident, (tags, members) in collected.relations.items():
        try:
            if not members or any(kind != "w" for kind, _, _ in members):
                raise ValueError("Unsupported relation geometry")
            rings = {}
            for role in ("outer", "inner"):
                lines = [LineString([locations.points[n] for n in member_ways.ways[ref]])
                         for _, ref, member_role in members if member_role == role]
                if not lines:
                    rings[role] = None
                    continue
                polygons, cuts, dangles, invalid = polygonize_full(lines)
                if not all(g.is_empty for g in (cuts, dangles, invalid)) or polygons.is_empty:
                    raise ValueError("Incomplete relation")
                rings[role] = unary_union(polygons)
            if rings["outer"] is None:
                raise ValueError("Missing outer geometry")
            geometry = rings["outer"] if rings["inner"] is None else rings["outer"].difference(rings["inner"])
            add(f"relation/{ident}", tags, geometry)
        except (KeyError, ValueError):
            counts["invalid_or_incomplete_geometry"] += 1
    payload = {"schema_version": 1, "source": SOURCE, "map_timestamp": timestamp,
               "generated_at": datetime.now(timezone.utc).isoformat(), "pbf_sha256": digest(pbf),
               "boundary_sha256": digest(boundary_path), "attribution": "© OpenStreetMap contributors, ODbL",
               "counts": {"fuel_objects": collected.fuel_objects, "eligible_stations": len(stations), **counts},
               "stations": [stations[k] for k in sorted(stations)]}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".part")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    print(json.dumps({"map_timestamp": timestamp, **payload["counts"]}, indent=2), flush=True)
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", type=Path, default=ROOT / "data/osrm/austria.osm.pbf")
    parser.add_argument("--boundary", type=Path, default=ROOT / "data/osrm/austria-boundary.geojson")
    parser.add_argument("--output", type=Path, default=ROOT / "data/osrm/stations.json")
    args = parser.parse_args()
    extract(args.pbf, args.boundary, args.output)


if __name__ == "__main__":
    main()
