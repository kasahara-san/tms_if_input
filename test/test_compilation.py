"""Compile changing construction plans without relying on sample IDs or geometry."""

import copy
import json
import math
import re
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from tms_if_input.compiler import compile_scenario, export_compilation
from tms_if_input.model import load_inputs, parse_parameter, record_name


SAMPLES = Path(__file__).resolve().parents[1] / "json_samples"


def write_pair(tmp_path, geojson, root):
    geojson_path = tmp_path / "replaceable-input.geojson"
    xml_path = tmp_path / "arbitrary-scenario.xml"
    geojson_path.write_text(json.dumps(geojson), encoding="utf-8")
    xml_path.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")
    return geojson_path, xml_path


def record(compilation, name, model=None):
    matches = [document for document in compilation.parameters
               if document["record_name"] == name and (
                   model is None or document.get("model_name") == model
                   or (isinstance(document.get("model_name"), list)
                       and model in document["model_name"])
               )]
    assert len(matches) == 1, (name, model, matches)
    return matches[0]


def assert_quaternion(document, vector, direction=1, array=False):
    yaw = math.atan2(direction * vector["y"], direction * vector["x"])
    expected = {"qx": 0, "qy": 0, "qz": math.sin(yaw / 2), "qw": math.cos(yaw / 2)}
    for key, value in expected.items():
        assert document[key] == pytest.approx([value, value] if array else value)


def route_geometry(document):
    return document["label"], tuple(document["x"]), tuple(document["y"])


def assert_route_schema(routes):
    by_id = {document["section_id"]: document for document in routes}
    assert len(by_id) == len(routes)
    for document in routes:
        assert re.fullmatch(r"route_[0-9a-f]{16}_.+", document["section_id"])
        assert document["record_name"] == document["section_id"]
        assert document["preferred_direction"] == "up"
        size = len(document["x"])
        assert size >= 1
        for key in ("y", "z", "qx", "qy", "qz", "qw"):
            assert len(document[key]) == size
        for key in ("z", "qx", "qy", "qz"):
            assert document[key] == [0] * size
        assert document["qw"] == [1] * size
        for direction in ("up", "down"):
            for label in ("main", "sub"):
                linked = document[f"related_point_{direction}_{label}"]
                if linked:
                    assert by_id[linked]["label"] == label


