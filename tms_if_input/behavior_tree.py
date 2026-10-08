"""Compile the scenario flow into one construction behavior tree per machine.

Completion flags are monotonic within one imported scenario.  Clearing a flag
inside an individual machine tree would let another machine miss the completion
and wait forever, so only the database import initializes these flags to false.
"""

from __future__ import annotations

from collections import defaultdict
import json
from typing import Any
import xml.etree.ElementTree as ET

from .model import Plan, Task, record_name


INITIALIZE_FLAGS = "initialize_flgs"
COMPLETION_FLAGS = "task_completion_flgs"
_ENCODED_FLAG = "__encoded_"

_LEAF_IDS = {
    "excavator": "LeafNodeExcavator",
    "crawler_dump": "LeafNodeCrawlerDump",
    "bulldozer": "LeafNodeBulldozer",
}

# These are the ports used by the new construction interface in the supplied
# specification.  The model declares every emitted custom node and port.
_PORTS: dict[str, tuple[str, ...]] = {
    "ExecuteSubtask": (
        "model_name", "subtask_name", "machine_record_name",
        "terminate_condition", "subtask_parameters",
    ),
    "LeafNodeExcavator": (
        "model_name", "primitive_name", "previous_target_record_name",
        "target_record_name",
    ),
    "LeafNodeCrawlerDump": (
        "model_name", "primitive_name", "read_direction", "record_name",
    ),
    "LeafNodeBulldozer": ("model_name", "primitive_name", "record_name"),
    "MongoValueReader": (
        "mongo_record_name", "mongo_param_name", "output_port",
    ),
    "MongoValueWriter": (
        "input_value", "mongo_record_name", "mongo_param_name",
    ),
    "ConditionalExpression": ("conditional_expression",),
    "SetLocalBlackboard": ("output_key", "value"),
    "KeepRunningUntilFlgup": ("key",),
}


def completion_flag(plan: Plan, task: Task) -> tuple[str, str]:
    """Return the MongoDB record and field marking completion of a task."""
    if task.name == "initialize":
        prefix = "initialize_flg_"
        raw = record_name(plan, task, prefix + plan.model(task))
        suffix = raw[len(prefix):]
        # Repeated initialization can also make otherwise normal names collide:
        # model zx200 + task 1 versus the separate instance model zx200_1.
        collisions = sum(
            other.name == "initialize"
            and record_name(plan, other, prefix + plan.model(other)) == raw
            for other in plan.tasks.values()
        )
        suffix = _safe_flag_suffix(suffix, (plan.model(task), task.id), collisions > 1)
        return (
            INITIALIZE_FLAGS,
            prefix + suffix,
        )
    return COMPLETION_FLAGS, "completed_flg_" + _safe_flag_suffix(task.id, task.id)


def _safe_flag_suffix(value: str, identity: Any, force: bool = False) -> str:
    """Keep ordinary names; encode MongoDB path/key characters injectively."""
    if not force and not any(character in value for character in ".$\0") and not value.startswith(_ENCODED_FLAG):
        return value
    # The prefix is reserved even for literal input names, preventing a literal
    # model/ID from impersonating the encoded representation of another input.
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8").hex()
    return _ENCODED_FLAG + encoded


def _predecessors(plan: Plan, task: Task) -> list[Task]:
    """Resolve execution dependencies, skipping structural start/end nodes."""
    result: dict[str, Task] = {}

    def visit(predecessor: Task) -> None:
        if predecessor.name in {"start", "end"}:
            for previous in plan.predecessors(predecessor.id):
                visit(previous)
        else:
            result[predecessor.id] = predecessor

    for predecessor in plan.predecessors(task.id):
        visit(predecessor)
    return [candidate for candidate in plan.ordered_tasks() if candidate.id in result]


def _required_completions(plan: Plan) -> set[str]:
    return {
        predecessor.id
        for task in plan.tasks.values()
        if task.name not in {"start", "end"}
        for predecessor in _predecessors(plan, task)
    }


