"""Requested trees preserve their content in the existing compact XML format."""

from copy import deepcopy
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import pytest

from test_behavior_tree import compact_requested_xml

from tms_if_input.compiler import compile_scenario, export_compilation


TESTS = Path(__file__).resolve().parent
SAMPLES = TESTS.parent / "json_samples"
GEOJSON = SAMPLES / "261004-kyoto.geojson"
XML = SAMPLES / "261004-kyoto.xml"
RAW_REPLACEMENTS = json.loads((TESTS / "fixtures/replacement_tasks.json").read_text())
REPLACEMENTS = {model: compact_requested_xml(xml) for model, xml in RAW_REPLACEMENTS.items()}


@pytest.mark.parametrize("model", ["d37pxi", "mst110cr_2", "mst2200vd"])
def test_compact_requested_xml_remains_an_exact_string_in_exported_task_json(tmp_path, model):
    compiled = compile_scenario(GEOJSON, XML)
    task = next(task for task in compiled.tasks if task["model_name"] == model)
    expected = REPLACEMENTS[model]
    assert isinstance(task["task_sequence"], str)
    assert task["task_sequence"] == expected

    destination = export_compilation(compiled, tmp_path / "review")
    stored_tasks = json.loads((destination / "tasks.json").read_text())
    stored = next(task for task in stored_tasks if task["model_name"] == model)
    assert isinstance(stored["task_sequence"], str)
    assert stored["task_sequence"] == expected
    assert (destination / stored["description"]).read_text() == expected + "\n"


@pytest.mark.parametrize("model", ["d37pxi", "mst110cr_2", "mst2200vd"])
def test_compact_format_preserves_raw_node_attributes_and_expression_spaces(model):
    compiled = compile_scenario(GEOJSON, XML)
    task = next(task for task in compiled.tasks if task["model_name"] == model)
    sequence = task["task_sequence"]
    assert sequence.startswith('<root main_tree_to_execute="BehaviorTree">')
    assert sequence.endswith("</root>")
    assert not any(character in sequence for character in "\r\n\t")
    assert "<?xml" not in sequence
    assert "<!--" not in sequence
    assert re.search(r">\s+<", sequence) is None
    original = ET.fromstring(RAW_REPLACEMENTS[model])
    actual = ET.fromstring(sequence)
    assert [(node.tag, node.attrib) for node in actual.iter()] == [
        (node.tag, node.attrib) for node in original.iter()]
    expressions = actual.findall("BehaviorTree//Action[@ID='ConditionalExpression']")
    assert expressions
    assert all(" == true" in action.attrib["conditional_expression"] for action in expressions)
    assert '<AlwaysSuccess />' in sequence
    assert '<input_port name="terminate_condition"/>' in sequence
    assert re.search(r'<Action ID="ExecuteSubtask"[^>]* />', sequence) is None


def test_requested_dump_preserves_empty_type_attribute_and_quoted_section_value():
    compiled = compile_scenario(GEOJSON, XML)
    for model in ("mst110cr_2", "mst2200vd"):
        task = next(task for task in compiled.tasks if task["model_name"] == model)
        root = ET.fromstring(task["task_sequence"])
        writers = root.findall("BehaviorTree//Action[@ID='MongoValueWriter']")
        assert all(writer.attrib["input_type"] == "" for writer in writers)
        current_section = next(writer for writer in writers
                               if writer.attrib["mongo_param_name"] == "CURRENT_SECTION")
        assert current_section.attrib["input_value"].startswith('"route_')
        assert current_section.attrib["input_value"].endswith('"')
        ports = root.findall("TreeNodesModel/Action[@ID='MongoValueWriter']/input_port")
        assert [port.attrib["name"] for port in ports] == [
            "input_value", "mongo_param_name", "mongo_record_name"]


def test_legacy_and_requested_dump_model_names_compile_to_the_same_documents(tmp_path):
    original_bytes = XML.read_bytes()
    root = ET.fromstring(original_bytes)
    root.find("./machines/machine[@type='mst2200vdr']").set("type", "mst2200vd")
    requested_path = tmp_path / "requested-model.xml"
    requested_path.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")
    legacy = compile_scenario(GEOJSON, XML)
    requested = compile_scenario(GEOJSON, requested_path)
    assert legacy == requested
    assert XML.read_bytes() == original_bytes

    models = [model for document in legacy.parameters + legacy.tasks
              for model in (document.get("model_name", [])
                            if isinstance(document.get("model_name"), list)
                            else [document.get("model_name")])]
    assert "mst2200vd" in models
    assert "mst2200vdr" not in models
    flags = next(document for document in legacy.parameters
                 if document["record_name"] == "initialize_flgs")
    assert flags["initialize_flg_mst2200vd"] is False
    assert "initialize_flg_mst2200vdr" not in flags
    for task in legacy.tasks:
        assert "mst2200vdr" not in task["task_sequence"]
    excavator = next(task for task in legacy.tasks if task["model_name"] == "zx200")
    readers = ET.fromstring(excavator["task_sequence"]).findall(
        "BehaviorTree//Action[@ID='MongoValueReader']")
    assert "initialize_flg_mst2200vd" in [action.attrib["mongo_param_name"] for action in readers]