def assert_parameter_schema(compilation, plan, graph, entry_nodes):
    """Derive all expected parameter values directly from each supplied input."""
    expected_fence = [[{"x": x, "y": y} for x, y in ring]
                      for polygon in graph.geofences for ring in polygon]
    assert record(compilation, "geo_fence")["coordinates"] == expected_fence
    dump_models = [model for alias, model in plan.machines.items()
                   if plan.machine_kind(alias) == "crawler_dump"]
    flags = record(compilation, "initialize_flgs")
    for task in plan.tasks.values():
        params = task.parameters
        if task.name == "initialize":
            model = plan.model(task)
            position = record(compilation, record_name(plan, task, "initial_position"), model)
            point = graph.point(params["target_node"])
            assert (position["x"], position["y"], position["z"]) == (point.x, point.y, 0)
            for axis in "xyzw":
                assert position["q" + axis] == params["rotation"][axis]
            assert flags[record_name(plan, task, "initialize_flg_" + model)] is False
            kind = plan.machine_kind(task.machine)
            if kind == "excavator":
                pose = record(compilation, record_name(plan, task, "initial_pose"), model)
                assert pose["planning_group"] == "manipulator"
                assert pose["waypoints"] == [{"type": "joint_values_absolute", "data": {
                    joint + "_joint": math.radians(params[joint])
                    for joint in ("boom", "swing", "arm", "bucket") if joint in params
                }}]
                for key in ("time_scale", "acceleration_scale", "velocity_scale"):
                    assert pose[key] == 1
                move = record(compilation, record_name(plan, task, "initial_move_pose"), model)
                assert move["waypoints"] == [{"type": "joint_values_absolute", "data": {
                    "boom_joint": -0.174533, "arm_joint": 2.61799,
                    "bucket_joint": 2.26893, "swing_joint": 0,
                }}]
            elif kind == "crawler_dump":
                for name, key, document_type in (
                    ("initial_pose", "swing", "static"),
                    ("initial_pose_vessel", "vessel", "dynamic"),
                    ("initial_move_pose", None, "static"),
                ):
                    pose = record(compilation, record_name(plan, task, name), model)
                    assert pose["model_name"] == [model]
                    assert pose["type"] == document_type
                    assert pose["target_angle"] == (math.radians(params[key]) if key else 0)
        elif task.name == "excavation_loading":
            area = record(compilation, record_name(plan, task, "excavation_loading_params"))
            assert area["model_name"] == plan.model(task)
            assert (area["type"], area["task_type"]) == ("dynamic", "excavation_loading")
            assert area["block_angle"] == math.radians(params["block_angle"])
            for key in ("block_size", "block_center", "block_vector"):
                assert area[key] == params[key]
            for key in ("backhoe_node", "dump_node"):
                assert area[key] == [graph.point(node).xy() for node in params[key]]
            for index, node in enumerate(params["dump_node"], 1):
                position = record(compilation, record_name(plan, task, f"loading_position_{index}"))
                assert position["model_name"] == dump_models
                assert (position["x"], position["y"]) == (graph.point(node).x, graph.point(node).y)
                assert position["z"] == 0
                for axis in "xyzw":
                    assert position["q" + axis] == params["rotation"][index - 1][axis]
        elif task.name == "leveling":
            connections = [graph.point(node) for node in params["connection_node"]]
            vector = params["block_vector"]
            entry = record(compilation, record_name(plan, task, "dumps_entry_point_leveling_area"))
            # Fixture-known directed route endpoints keep this expectation
            # independent of the production entry-selection implementation.
            expected = graph.point(entry_nodes[task.id])
            assert expected.id in {str(node) for node in params["connection_node"]}
            assert (entry["x"], entry["y"], entry["z"]) == (expected.x, expected.y, 0)
            assert entry["model_name"] == dump_models
            assert_quaternion(entry, vector, -1)
            for index, node in enumerate(params["dump_node"], 1):
                point = graph.point(node)
                distance = lambda connection: abs(
                    (point.x - connection.x) * -vector["y"]
                    + (point.y - connection.y) * vector["x"])
                connection = min(connections, key=distance)
                position = record(compilation, record_name(plan, task, f"dump_node_{index}"))
                assert position["model_name"] == dump_models
                assert position["x"] == [connection.x, point.x]
                assert position["y"] == [connection.y, point.y]
                assert position["z"] == [0, 0]
                assert_quaternion(position, vector, -1, array=True)
            for index, node in enumerate(params["bulldozer_node"], 1):
                position = record(compilation, record_name(plan, task, f"bulldozer_node_{index}"))
                assert position["model_name"] == plan.model(task)
                assert (position["x"], position["y"]) == (graph.point(node).x, graph.point(node).y)
                assert position["z"] == 0
                assert_quaternion(position, vector)
            for index, (center, volume) in enumerate(zip(params["block_center"], params["soil_volume"]), 1):
                block = record(compilation, record_name(plan, task, f"block_{index}"))
                assert block["block_size"] == params["block_size"]
                assert block["block_center"] == center
                assert block["soil_volume"] == volume


