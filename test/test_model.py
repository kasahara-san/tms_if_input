"""Reject unusable input before any ROS or database work starts."""

import copy
import json
import xml.etree.ElementTree as ET

import pytest

from tms_if_input.model import (
    identifier, load_inputs, parse_geojson, parse_parameter, parse_plan,
)


def plan_xml():
    return """<ConstructionPlan>
      <machines><machine id="dig" type="zx120" /></machines>
      <procedure><tasks>
        <task id="start" name="start" />
        <task id="ready" name="initialize">
          <parameter name="machine" value="dig" />
          <parameter name="target_node" value="4294967300" />
          <parameter name="rotation" value="{x:0,y:0,z:0,w:1}" />
          <parameter name="swing" value="-2.5" />
          <parameter name="boom" value="10" />
          <parameter name="arm" value="20" />
          <parameter name="bucket" value="30" />
        </task>
        <task id="work" name="excavation_loading">
          <parameter name="machine" value="dig" />
          <parameter name="block_size" value="{x:2,y:3,z:4}" />
          <parameter name="block_angle" value="45" />
          <parameter name="block_center" value="[{x:1,y:2,z:3}]" />
          <parameter name="block_vector" value="{x:1,y:0,z:0}" />
          <parameter name="backhoe_node" value="[4294967300]" />
          <parameter name="dump_node" value='["dump-A"]' />
        </task>
        <task id="end" name="end" />
      </tasks><flow>
        <edge from="start" to="ready" />
        <edge from="ready" to="work" />
        <edge from="work" to="end" />
      </flow></procedure>
    </ConstructionPlan>"""


def graph_data():
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"id": 4294967300},
             "geometry": {"type": "Point", "coordinates": [0, 0, 99]}},
            {"type": "Feature", "properties": {"id": "dump-A"},
             "geometry": {"type": "Point", "coordinates": [2, 0]}},
            {"type": "Feature", "properties": {
                "id": "route-A", "startid": 4294967300, "endid": "dump-A"},
             "geometry": {"type": "LineString",
                          "coordinates": [[0, 0], [1, 0.5], [2, 0]]}},
            {"type": "Feature", "properties": {"name": "geo_fence"},
             "geometry": {"type": "Polygon", "coordinates": [
                 [[-10, -10], [10, -10], [10, 10], [-10, 10], [-10, -10]],
                 [[-1, -1], [-1, 1], [1, 1], [1, -1], [-1, -1]],
             ]}},
        ],
    }


def replace_parameter(root, task, key, value):
    root.find(f"./procedure/tasks/task[@id='{task}']/parameter[@name='{key}']").set(
        "value", value)


def test_vectors_and_exponents_parse_without_evaluation(tmp_path):
    assert parse_parameter("{x:-2.5e2, y: 0.5, z: 0}") == {
        "x": -250.0, "y": 0.5, "z": 0,
    }
    assert parse_parameter('[{x:1,y:2}, {x:3,y:4}]') == [
        {"x": 1, "y": 2}, {"x": 3, "y": 4},
    ]
    marker = tmp_path / "executed"
    expression = f"__import__('pathlib').Path({str(marker)!r}).touch()"
    assert parse_parameter(expression) == expression
    assert not marker.exists()


@pytest.mark.parametrize("value", [None, True, False, 1.2, "", "  ", [], {}])
def test_invalid_identifiers_are_rejected(value):
    with pytest.raises(ValueError):
        identifier(value)


def test_ids_are_opaque_and_large_integer_ids_are_not_truncated():
    plan = parse_plan(plan_xml())
    graph = parse_geojson(graph_data())
    assert identifier(4294967300) == "4294967300"
    assert identifier("000001") == "000001"
    assert plan.tasks["ready"].parameters["target_node"] == "4294967300"
    assert graph.point(4294967300).xy() == {"x": 0, "y": 0}
    assert graph.point("dump-A").xy() == {"x": 2, "y": 0}
    assert graph.sections[0].coordinates == ((0, 0), (1, 0.5), (2, 0))
    assert len(graph.geofences[0]) == 2