def synchronization_documents(plan: Plan) -> list[dict[str, Any]]:
    """Initial boolean flag records needed by the generated machine trees."""
    records: dict[str, dict[str, Any]] = {}
    required = _required_completions(plan)
    for task in plan.ordered_tasks():
        if task.name in {"start", "end"}:
            continue
        if task.name != "initialize" and task.id not in required:
            continue
        record, field = completion_flag(plan, task)
        document = records.setdefault(record, {"type": "static", "record_name": record})
        document[field] = False
    return list(records.values())


def _action(parent: ET.Element, node_id: str, **ports: str) -> ET.Element:
    return ET.SubElement(parent, "Action", {"ID": node_id, **ports})


def _append_wait(
    sequence: ET.Element,
    plan: Plan,
    task: Task,
    machine_index: int,
    task_index: int,
) -> None:
    dependencies = _predecessors(plan, task)
    if not dependencies:
        return

    # KeepRunningUntilFlgup checks a typed boolean on the *local* blackboard,
    # not a MongoDB field.  Work actions belong after this decorator; placing
    # them inside its polling child could repeat successful work every tick.
    ready_key = f"dependencies_ready_{machine_index}_{task_index}"
    _action(sequence, "SetLocalBlackboard", output_key=ready_key, value="false")
    decorator = ET.SubElement(
        sequence, "Decorator", {"ID": "KeepRunningUntilFlgup", "key": ready_key},
    )
    polling = ET.SubElement(decorator, "Sequence")
    conditions: list[str] = []
    for dependency_index, predecessor in enumerate(dependencies):
        record, field = completion_flag(plan, predecessor)
        # Local expression identifiers must be valid independently of arbitrary
        # XML task IDs and model identifiers.
        output_key = f"dependency_{machine_index}_{task_index}_{dependency_index}"
        _action(
            polling,
            "MongoValueReader",
            mongo_record_name=record,
            mongo_param_name=field,
            output_port=output_key,
        )
        conditions.append(output_key + " == true")
    conditional = ET.SubElement(polling, "IfThenElse")
    _action(
        conditional, "ConditionalExpression",
        conditional_expression=" and ".join(conditions),
    )
    _action(conditional, "SetLocalBlackboard", output_key=ready_key, value="true")
    ET.SubElement(conditional, "AlwaysSuccess")


def _append_primitive(
    sequence: ET.Element,
    kind: str,
    model: str,
    primitive: str,
    record: str = "",
    previous_record: str = "",
) -> None:
    ports = {"model_name": model, "primitive_name": primitive}
    if kind == "excavator":
        ports.update(
            previous_target_record_name=previous_record,
            target_record_name=record,
        )
    elif kind == "crawler_dump":
        ports.update(read_direction="", record_name=record)
    else:
        ports["record_name"] = record
    _action(sequence, _LEAF_IDS[kind], **ports)


def _append_initialize(sequence: ET.Element, plan: Plan, task: Task) -> None:
    model = plan.model(task)
    kind = plan.machine_kind(task.machine)

    def name(base: str) -> str:
        return record_name(plan, task, base)

    if kind == "excavator":
        _append_subtask(
            sequence, model, "subtask_excavator_change_pose", name("initial_move_pose"),
        )
        _append_primitive(
            sequence, kind, model, "primitive_excavator_follow_waypoints",
            name("initial_position"),
        )
        if any(joint in task.parameters for joint in ("swing", "boom", "arm", "bucket")):
            _append_primitive(
                sequence, kind, model, "primitive_excavator_change_pose", name("initial_pose"),
            )
    elif kind == "crawler_dump":
        _append_primitive(
            sequence, kind, model, "primitive_crawlerdump_swing", name("initial_move_pose"),
        )
        if "vessel" in task.parameters:
            _append_primitive(
                sequence, kind, model, "primitive_crawlerdump_release_soil",
                name("initial_pose_vessel"),
            )
        _append_primitive(
            sequence, kind, model, "primitive_crawlerdump_follow_waypoints",
            name("initial_position"),
        )
        if "swing" in task.parameters:
            _append_primitive(
                sequence, kind, model, "primitive_crawlerdump_swing", name("initial_pose"),
            )
    else:
        _append_primitive(
            sequence, kind, model, "primitive_bulldozer_follow_waypoints",
            name("initial_position"),
        )