def synthetic_inputs(connection_count=1, excavation_count=1):
    points = {"loading-0": (0, 0), "backhoe-0": (-2, 0),
              "road-A": (10, 0), "road-B": (20, 0), "road-C": (30, 0),
              "pass-A": (10, 3), "pass-B": (20, 3)}
    for index in range(1, excavation_count):
        points[f"loading-{index}"] = (0, -index * 3)
        points[f"backhoe-{index}"] = (-2, -index * 3)
    for index in range(connection_count):
        x = 40 + index * 4
        points[f"connection-{index}"] = (x, 0)
        points[f"dump-{index}"] = (x + 0.1, -10)
        points[f"grader-{index}"] = (x + 0.1, -20)
    features = [{"type": "Feature", "properties": {"id": name},
        "geometry": {"type": "Point", "coordinates": [x, y, 99]}}
        for name, (x, y) in points.items()]
    routes = [("main-0", "loading-0", "road-A", [[5, 1]]),
              ("main-1", "road-A", "road-B", [[13, 0], [17, 0]]),
              ("main-2", "road-B", "road-C", []),
              ("sub-0", "road-A", "pass-A", []),
              ("sub-1", "pass-A", "pass-B", [[15, 4]]),
              ("sub-2", "pass-B", "road-B", [])]
    routes += [(f"terminal-{index}", "road-C", f"connection-{index}", [])
               for index in range(connection_count)]
    for index, (name, start, end, middle) in enumerate(routes):
        coordinates = [list(points[start][:2])] + middle + [list(points[end][:2])]
        # Deliberately store half the roads opposite to their required up direction.
        if index % 2:
            start, end, coordinates = end, start, list(reversed(coordinates))
        features.append({"type": "Feature", "properties": {
            "id": name, "startid": start, "endid": end},
            "geometry": {"type": "LineString", "coordinates": coordinates}})
        features.append({"type": "Feature", "properties": {
            "id": "reverse-" + name, "startid": end, "endid": start},
            "geometry": {"type": "LineString", "coordinates": list(reversed(coordinates))}})
    features.append({"type": "Feature", "properties": {"name": "geo_fence"},
                     "geometry": {"type": "Polygon", "coordinates": [
                         [[-5, -25], [60, -25], [60, 10], [-5, 10], [-5, -25]],
                         [[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]],
                     ]}})
    root = ET.Element("ConstructionPlan")
    machines = ET.SubElement(root, "machines")
    for alias, model in (("dig", "zx120"), ("haul", "mst110cr"), ("grade", "d37pxi")):
        ET.SubElement(machines, "machine", {"id": alias, "type": model})
    procedure = ET.SubElement(root, "procedure")
    tasks = ET.SubElement(procedure, "tasks")

    def task(name, identifier, parameters=None):
        element = ET.SubElement(tasks, "task", {"id": identifier, "name": name})
        for key, value in (parameters or {}).items():
            ET.SubElement(element, "parameter", {"name": key, "value": json.dumps(value)
                          if isinstance(value, (list, dict, int, float)) else value})

    task("start", "begin")
    for alias, target, joints in (("dig", "backhoe-0", {"boom": 12, "swing": -3, "arm": 4, "bucket": 5}),
                                  ("haul", "road-A", {"swing": 1.5, "vessel": 2}),
                                  ("grade", "grader-0", {})):
        task("initialize", "ready-" + alias, {"machine": alias, "target_node": target,
                                               "rotation": {"x": 0, "y": 0, "z": 0, "w": 1}, **joints})
    task("excavation_loading", "dig-work", {
        "machine": "dig", "block_size": {"x": 2, "y": 3, "z": 4}, "block_angle": 37,
        "block_center": [{"x": -1, "y": -index * 3, "z": 8} for index in range(excavation_count)],
        "block_vector": {"x": 0, "y": 1, "z": 0},
        "backhoe_node": [f"backhoe-{index}" for index in range(excavation_count)],
        "dump_node": [f"loading-{index}" for index in range(excavation_count)],
        "rotation": [{"x": index + 0.25, "y": -index - 0.5,
                      "z": index + 0.75, "w": index + 1}
                     for index in range(excavation_count)],
    })
    task("leveling", "grade-work", {
        "machine": "grade", "leveling_height": 7.25, "block_size": {"x": 2, "y": 4},
        "block_center": [{"x": 40 + index * 4, "y": -15, "z": 7.25} for index in range(connection_count)],
        "block_vector": {"x": 0, "y": 1, "z": 0},
        "soil_volume": [index + 1.25 for index in range(connection_count)],
        "dump_node": [f"dump-{index}" for index in range(connection_count)],
        "bulldozer_node": [f"grader-{index}" for index in range(connection_count)],
        "connection_node": [f"connection-{index}" for index in range(connection_count)],
    })
    task("transport", "haul-work", {"machine": "haul", "excavation_loading_task": "dig-work",
                                      "leveling_task": "grade-work",
                                      "main_node": ["road-A", "road-B", "road-C"],
                                      "sub_node": ["pass-A", "pass-B"]})
    task("end", "finish")
    flow = ET.SubElement(procedure, "flow")
    for alias in ("dig", "haul", "grade"):
        ET.SubElement(flow, "edge", {"from": "begin", "to": "ready-" + alias})
        for work in ("dig-work", "grade-work", "haul-work"):
            ET.SubElement(flow, "edge", {"from": "ready-" + alias, "to": work})
    for work in ("dig-work", "grade-work", "haul-work"):
        ET.SubElement(flow, "edge", {"from": work, "to": "finish"})
    return {"type": "FeatureCollection", "features": features}, root


@pytest.mark.parametrize("degrees,radians", [
    (0, 0), (90, math.pi / 2), (-180, -math.pi), (450, 2.5 * math.pi),
])
def test_all_scalar_angles_convert_once_without_changing_geometry_or_quaternions(
        tmp_path, degrees, radians):
    geojson, root = synthetic_inputs()
    baseline = compile_scenario(*write_pair(tmp_path, geojson, root))
    angular_parameters = {
        "ready-dig": ("boom", "swing", "arm", "bucket"),
        "ready-haul": ("swing", "vessel"),
        "dig-work": ("block_angle",),
    }
    for task_id, names in angular_parameters.items():
        task = root.find(f"./procedure/tasks/task[@id='{task_id}']")
        for name in names:
            task.find(f"parameter[@name='{name}']").set("value", str(degrees))
    paths = write_pair(tmp_path, geojson, root)
    result = compile_scenario(*paths)
    expected = copy.deepcopy(baseline)
    record(expected, "initial_pose", "zx120")["waypoints"][0]["data"] = {
        joint + "_joint": radians for joint in ("boom", "swing", "arm", "bucket")}
    for name in ("initial_pose", "initial_pose_vessel"):
        record(expected, name, "mst110cr")["target_angle"] = radians
    record(expected, "excavation_loading_params")["block_angle"] = radians
    # Comparing all documents also protects input quaternions, coordinates,
    # direction vectors, route IDs, and non-angle values from conversion.
    assert result.parameters == expected.parameters
    assert result.tasks == expected.tasks
    assert compile_scenario(*paths) == result
    plan, _ = load_inputs(*paths)
    for task_id, names in angular_parameters.items():
        assert {name: plan.tasks[task_id].parameters[name] for name in names} == {
            name: degrees for name in names}