def test_namespaced_xml_and_reordered_tasks_preserve_flow_order():
    root = ET.fromstring(plan_xml())
    tasks = root.find("./procedure/tasks")
    tasks[:] = list(reversed(list(tasks)))
    for element in root.iter():
        element.tag = "{urn:construction}" + element.tag
    plan = parse_plan(ET.tostring(root, encoding="unicode"))
    assert [task.id for task in plan.ordered_tasks()] == ["start", "ready", "work", "end"]


@pytest.mark.parametrize("case", [
    "unknown-machine", "missing-rotation", "duplicate-parameter", "duplicate-task",
    "unknown-task-type", "unknown-flow-target", "cycle", "unreachable-task",
    "missing-start", "missing-end", "parallel-same-machine", "no-initialize",
    "missing-work-reference", "unequal-array-length", "empty-blocks",
    "zero-direction", "zero-quaternion", "nonfinite-coordinate", "negative-size",
])
def test_invalid_xml_is_rejected_with_context(case):
    root = ET.fromstring(plan_xml())
    tasks = root.find("./procedure/tasks")
    ready = tasks.find("task[@id='ready']")
    work = tasks.find("task[@id='work']")
    flow = root.find("./procedure/flow")
    if case == "unknown-machine":
        replace_parameter(root, "ready", "machine", "missing")
    elif case == "missing-rotation":
        ready.remove(ready.find("parameter[@name='rotation']"))
    elif case == "duplicate-parameter":
        ready.append(copy.deepcopy(ready.find("parameter[@name='rotation']")))
    elif case == "duplicate-task":
        tasks.append(copy.deepcopy(work))
    elif case == "unknown-task-type":
        work.set("name", "unknown_action")
    elif case == "unknown-flow-target":
        ET.SubElement(flow, "edge", {"from": "ready", "to": "missing"})
    elif case == "cycle":
        ET.SubElement(flow, "edge", {"from": "work", "to": "ready"})
    elif case == "unreachable-task":
        flow.remove(flow.find("edge[@from='start']"))
    elif case in {"missing-start", "missing-end"}:
        tasks.remove(tasks.find("task[@id='" + case[8:] + "']"))
    elif case == "parallel-same-machine":
        flow.remove(flow.find("edge[@from='ready']"))
        ET.SubElement(flow, "edge", {"from": "start", "to": "work"})
        ET.SubElement(flow, "edge", {"from": "ready", "to": "end"})
    elif case == "no-initialize":
        tasks.remove(ready)
        flow[:] = []
        ET.SubElement(flow, "edge", {"from": "start", "to": "work"})
        ET.SubElement(flow, "edge", {"from": "work", "to": "end"})
    elif case == "missing-work-reference":
        work.set("name", "transport")
        work[:] = []
        ET.SubElement(work, "parameter", {"name": "machine", "value": "dig"})
        for name in ("excavation_loading_task", "leveling_task"):
            ET.SubElement(work, "parameter", {"name": name, "value": "absent"})
        root.find("./machines/machine").set("type", "mst110cr")
        ET.SubElement(ready, "parameter", {"name": "vessel", "value": "0"})
    elif case == "unequal-array-length":
        replace_parameter(root, "work", "dump_node", '["dump-A", "dump-B"]')
    elif case == "empty-blocks":
        replace_parameter(root, "work", "block_center", "[]")
    elif case == "zero-direction":
        replace_parameter(root, "work", "block_vector", "{x:0,y:0,z:1}")
    elif case == "zero-quaternion":
        replace_parameter(root, "ready", "rotation", "{x:0,y:0,z:0,w:0}")
    elif case == "nonfinite-coordinate":
        replace_parameter(root, "work", "block_center", "[{x:1e999,y:2,z:3}]")
    elif case == "negative-size":
        replace_parameter(root, "work", "block_size", "{x:-2,y:3,z:4}")
    with pytest.raises(ValueError) as error:
        parse_plan(ET.tostring(root, encoding="unicode"))
    assert str(error.value)


