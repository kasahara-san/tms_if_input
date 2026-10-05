"""Grouped transport imports preserve legacy routes and runtime state."""

from copy import deepcopy
import json
from pathlib import Path

from test_database import ClientFactory, FakeDatabase

from tms_if_input.compiler import Compilation, compile_scenario
from tms_if_input.database import write_database


SAMPLES = Path(__file__).resolve().parents[1] / "json_samples"
GEOJSON = SAMPLES / "261004-kyoto.geojson"
XML = SAMPLES / "261004-kyoto.xml"
LINK_FIELDS = (
    "related_point_up_main", "related_point_up_sub",
    "related_point_down_main", "related_point_down_sub",
)
# IDs already persisted by the previous, one-document-per-edge importer.
# Build these documents directly from input geometry, independently of the
# grouped importer and without depending on a generated verification snapshot.
LEGACY_MAIN_IDS = (
    "2206133296", "3260195895", "3453947199", "3775605330",
    "2109419383", "944236149", "1285700315", "3483617579",
    "1901058875", "840315598", "3159144693", "936963196",
)
LEGACY_SUB_IDS = (
    "768363352", "3851915284", "1769298385",
    "2192002421", "1547089919", "2758184951",
)


def legacy_documents(source):
    lines = {str(feature["properties"]["id"]): feature for feature in source["features"]
             if feature["geometry"]["type"] == "LineString"}
    documents = []
    for label, identifiers in (("main", LEGACY_MAIN_IDS), ("sub", LEGACY_SUB_IDS)):
        for identifier in identifiers:
            coordinates = lines[identifier]["geometry"]["coordinates"]
            count = len(coordinates)
            documents.append({
                "_id": "legacy-" + identifier,
                "model_name": ["mst110cr", "mst2200vdr"], "type": "static",
                "record_name": identifier, "section_id": identifier,
                "label": label, "preferred_direction": "up",
                "x": [point[0] for point in coordinates],
                "y": [point[1] for point in coordinates],
                "z": [0.0] * count, "qx": [0.0] * count,
                "qy": [0.0] * count, "qz": [0.0] * count, "qw": [1.0] * count,
                **dict.fromkeys(LINK_FIELDS, ""),
                "_tms_if_input": {"import_key": "previous-scenario", "generation": "old"},
            })
    assert len(documents) == 18
    documents.extend([
        {"_id": "runtime-initialize", "record_name": "initialize_flgs",
         "initialize_flg_mst110cr": True, "initialize_flg_mst2200vdr": True},
        {"_id": "runtime-completion", "record_name": "task_completion_flgs",
         "completed_flg_3343055156": True, "completed_flg_3142704754": True},
    ])
    return documents


def grouped_routes(geojson=GEOJSON):
    compiled = compile_scenario(geojson, XML)
    routes = [document for document in compiled.parameters if "section_id" in document]
    assert len(routes) == 7
    assert sum(document["label"] == "main" for document in routes) == 5
    assert sum(document["label"] == "sub" for document in routes) == 2
    return Compilation(routes, [])


def assert_group_links_are_scoped(compilation):
    identifiers = {document["section_id"] for document in compilation.parameters}
    assert len(identifiers) == 7
    for document in compilation.parameters:
        assert document["record_name"] == document["section_id"]
        assert document["section_id"].startswith("route_")
        for key in LINK_FIELDS:
            assert document[key] == "" or document[key] in identifiers


def test_grouped_sample_adds_seven_routes_without_modifying_legacy_data():
    geojson_before, xml_before = GEOJSON.read_bytes(), XML.read_bytes()
    legacy = legacy_documents(json.loads(geojson_before))
    database = FakeDatabase(parameters=legacy)
    factory = ClientFactory(database)
    compilation = grouped_routes()
    original = deepcopy(compilation)
    assert_group_links_are_scoped(compilation)
    assert {document["section_id"] for document in compilation.parameters}.isdisjoint(
        LEGACY_MAIN_IDS + LEGACY_SUB_IDS)

    first = write_database(compilation, client_factory=factory)
    assert (first["inserted_parameters"], first["skipped_parameters"]) == (7, 0)
    assert first["inserted_tasks"] == 0
    assert database["parameter"].documents == legacy + original.parameters
    assert database["task"].documents == []
    stored = deepcopy(database["parameter"].documents)

    repeated = write_database(compilation, client_factory=factory)
    assert (repeated["inserted_parameters"], repeated["skipped_parameters"]) == (0, 7)
    assert database["parameter"].documents == stored
    assert len(database["parameter"].insert_calls) == 7
    assert compilation == original
    assert all(client.closed for client in factory.clients)
    assert GEOJSON.read_bytes() == geojson_before
    assert XML.read_bytes() == xml_before


def test_same_source_ids_with_changed_geometry_append_a_distinct_route_generation(tmp_path):
    geojson_before, xml_before = GEOJSON.read_bytes(), XML.read_bytes()
    source = json.loads(geojson_before)
    legacy = legacy_documents(source)
    database = FakeDatabase(parameters=legacy)
    factory = ClientFactory(database)
    first_compilation = grouped_routes()
    first_original = deepcopy(first_compilation)
    write_database(first_compilation, client_factory=factory)
    first_stored = deepcopy(database["parameter"].documents)

    changed_source = deepcopy(source)
    feature = next(feature for feature in changed_source["features"]
                   if feature["geometry"]["type"] == "LineString"
                   and str(feature["properties"]["id"]) == "3851915284")
    first, second = feature["geometry"]["coordinates"][:2]
    midpoint = [(left + right) / 2 for left, right in zip(first, second)]
    feature["geometry"]["coordinates"].insert(1, midpoint)
    # Only a vertex changes; every feature/node/LineString ID and endpoint
    # property remains exactly the same as the original supplied scenario.
    assert [item["properties"] for item in changed_source["features"]] == [
        item["properties"] for item in source["features"]]
    changed_geojson = tmp_path / "same-identifiers-new-waypoint.geojson"
    changed_geojson.write_text(json.dumps(changed_source), encoding="utf-8")
    changed_bytes = changed_geojson.read_bytes()
    second_compilation = grouped_routes(changed_geojson)
    second_original = deepcopy(second_compilation)
    first_ids = {document["section_id"] for document in first_compilation.parameters}
    second_ids = {document["section_id"] for document in second_compilation.parameters}
    assert first_ids.isdisjoint(second_ids)
    assert_group_links_are_scoped(second_compilation)
    assert sum(len(document["x"]) for document in second_compilation.parameters) == (
        sum(len(document["x"]) for document in first_compilation.parameters) + 1)

    appended = write_database(second_compilation, client_factory=factory)
    assert (appended["inserted_parameters"], appended["skipped_parameters"]) == (7, 0)
    assert database["parameter"].documents == first_stored + second_original.parameters
    stored = deepcopy(database["parameter"].documents)
    repeated = write_database(second_compilation, client_factory=factory)
    assert (repeated["inserted_parameters"], repeated["skipped_parameters"]) == (0, 7)
    assert database["parameter"].documents == stored
    assert database["parameter"].documents[:len(legacy)] == legacy
    assert len(database["parameter"].insert_calls) == 14
    assert first_compilation == first_original
    assert second_compilation == second_original
    assert database["task"].documents == []
    assert all(client.closed for client in factory.clients)
    assert changed_geojson.read_bytes() == changed_bytes
    assert GEOJSON.read_bytes() == geojson_before
    assert XML.read_bytes() == xml_before