def test_sample_compiles_from_input_values_and_contains_every_route():
    paths = SAMPLES / "261004-kyoto.geojson", SAMPLES / "261004-kyoto.xml"
    result = compile_scenario(*paths)
    plan, graph = load_inputs(*paths)
    assert_parameter_schema(result, plan, graph, {"2600159710": "919561426"})
    assert len(result.tasks) == len(plan.machines)
    routes = [document for document in result.parameters if "section_id" in document]
    assert len(result.parameters) == 54
    assert len(routes) == 7
    assert sum(document["label"] == "main" for document in routes) == 5
    assert sum(document["label"] == "sub" for document in routes) == 2
    assert_route_schema(routes)
    # These fixture-known paths stop at the XML-listed nodes. Loading and
    # leveling access roads must not appear in the stored navigation groups.
    expected_paths = [
        ("main", ["7609442", "1755144311"]),
        ("main", ["1755144311", "365138918"]),
        ("main", ["365138918", "2943157174", "95297176", "3887281787", "1327926290"]),
        ("main", ["1327926290", "2357889456"]),
        ("main", ["2357889456", "758911660"]),
        ("sub", ["1755144311", "2657600551", "4025910820", "365138918"]),
        ("sub", ["1327926290", "3813604511", "2995202696", "2357889456"]),
    ]
    expected_geometries = set()
    for label, path in expected_paths:
        vertices = []
        for start, end in zip(path, path[1:]):
            section = next(section for section in graph.sections
                           if {section.start, section.end} == {start, end})
            coordinates = list(section.coordinates)
            if section.start != start:
                coordinates.reverse()
            vertices.extend(coordinates[1:] if vertices and vertices[-1] == coordinates[0] else coordinates)
        expected_geometries.add((label, tuple(point[0] for point in vertices),
                                tuple(point[1] for point in vertices)))
    assert {route_geometry(document) for document in routes} == expected_geometries


@pytest.mark.parametrize("connections,blocks", [(1, 1), (2, 3), (4, 2), (12, 5), (15, 12)])
def test_variable_rings_blocks_connections_and_machine_count(tmp_path, connections, blocks):
    geojson, root = synthetic_inputs(connections, blocks)
    paths = write_pair(tmp_path, geojson, root)
    result = compile_scenario(*paths)
    plan, graph = load_inputs(*paths)
    assert_parameter_schema(result, plan, graph, {"grade-work": "connection-0"})
    assert len(result.tasks) == 3
    assert sum(document["record_name"].startswith("loading_position_")
               for document in result.parameters) == blocks
    assert sum(document["record_name"].startswith("block_")
               for document in result.parameters) == connections
    assert {document["record_name"] for document in result.parameters
            if document["record_name"].startswith("dump_node_")} == {
                f"dump_node_{index}" for index in range(1, connections + 1)}
    assert len(record(result, "geo_fence")["coordinates"]) == 2
    assert len([document for document in result.parameters if "section_id" in document]) == 4


def test_explicit_transport_lists_ignore_point_metadata_and_unlisted_roads(tmp_path):
    geojson, root = synthetic_inputs(connection_count=2)
    baseline = compile_scenario(*write_pair(tmp_path, geojson, root))
    for feature in geojson["features"]:
        if feature["geometry"]["type"] == "Point":
            feature["properties"]["metadata"] = {
                "route_rank": 0 if feature["properties"]["id"].startswith("pass-") else 1}
    geojson["features"].extend([
        {"type": "Feature", "properties": {"id": "unlisted", "metadata": ["unrelated"]},
         "geometry": {"type": "Point", "coordinates": [10, 6]}},
        {"type": "Feature", "properties": {
            "id": "unlisted-road", "startid": "road-A", "endid": "unlisted"},
         "geometry": {"type": "LineString", "coordinates": [[10, 0], [10, 6]]}},
        {"type": "Feature", "properties": {
            "id": "boundary-only", "startid": "loading-0", "endid": "connection-0"},
         "geometry": {"type": "LineString", "coordinates": [[0, 0], [40, 0]]}},
    ])
    result = compile_scenario(*write_pair(tmp_path, geojson, root))
    assert result.parameters == baseline.parameters
    assert result.tasks == baseline.tasks