def test_malformed_xml_is_rejected():
    with pytest.raises(ValueError, match="XML"):
        parse_plan("<ConstructionPlan><machines>")


def test_extra_quaternion_fields_cannot_hide_a_zero_rotation():
    root = ET.fromstring(plan_xml())
    replace_parameter(root, "ready", "rotation", "{x:0,y:0,z:0,w:0,extra:1}")
    with pytest.raises(ValueError, match="quaternion"):
        parse_plan(ET.tostring(root, encoding="unicode"))


@pytest.mark.parametrize("geometry_type", ["Point", "LineString", "MultiPolygon"])
def test_a_named_geofence_with_wrong_geometry_cannot_be_silently_ignored(geometry_type):
    data = graph_data()
    if geometry_type == "Point":
        data["features"][0]["properties"]["name"] = "geo_fence"
    elif geometry_type == "LineString":
        data["features"][2]["properties"]["name"] = "geo_fence"
    else:
        data["features"][3]["geometry"]["type"] = geometry_type
    with pytest.raises(ValueError, match="geo_fence"):
        parse_geojson(data)


@pytest.mark.parametrize("case", [
    "wrong-root", "nonarray-features", "duplicate-point", "unknown-endpoint",
    "incorrect-endpoint-coordinate", "nonfinite-point", "nonnumeric-point",
    "missing-point-coordinate", "invalid-rank", "invalid-metadata", "short-line",
    "unclosed-ring", "short-ring", "invalid-geofence-geometry",
])
def test_invalid_geojson_is_rejected_with_context(case):
    data = graph_data()
    features = data["features"]
    if case == "wrong-root":
        data["type"] = "GeometryCollection"
    elif case == "nonarray-features":
        data["features"] = {}
    elif case == "duplicate-point":
        features.append(copy.deepcopy(features[0]))
    elif case == "unknown-endpoint":
        features[2]["properties"]["endid"] = "missing"
    elif case == "incorrect-endpoint-coordinate":
        features[2]["geometry"]["coordinates"][0] = [123, 456]
    elif case == "nonfinite-point":
        features[0]["geometry"]["coordinates"][0] = float("nan")
    elif case == "nonnumeric-point":
        features[0]["geometry"]["coordinates"][0] = "0"
    elif case == "missing-point-coordinate":
        features[0]["geometry"]["coordinates"] = [0]
    elif case == "invalid-rank":
        features[0]["properties"]["metadata"] = {"route_rank": 2}
    elif case == "invalid-metadata":
        features[0]["properties"]["metadata"] = []
    elif case == "short-line":
        features[2]["geometry"]["coordinates"] = [[0, 0]]
    elif case == "unclosed-ring":
        features[3]["geometry"]["coordinates"][0][-1] = [0, 0]
    elif case == "short-ring":
        features[3]["geometry"]["coordinates"][0] = [[0, 0], [1, 1], [0, 0]]
    elif case == "invalid-geofence-geometry":
        features[3]["geometry"]["type"] = "MultiPolygon"
    with pytest.raises(ValueError) as error:
        parse_geojson(data)
    assert str(error.value)


def test_loading_files_checks_xml_node_references_and_reports_bad_json(tmp_path):
    geojson = tmp_path / "any-name.geojson"
    xml = tmp_path / "different-name.xml"
    geojson.write_text(json.dumps(graph_data()), encoding="utf-8-sig")
    xml.write_text(plan_xml(), encoding="utf-8-sig")
    plan, graph = load_inputs(geojson, xml)
    assert plan.tasks["work"].name == "excavation_loading"
    assert graph.point("dump-A").x == 2
    xml.write_text(plan_xml().replace("dump-A", "missing"), encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        load_inputs(geojson, xml)
    geojson.write_text('{"features": [}', encoding="utf-8")
    with pytest.raises(ValueError, match="GeoJSON"):
        load_inputs(geojson, xml)
