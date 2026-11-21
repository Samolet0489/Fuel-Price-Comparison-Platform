import json

import pytest
from shapely.geometry import box, mapping


@pytest.fixture(autouse=True)
def local_test_boundary(tmp_path, monkeypatch):
    """Synthetic test rectangle, never copied to production data."""
    path = tmp_path / "test-boundary.geojson"
    path.write_text(json.dumps({"type": "Feature", "properties": {"osm_relation_id": 16239,
        "source": "SYNTHETIC UNIT TEST ONLY"}, "geometry": mapping(box(9, 46, 18, 50))}), encoding="utf-8")
    monkeypatch.setenv("AUSTRIA_BOUNDARY_FILE", str(path))
    monkeypatch.setenv("OSRM_BASE_URL", "http://osrm.test:5000")
    return path