@pytest.mark.parametrize("connection_order", [(2, 0, 1), (1, 2, 0)])
@pytest.mark.parametrize("reverse_roads", [False, True])
def test_entry_follows_first_matching_connection_without_changing_dump_paths(
        tmp_path, connection_order, reverse_roads):
    geojson, root = synthetic_inputs(connection_count=3)
    baseline = compile_scenario(*write_pair(tmp_path, geojson, root))
    connection_parameter = root.find(
        "./procedure/tasks/task[@id='grade-work']/parameter[@name='connection_node']")
    connection_parameter.set("value", json.dumps([
        f"connection-{index}" for index in connection_order]))
    if reverse_roads:
        # Remove opposite duplicates so raw end-point matching cannot use
        # the duplicate road to conceal a failure to orient the real road.
        geojson["features"] = [feature for feature in geojson["features"]
                               if not str(feature["properties"].get("id", "")).startswith("reverse-")]
        for feature in geojson["features"]:
            if feature["geometry"]["type"] != "LineString":
                continue
            properties = feature["properties"]
            properties["startid"], properties["endid"] = (
                properties["endid"], properties["startid"])
            feature["geometry"]["coordinates"].reverse()
    paths = write_pair(tmp_path, geojson, root)
    result = compile_scenario(*paths)
    plan, graph = load_inputs(*paths)
    assert_parameter_schema(result, plan, graph, {
        "grade-work": f"connection-{connection_order[0]}"})
    entry_name = "dumps_entry_point_leveling_area"
    assert [document for document in result.parameters if document["record_name"] != entry_name] == [
        document for document in baseline.parameters if document["record_name"] != entry_name]
    assert result.tasks == baseline.tasks


def test_routes_reverse_all_vertices_and_link_multiple_passing_nodes(tmp_path):
    geojson, root = synthetic_inputs()
    result = compile_scenario(*write_pair(tmp_path, geojson, root))
    documents = [document for document in result.parameters if "section_id" in document]
    assert len(documents) == 4
    assert_route_schema(documents)
    routes = {route_geometry(document): document for document in documents}
    names = {
        "entry": ("main", (10,), (0,)),
        "first": ("main", (10, 13, 17, 20), (0, 0, 0, 0)),
        "last": ("main", (20, 30), (0, 0)),
        "passing": ("sub", (10, 10, 15, 20, 20), (0, 3, 4, 3, 0)),
    }
    assert set(routes) == set(names.values())
    routes = {name: routes[geometry] for name, geometry in names.items()}
    assert routes["entry"]["section_id"].endswith("_road-A")
    assert routes["first"]["section_id"].endswith("_main-1")
    assert routes["last"]["section_id"].endswith("_main-2")
    assert routes["passing"]["section_id"].endswith("_sub-0")
    expected_links = {
        "entry": ("first", "passing", "", ""),
        "first": ("last", "", "entry", ""),
        "last": ("", "", "first", "passing"),
        "passing": ("last", "", "entry", ""),
    }
    for name, links in expected_links.items():
        document = routes[name]
        assert tuple(document[f"related_point_{direction}_{label}"]
                     for direction, label in (("up", "main"), ("up", "sub"), ("down", "main"), ("down", "sub"))) == tuple(
                         routes[link]["section_id"] if link else "" for link in links)


@pytest.mark.parametrize("join_offset", [0, 0.0001])
def test_joined_main_preserves_internal_vertices_and_only_drops_identical_join(tmp_path, join_offset):
    geojson, root = synthetic_inputs()
    root.find("./procedure/tasks/task[@id='haul-work']/parameter[@name='sub_node']").set("value", '[]')
    geojson["features"] = [feature for feature in geojson["features"]
                           if not feature["properties"].get("id", "").removeprefix("reverse-").startswith("sub-")]
    for feature in geojson["features"]:
        identifier = feature["properties"].get("id")
        coordinates = feature["geometry"]["coordinates"]
        if identifier == "main-1":
            coordinates.insert(-2, [13, 0])
        elif identifier == "reverse-main-1":
            coordinates.insert(1, [13, 0])
        elif identifier == "main-2":
            coordinates[0] = [20 + join_offset, 0]
        elif identifier == "reverse-main-2":
            coordinates[-1] = [20 + join_offset, 0]
    result = compile_scenario(*write_pair(tmp_path, geojson, root))
    routes = [document for document in result.parameters if "section_id" in document]
    assert len(routes) == 1
    assert_route_schema(routes)
    expected_x = [10, 13, 13, 17, 20] + ([20 + join_offset] if join_offset else []) + [30]
    assert routes[0]["x"] == expected_x
    assert routes[0]["y"] == [0] * len(expected_x)