def test_numbered_dump_input_keeps_its_own_initialization_flag(tmp_path):
    root = ET.parse(XML).getroot()
    root.find("./machines/machine[@type='mst110cr']").set("type", "mst110cr_2")
    scenario = tmp_path / "numbered-dump.xml"
    scenario.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")
    compiled = compile_scenario(GEOJSON, scenario)
    flags = next(document for document in compiled.parameters
                 if document["record_name"] == "initialize_flgs")
    dump = next(task for task in compiled.tasks if task["model_name"] == "mst110cr_2")
    actions = ET.fromstring(dump["task_sequence"]).findall(
        "BehaviorTree//Action[@mongo_record_name='initialize_flgs']")
    references = {action.attrib["mongo_param_name"] for action in actions}
    assert "initialize_flg_mst110cr_2" in references
    assert references <= flags.keys()


@pytest.mark.parametrize("source,target", [
    ("3343055156", "2318567684"),
    ("2318567684", "3343055156"),
])
def test_work_dependencies_keep_generated_completion_readers_and_writers(tmp_path, source, target):
    root = ET.parse(XML).getroot()
    ET.SubElement(root.find("./procedure/flow"), "edge", {"from": source, "to": target})
    scenario = tmp_path / "work-dependency.xml"
    scenario.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")
    compiled = compile_scenario(GEOJSON, scenario)
    dump = next(task for task in compiled.tasks if task["model_name"] == "mst110cr_2")
    assert dump["task_sequence"] != REPLACEMENTS["mst110cr_2"]
    actions = [action for task in compiled.tasks
               for action in ET.fromstring(task["task_sequence"]).findall("BehaviorTree//Action")
               if action.attrib.get("mongo_record_name") == "task_completion_flgs"]
    field = "completed_flg_" + source
    assert any(action.attrib["ID"] == "MongoValueWriter" and
               action.attrib["mongo_param_name"] == field for action in actions)
    assert any(action.attrib["ID"] == "MongoValueReader" and
               action.attrib["mongo_param_name"] == field for action in actions)


def test_repeated_work_areas_keep_generated_tasks_and_distinct_parameter_references(tmp_path):
    root = ET.parse(XML).getroot()
    tasks = root.find("./procedure/tasks")
    first = root.find("./procedure/tasks/task[@name='leveling']")
    second = deepcopy(first)
    second.set("id", "second-grade")
    tasks.append(second)
    first_haul = root.find("./procedure/tasks/task[@name='transport']")
    haul = deepcopy(first_haul)
    haul.set("id", "second-haul")
    haul.find("parameter[@name='leveling_task']").set("value", second.get("id"))
    tasks.append(haul)
    flow = root.find("./procedure/flow")
    end_id = root.find("./procedure/tasks/task[@name='end']").get("id")
    ET.SubElement(flow, "edge", {"from": first.get("id"), "to": second.get("id")})
    ET.SubElement(flow, "edge", {"from": second.get("id"), "to": end_id})
    ET.SubElement(flow, "edge", {"from": first.get("id"), "to": haul.get("id")})
    ET.SubElement(flow, "edge", {"from": first_haul.get("id"), "to": haul.get("id")})
    ET.SubElement(flow, "edge", {"from": haul.get("id"), "to": end_id})
    scenario = tmp_path / "two-work-areas.xml"
    scenario.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")

    compiled = compile_scenario(GEOJSON, scenario)
    bulldozer = next(task for task in compiled.tasks if task["model_name"] == "d37pxi")
    assert bulldozer["task_sequence"] != REPLACEMENTS["d37pxi"]
    subtasks = ET.fromstring(bulldozer["task_sequence"]).findall(
        "BehaviorTree//Action[@ID='ExecuteSubtask']")
    leveling = [action for action in subtasks if action.attrib["subtask_name"] == "leveling"]
    assert [action.attrib["subtask_parameters"] for action in leveling] == [
        "leveling_params_" + first.get("id"), "leveling_params_second-grade"]
    records = {document["record_name"] for document in compiled.parameters}
    assert all(action.attrib["subtask_parameters"] in records for action in leveling)
    for model in ("mst110cr_2", "mst2200vd"):
        dump = next(task for task in compiled.tasks if task["model_name"] == model)
        assert dump["task_sequence"] != REPLACEMENTS[model]
        work = ET.fromstring(dump["task_sequence"]).findall(
            "BehaviorTree//Action[@ID='ExecuteSubtask']")
        assert any(action.attrib["subtask_name"] == "transport" for action in work)


def test_repeated_transport_keeps_both_work_steps_for_the_same_dump(tmp_path):
    root = ET.parse(XML).getroot()
    first = root.find("./procedure/tasks/task[@id='3142704754']")
    second = deepcopy(first)
    second.set("id", "second-haul")
    root.find("./procedure/tasks").append(second)
    flow = root.find("./procedure/flow")
    end_id = root.find("./procedure/tasks/task[@name='end']").get("id")
    ET.SubElement(flow, "edge", {"from": first.get("id"), "to": second.get("id")})
    ET.SubElement(flow, "edge", {"from": second.get("id"), "to": end_id})
    scenario = tmp_path / "two-transport-steps.xml"
    scenario.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")

    compiled = compile_scenario(GEOJSON, scenario)
    dump = next(task for task in compiled.tasks if task["model_name"] == "mst2200vd")
    work = ET.fromstring(dump["task_sequence"]).findall(
        "BehaviorTree//Action[@ID='ExecuteSubtask']")
    transports = [action for action in work if action.attrib["subtask_name"] == "transport"]
    assert [action.attrib["subtask_parameters"] for action in transports] == [
        "transport_params_3142704754", "transport_params_second-haul"]
    records = {document["record_name"] for document in compiled.parameters}
    assert all(action.attrib["subtask_parameters"] in records for action in transports)