def _append_work(sequence: ET.Element, plan: Plan, task: Task) -> None:
    _append_subtask(
        sequence, plan.model(task), task.name,
        context_record_name(plan, task),
    )


def _append_subtask(
    sequence: ET.Element,
    model: str,
    subtask: str,
    record: str,
) -> None:
    _action(
        sequence, "ExecuteSubtask", model_name=model, subtask_name=subtask,
        subtask_parameters=record, machine_record_name="", terminate_condition="",
    )


def context_record_name(plan: Plan, task: Task) -> str:
    """Record binding for work that needs to distinguish multiple work areas."""
    if task.name == "excavation_loading":
        return record_name(plan, task, "excavation_loading_params")
    counts = {
        name: sum(other.name == name for other in plan.tasks.values())
        for name in ("excavation_loading", "leveling")
    }
    if task.name == "leveling" and counts["leveling"] > 1:
        return record_name(plan, task, "leveling_params")
    if task.name == "transport" and (
        counts["excavation_loading"] > 1
        or counts["leveling"] > 1
        or sum(other.name == "transport" and other.machine == task.machine
               for other in plan.tasks.values()) > 1
    ):
        return record_name(plan, task, "transport_params")
    return ""


def _append_completion(sequence: ET.Element, plan: Plan, task: Task) -> None:
    record, field = completion_flag(plan, task)
    _action(
        sequence,
        "MongoValueWriter",
        input_value="true",
        mongo_record_name=record,
        mongo_param_name=field,
    )


def _append_tree_nodes_model(root: ET.Element, kind: str) -> None:
    nodes = sorted({
        (element.tag, element.attrib["ID"])
        for element in root.iter()
        if element.tag in {"Action", "Decorator"}
    })
    # Preserve the declaration order in each requested machine XML.
    execute = ("Action", "ExecuteSubtask")
    if execute in nodes and kind != "excavator":
        nodes.remove(execute)
        position = next((index for index, (tag, _) in enumerate(nodes)
                         if tag == "Decorator"), len(nodes))
        if kind == "bulldozer" and ("Action", "SetLocalBlackboard") in nodes:
            position = nodes.index(("Action", "SetLocalBlackboard"))
        nodes.insert(position, execute)
    model = ET.SubElement(root, "TreeNodesModel")
    for tag, node_id in nodes:
        declaration = ET.SubElement(model, tag, {"ID": node_id})
        for port in _PORTS[node_id]:
            ET.SubElement(declaration, "input_port", {"name": port})


def _serialize_tree(root: ET.Element) -> str:
    """Keep the exact empty-tag formatting of the requested subtask XML."""
    text = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    for element in root.iter("Action"):
        if element.attrib["ID"] == "ExecuteSubtask":
            original = ET.tostring(element, encoding="unicode", short_empty_elements=True)
            text = text.replace(original, original.replace(" />", "/>"))
    return text


def build_task_documents(plan: Plan) -> list[dict[str, Any]]:
    """Compile actual machine assignments, task order, and synchronization."""
    by_machine: dict[str, list[Task]] = defaultdict(list)
    for task in plan.ordered_tasks():
        if task.name not in {"start", "end"}:
            by_machine[task.machine].append(task)

    required = _required_completions(plan)
    documents: list[dict[str, Any]] = []
    for machine_index, (alias, model) in enumerate(plan.machines.items(), start=1):
        tasks = by_machine.get(alias)
        if not tasks:
            continue
        root = ET.Element("root", {"main_tree_to_execute": "BehaviorTree"})
        tree = ET.SubElement(root, "BehaviorTree", {"ID": "BehaviorTree"})
        sequence = ET.SubElement(tree, "Sequence")
        for task_index, task in enumerate(tasks):
            _append_wait(sequence, plan, task, machine_index, task_index)
            if task.name == "initialize":
                _append_initialize(sequence, plan, task)
            else:
                _append_work(sequence, plan, task)
            if task.name == "initialize" or task.id in required:
                _append_completion(sequence, plan, task)
        _append_tree_nodes_model(root, plan.machine_kind(alias))
        documents.append({
            "task_id": machine_index,
            "type": "task",
            "model_name": model,
            "description": model + "_task.xml",
            "task_sequence": _serialize_tree(root),
        })
    return documents