def test_group_ids_are_stable_and_change_when_geometry_changes_without_new_source_ids(tmp_path):
    geojson, root = synthetic_inputs()
    paths = write_pair(tmp_path, geojson, root)
    baseline = compile_scenario(*paths)
    assert compile_scenario(*paths).parameters == baseline.parameters
    original_ids = {document["section_id"] for document in baseline.parameters if "section_id" in document}
    # Machine and task names do not describe the road or change its identity.
    for machine in root.findall("./machines/machine"):
        machine.set("type", "replacement_" + machine.get("id"))
    root.find("./procedure/tasks/task[@id='haul-work']").set("id", "renamed-haul-work")
    for edge in root.findall("./procedure/flow/edge"):
        for key in ("from", "to"):
            if edge.get(key) == "haul-work":
                edge.set(key, "renamed-haul-work")
    same_roads = compile_scenario(*write_pair(tmp_path, geojson, root))
    assert {document["section_id"] for document in same_roads.parameters if "section_id" in document} == original_ids
    for feature in geojson["features"]:
        if feature["properties"].get("id") == "main-1":
            feature["geometry"]["coordinates"][1] = [17, 2]
        elif feature["properties"].get("id") == "reverse-main-1":
            feature["geometry"]["coordinates"][-2] = [17, 2]
    changed = compile_scenario(*write_pair(tmp_path, geojson, root))
    changed_ids = {document["section_id"] for document in changed.parameters if "section_id" in document}
    assert len(changed_ids) == len(original_ids) == 4
    assert not original_ids & changed_ids


def test_single_explicit_main_node_is_one_waypoint_and_excludes_access_roads(tmp_path):
    geojson, root = synthetic_inputs()
    geojson["features"] = [feature for feature in geojson["features"]
                           if feature["geometry"]["type"] != "LineString"
                           or feature["properties"]["id"] in {"main-0", "reverse-main-0"}]
    geojson["features"].append({"type": "Feature", "properties": {
        "id": "access", "startid": "road-A", "endid": "connection-0"},
        "geometry": {"type": "LineString", "coordinates": [[10, 0], [25, 1], [40, 0]]}})
    transport = root.find("./procedure/tasks/task[@id='haul-work']")
    transport.find("parameter[@name='main_node']").set("value", '["road-A"]')
    transport.find("parameter[@name='sub_node']").set("value", '[]')
    paths = write_pair(tmp_path, geojson, root)
    result = compile_scenario(*paths)
    plan, graph = load_inputs(*paths)
    assert_parameter_schema(result, plan, graph, {"grade-work": "connection-0"})
    routes = [document for document in result.parameters if "section_id" in document]
    assert_route_schema(routes)
    assert len(routes) == 1
    assert route_geometry(routes[0]) == ("main", (10,), (0,))
    assert routes[0]["section_id"].endswith("_road-A")
    assert all(routes[0][f"related_point_{direction}_{label}"] == ""
               for direction in ("up", "down") for label in ("main", "sub"))


def transformed_sample():
    geojson = json.loads((SAMPLES / "261004-kyoto.geojson").read_text())
    root = ET.parse(SAMPLES / "261004-kyoto.xml").getroot()
    id_map = {str(feature["properties"]["id"]): f"site-point-or-road-{index}"
              for index, feature in enumerate(geojson["features"])
              if "id" in feature["properties"]}
    task_map = {task.get("id"): f"task:{index}:replacement" for index, task in enumerate(root.findall("./procedure/tasks/task"))}
    alias_map, model_map = {}, {}
    for index, machine in enumerate(root.findall("./machines/machine")):
        alias_map[machine.get("id")] = f"machine_alias_{index + 10}"
        model_map[machine.get("type")] = f"replacement_model_{index + 10}"
        machine.set("id", alias_map[machine.get("id")])
        machine.set("type", model_map[machine.get("type")])

    def transform_coordinates(raw):
        if raw and isinstance(raw[0], (int, float)):
            return [-raw[1] + 1000, raw[0] - 700] + raw[2:]
        return [transform_coordinates(value) for value in raw]

    for feature in geojson["features"]:
        props = feature["properties"]
        for key in ("id", "startid", "endid"):
            if key in props:
                props[key] = id_map[str(props[key])]
        feature["geometry"]["coordinates"] = transform_coordinates(feature["geometry"]["coordinates"])
    # Ring/vertex count changes are independent of machine and task counts.
    fence = next(feature for feature in geojson["features"] if feature["properties"].get("name") == "geo_fence")
    fence["geometry"]["coordinates"].append([[100, 100], [102, 100], [101, 102], [100, 100]])
    ring = fence["geometry"]["coordinates"][0]
    ring.insert(1, [(ring[0][axis] + ring[1][axis]) / 2 for axis in (0, 1)])
    for task in root.findall("./procedure/tasks/task"):
        task.set("id", task_map[task.get("id")])
        for parameter in task.findall("parameter"):
            key, raw = parameter.get("name"), parameter.get("value")
            if key == "machine":
                parameter.set("value", alias_map[raw])
            elif key == "target_node":
                parameter.set("value", id_map[raw])
            elif key in ("excavation_loading_task", "leveling_task"):
                parameter.set("value", task_map[raw])
            elif key.endswith("_node"):
                parameter.set("value", json.dumps([id_map[str(node)] for node in parse_parameter(raw)]))
            elif key == "block_center":
                centers = parse_parameter(raw)
                for center in centers:
                    center["x"], center["y"] = -center["y"] + 1000, center["x"] - 700
                parameter.set("value", json.dumps(centers))
            elif key == "block_vector":
                vector = parse_parameter(raw)
                vector["x"], vector["y"] = -vector["y"], vector["x"]
                parameter.set("value", json.dumps(vector))
            elif key == "rotation":
                rotations = parse_parameter(raw)
                rotations = rotations if isinstance(rotations, list) else [rotations]
                sine = math.sqrt(0.5)
                for quaternion in rotations:
                    quaternion["x"], quaternion["y"], quaternion["z"], quaternion["w"] = (
                        sine * (quaternion["x"] - quaternion["y"]),
                        sine * (quaternion["x"] + quaternion["y"]),
                        sine * (quaternion["w"] + quaternion["z"]),
                        sine * (quaternion["w"] - quaternion["z"]))
                parameter.set("value", json.dumps(rotations if task.get("name") == "excavation_loading"
                                                  else rotations[0]))
    for edge in root.findall("./procedure/flow/edge"):
        for key in ("from", "to"):
            edge.set(key, task_map[edge.get(key)])
    return geojson, root, id_map, model_map


