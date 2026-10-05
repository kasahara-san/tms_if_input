"""Public output matches the logical sections and all links in waypoint slide 2."""

import json
from pathlib import Path

from tms_if_input.model import load_inputs
from tms_if_input.routes import build_route_documents


def test_public_sections_match_all_source_waypoints_and_spec_connections():
    root = Path(__file__).resolve().parents[1]
    geojson, xml = root / "json_samples/261004-kyoto.geojson", root / "json_samples/261004-kyoto.xml"
    plan, graph = load_inputs(geojson, xml)
    documents = build_route_documents(plan, graph)
    # Source chains and connections are independently transcribed from XML
    # roles and slide 2. The production grouping/hash code is not used here.
    definitions = {
        "sec1": ("3260195895", "main", ["7609442", "1755144311"]),
        "sec2": ("3453947199", "main", ["1755144311", "365138918"]),
        "sec3": ("3775605330", "main", ["365138918", "2943157174", "95297176", "3887281787", "1327926290"]),
        "sec4": ("3483617579", "main", ["1327926290", "2357889456"]),
        "sec5": ("1901058875", "main", ["2357889456", "758911660"]),
        "sec6": ("768363352", "sub", ["1755144311", "2657600551", "4025910820", "365138918"]),
        "sec7": ("2192002421", "sub", ["1327926290", "3813604511", "2995202696", "2357889456"]),
    }
    expected = {
        "sec1": ("sec2", "sec6", "", ""), "sec2": ("sec3", "", "sec1", ""),
        "sec3": ("sec4", "sec7", "sec2", "sec6"), "sec4": ("sec5", "", "sec3", ""),
        "sec5": ("", "", "sec4", "sec7"), "sec6": ("sec3", "", "sec1", ""), "sec7": ("sec5", "", "sec3", ""),
    }
    representatives = {value[0]: name for name, value in definitions.items()}
    named = {representatives[document["section_id"].split("_", 2)[2]]: document for document in documents}
    assert set(named) == set(definitions)
    names = {document["section_id"]: name for name, document in named.items()}
    actual = {name: tuple(names[document[f"related_point_{direction}_{label}"]]
                          if document[f"related_point_{direction}_{label}"] else ""
                          for direction in ("up", "down") for label in ("main", "sub")) for name, document in named.items()}
    assert actual == expected

    source = json.loads(geojson.read_text())
    roads = [feature for feature in source["features"] if feature["geometry"]["type"] == "LineString"]
    for name, (_, label, nodes) in definitions.items():
        coordinates = []
        for start, end in zip(nodes, nodes[1:]):
            road = next(road for road in roads if {str(road["properties"]["startid"]), str(road["properties"]["endid"])} == {start, end})
            edge = road["geometry"]["coordinates"]
            if str(road["properties"]["startid"]) != start:
                edge = list(reversed(edge))
            coordinates.extend(edge[1:] if coordinates and coordinates[-1] == edge[0] else edge)
        document = named[name]
        assert document["label"] == label
        assert document["x"] == [point[0] for point in coordinates]
        assert document["y"] == [point[1] for point in coordinates]
        assert all(len(document[field]) == len(coordinates) for field in ("z", "qx", "qy", "qz", "qw"))