def test_replacing_all_ids_models_coordinates_and_vectors_changes_output(tmp_path):
    geojson, root, id_map, model_map = transformed_sample()
    paths = write_pair(tmp_path, geojson, root)
    result = compile_scenario(*paths)
    plan, graph = load_inputs(*paths)
    leveling = next(task for task in plan.tasks.values() if task.name == "leveling")
    assert_parameter_schema(result, plan, graph, {leveling.id: id_map["919561426"]})
    assert {task["model_name"] for task in result.tasks} == set(model_map.values())
    baseline = compile_scenario(SAMPLES / "261004-kyoto.geojson", SAMPLES / "261004-kyoto.xml")
    routes = {route_geometry(document): document for document in result.parameters if "section_id" in document}
    previous_routes = [document for document in baseline.parameters if "section_id" in document]
    correspondence = {}
    for previous in previous_routes:
        changed = routes[(previous["label"], tuple(-y + 1000 for y in previous["y"]),
                          tuple(x - 700 for x in previous["x"]))]
        correspondence[previous["section_id"]] = changed["section_id"]
        assert changed["section_id"] != previous["section_id"]
        assert changed["x"] == pytest.approx([-y + 1000 for y in previous["y"]])
        assert changed["y"] == pytest.approx([x - 700 for x in previous["x"]])
        assert changed["label"] == previous["label"]
        assert changed["model_name"] == [model_map[model] for model in previous["model_name"]]
    assert len(correspondence) == len(routes) == len(previous_routes)
    changed_by_id = {document["section_id"]: document for document in routes.values()}
    for previous in previous_routes:
        changed = changed_by_id[correspondence[previous["section_id"]]]
        for direction in ("up", "down"):
            for label in ("main", "sub"):
                key = f"related_point_{direction}_{label}"
                assert changed[key] == correspondence.get(previous[key], "")
    assert len(record(result, "geo_fence")["coordinates"]) == 3
    for task in result.tasks:
        tree = ET.fromstring(task["task_sequence"])
        assert tree.find("BehaviorTree") is not None
        assert all(action.get("model_name") == task["model_name"]
                   for action in tree.findall("./BehaviorTree//Action") if action.get("model_name"))


@pytest.mark.parametrize("case", ["disconnected", "main-cycle", "branched-passing", "ambiguous-dump-column"])
def test_invalid_geometry_does_not_silently_drop_routes(tmp_path, case):
    geojson, root = synthetic_inputs(connection_count=2 if case == "ambiguous-dump-column" else 1)
    features = geojson["features"]
    if case == "disconnected":
        geojson["features"] = [feature for feature in features
                               if feature["properties"].get("id") not in {"main-1", "reverse-main-1"}]
    elif case == "main-cycle":
        features.append({"type": "Feature", "properties": {
            "id": "cycle", "startid": "road-A", "endid": "road-C"},
            "geometry": {"type": "LineString", "coordinates": [[10, 0], [30, 0]]}})
    elif case == "branched-passing":
        features.extend([
            {"type": "Feature", "properties": {"id": "dead-end"},
             "geometry": {"type": "Point", "coordinates": [12, 6]}},
            {"type": "Feature", "properties": {"id": "branch", "startid": "pass-A", "endid": "dead-end"},
             "geometry": {"type": "LineString", "coordinates": [[10, 3], [12, 6]]}},
        ])
        root.find("./procedure/tasks/task[@id='haul-work']/parameter[@name='sub_node']").set(
            "value", '["pass-A", "pass-B", "dead-end"]')
    else:
        next(feature for feature in features if feature["properties"].get("id") == "dump-0")["geometry"]["coordinates"] = [42, -10]
    with pytest.raises(ValueError):
        compile_scenario(*write_pair(tmp_path, geojson, root))


def test_export_is_parseable_and_preserves_compilation(tmp_path):
    geojson, root = synthetic_inputs()
    result = compile_scenario(*write_pair(tmp_path, geojson, root))
    destination = export_compilation(result, tmp_path / "review")
    assert json.loads((destination / "parameters.json").read_text()) == result.parameters
    assert json.loads((destination / "tasks.json").read_text()) == result.tasks
    for task in result.tasks:
        exported = destination / task["description"]
        assert exported.read_text().strip() == task["task_sequence"]
        assert ET.parse(exported).getroot().tag == "root"


def test_two_sequential_work_areas_keep_parameter_and_route_references_separate(tmp_path):
    geojson, root = synthetic_inputs()
    second_geojson, second_root = synthetic_inputs(connection_count=2, excavation_count=2)
    prefix = "area2:"
    for feature in second_geojson["features"]:
        props = feature["properties"]
        for key in ("id", "startid", "endid"):
            if key in props:
                props[key] = prefix + props[key]

        def translate(raw):
            if isinstance(raw[0], (int, float)):
                return [raw[0] + 100, raw[1] + 50] + raw[2:]
            return [translate(value) for value in raw]

        feature["geometry"]["coordinates"] = translate(feature["geometry"]["coordinates"])
    geojson["features"].extend(second_geojson["features"])
    tasks = root.find("./procedure/tasks")
    flow = root.find("./procedure/flow")
    for task in second_root.findall("./procedure/tasks/task"):
        if task.get("name") in {"start", "end"}:
            continue
        task.set("id", prefix + task.get("id"))
        for parameter in task.findall("parameter"):
            key, value = parameter.get("name"), parameter.get("value")
            if key in {"target_node", "excavation_loading_task", "leveling_task"}:
                parameter.set("value", prefix + value)
            elif key.endswith("_node"):
                parameter.set("value", json.dumps([prefix + node for node in parse_parameter(value)]))
            elif key == "block_center":
                centers = parse_parameter(value)
                for center in centers:
                    center["x"] += 100
                    center["y"] += 50
                parameter.set("value", json.dumps(centers))
        tasks.append(copy.deepcopy(task))
    for edge in list(flow):
        if edge.get("to") == "finish":
            flow.remove(edge)
    for work in ("dig-work", "grade-work", "haul-work"):
        for alias in ("dig", "haul", "grade"):
            ET.SubElement(flow, "edge", {"from": work, "to": prefix + "ready-" + alias})
    for edge in second_root.findall("./procedure/flow/edge"):
        if edge.get("from") == "begin":
            continue
        source = prefix + edge.get("from")
        target = "finish" if edge.get("to") == "finish" else prefix + edge.get("to")
        ET.SubElement(flow, "edge", {"from": source, "to": target})
    paths = write_pair(tmp_path, geojson, root)
    result = compile_scenario(*paths)
    plan, graph = load_inputs(*paths)
    assert_parameter_schema(result, plan, graph, {
        "grade-work": "connection-0", prefix + "grade-work": prefix + "connection-0"})
    assert len(result.tasks) == 3
    assert len(plan.tasks) == 14
    for identifier in ("haul-work", prefix + "haul-work"):
        context = record(result, "transport_params_" + identifier)
        task = plan.tasks[identifier]
        excavation = plan.tasks[task.parameters["excavation_loading_task"]]
        leveling = plan.tasks[task.parameters["leveling_task"]]
        assert context["excavation_loading_record"] == record_name(plan, excavation, "excavation_loading_params")
        assert context["entry_record"] == record_name(plan, leveling, "dumps_entry_point_leveling_area")
        assert len(context["route_section_ids"]) == 4
        assert all((record(result, section)["x"][0] >= 100) == identifier.startswith(prefix)
                   for section in context["route_section_ids"])
        for key in ("loading_position_records", "dump_records"):
            assert all(record(result, name) for name in context[key])
    first_ids = set(record(result, "transport_params_haul-work")["route_section_ids"])
    second_ids = set(record(result, "transport_params_" + prefix + "haul-work")["route_section_ids"])
    assert not first_ids & second_ids
    assert first_ids | second_ids == {document["section_id"] for document in result.parameters
                                    if "section_id" in document}
    completions = record(result, "task_completion_flgs")
    assert all(completions["completed_flg_" + identifier] is False
               for identifier in ("dig-work", "grade-work", "haul-work"))
