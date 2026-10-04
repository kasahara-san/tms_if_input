"""Compile construction-plan values into the specified MongoDB documents."""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Any

from .model import GeoGraph, Plan, Task, record_name
from .routes import build_route_documents, leveling_entry_node, transport_section_ids


def _number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"{context} must be finite")
    return float(value)


def _vector(value: Any, axes: str, context: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    for axis in axes:
        if axis not in value:
            raise ValueError(f"{context} is missing {axis}")
    for axis, component in value.items():
        _number(component, f"{context}.{axis}")
    return deepcopy(value)


def _values(task: Task, key: str) -> list:
    value = task.parameters.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"Task {task.id}: {key} must be a nonempty array")
    return value


def _required(task: Task, key: str) -> Any:
    if key not in task.parameters:
        raise ValueError(f"Task {task.id}: missing parameter {key}")
    return task.parameters[key]


def _rotation(vector: dict, direction: int = 1) -> dict:
    x, y = float(vector["x"]), float(vector["y"])
    if x == 0 and y == 0:
        raise ValueError("block_vector must have a nonzero XY direction")
    yaw = math.atan2(direction * y, direction * x)
    return {"qx": 0.0, "qy": 0.0, "qz": math.sin(yaw / 2), "qw": math.cos(yaw / 2)}


def _position(model: Any, name: str, point: dict, rotation: dict) -> dict:
    return {"type": "static", "model_name": model, "record_name": name,
            "x": point["x"], "y": point["y"], "z": 0.0, **rotation}


def _joint_pose(model: str, name: str, values: dict) -> dict:
    return {"planning_group": "manipulator", "model_name": model,
            "record_name": name,
            "waypoints": [{"type": "joint_values_relative", "data": values}],
            "time_scale": 1, "acceleration_scale": 1, "velocity_scale": 1}


def _dump_models(plan: Plan) -> list[str]:
    return list(dict.fromkeys(model for alias, model in plan.machines.items()
                              if plan.machine_kind(alias) == "crawler_dump"))


def _initial_documents(plan: Plan, graph: GeoGraph, task: Task) -> list[dict]:
    model = plan.model(task)
    rotation = _vector(_required(task, "rotation"), "xyzw", f"Task {task.id} rotation")
    point = graph.point(_required(task, "target_node")).xy()
    documents = [_position(model, record_name(plan, task, "initial_position"), point,
                           {"q" + key: rotation[key] for key in "xyzw"})]
    kind = plan.machine_kind(task.machine)
    if kind == "excavator":
        joints = {key + "_joint": _number(task.parameters[key], f"Task {task.id} {key}")
                  for key in ("boom", "swing", "arm", "bucket") if key in task.parameters}
        documents.append(_joint_pose(model, record_name(plan, task, "initial_pose"), joints))
        documents.append(_joint_pose(model, record_name(plan, task, "initial_move_pose"),
                                     {"swing_joint": 0.0}))
    elif kind == "crawler_dump":
        for base, parameter, document_type in (("initial_pose", "swing", "static"),
                                                ("initial_pose_vessel", "vessel", "dynamic"),
                                                ("initial_move_pose", None, "static")):
            angle = 0.0 if parameter is None else _number(_required(task, parameter),
                                                        f"Task {task.id} {parameter}")
            documents.append({"model_name": [model], "type": document_type,
                              "target_angle": angle, "record_name": record_name(plan, task, base)})
    return documents


def _excavation_documents(plan: Plan, graph: GeoGraph, task: Task,
                          dump_models: list[str]) -> list[dict]:
    block_size = _vector(_required(task, "block_size"), "xyz", f"Task {task.id} block_size")
    vector = _vector(_required(task, "block_vector"), "xyz", f"Task {task.id} block_vector")
    centers = [_vector(v, "xyz", f"Task {task.id} block_center") for v in _values(task, "block_center")]
    backhoe = [graph.point(node).xy() for node in _values(task, "backhoe_node")]
    dump = [graph.point(node).xy() for node in _values(task, "dump_node")]
    rotations = [_vector(value, "xyzw", f"Task {task.id} rotation")
                 for value in _values(task, "rotation")]
    if not len(centers) == len(backhoe) == len(dump) == len(rotations):
        raise ValueError(f"Task {task.id}: block_center, backhoe_node, dump_node and rotation lengths must match")
    documents = [{"model_name": plan.model(task), "type": "dynamic",
                  "task_type": "excavation_loading",
                  "record_name": record_name(plan, task, "excavation_loading_params"),
                  "block_size": block_size,
                  "block_angle": _number(_required(task, "block_angle"), f"Task {task.id} block_angle"),
                  "block_vector": vector, "block_center": centers,
                  "backhoe_node": backhoe, "dump_node": dump}]
    for index, (point, rotation) in enumerate(zip(dump, rotations), 1):
        documents.append(_position(dump_models, record_name(plan, task, f"loading_position_{index}"),
                                   point, {"q" + axis: rotation[axis] for axis in "xyzw"}))
    return documents


def _connection_for_dump(point: dict, connections: list[dict], vector: dict,
                         task_id: str) -> dict:
    # Project perpendicular to the travel direction. Euclidean nearest-neighbor
    # classification would switch columns as the distance into the area grows.
    norm = math.hypot(vector["x"], vector["y"])
    perpendicular = (-vector["y"] / norm, vector["x"] / norm)
    distances = [abs((point["x"] - entry["x"]) * perpendicular[0]
                     + (point["y"] - entry["y"]) * perpendicular[1])
                 for entry in connections]
    order = sorted(range(len(connections)), key=distances.__getitem__)
    if len(order) > 1 and math.isclose(distances[order[0]], distances[order[1]],
                                       rel_tol=1e-9, abs_tol=1e-6):
        raise ValueError(f"Task {task_id}: dump_node has an ambiguous connection column")
    return connections[order[0]]


def _leveling_documents(plan: Plan, graph: GeoGraph, task: Task,
                        dump_models: list[str]) -> list[dict]:
    vector = _vector(_required(task, "block_vector"), "xyz", f"Task {task.id} block_vector")
    dump_rotation = _rotation(vector, -1)
    bulldozer_rotation = _rotation(vector)
    connections = [graph.point(node).xy() for node in _values(task, "connection_node")]
    centers = [_vector(v, "xyz", f"Task {task.id} block_center") for v in _values(task, "block_center")]
    dump = [graph.point(node).xy() for node in _values(task, "dump_node")]
    bulldozer = [graph.point(node).xy() for node in _values(task, "bulldozer_node")]
    volumes = [_number(v, f"Task {task.id} soil_volume") for v in _values(task, "soil_volume")]
    if not len(centers) == len(dump) == len(bulldozer) == len(volumes):
        raise ValueError(f"Task {task.id}: leveling block and node array lengths must match")
    block_size = _vector(_required(task, "block_size"), "xy", f"Task {task.id} block_size")
    _number(_required(task, "leveling_height"), f"Task {task.id} leveling_height")
    entry = graph.point(leveling_entry_node(plan, graph, task)).xy()
    documents = [_position(dump_models, record_name(plan, task, "dumps_entry_point_leveling_area"),
                           entry, dump_rotation)]
    for index, point in enumerate(dump, 1):
        connection = _connection_for_dump(point, connections, vector, task.id)
        document = {"type": "static", "model_name": dump_models,
                    "record_name": record_name(plan, task, f"dump_node_{index}"),
                    "x": [connection["x"], point["x"]],
                    "y": [connection["y"], point["y"]], "z": [0.0, 0.0]}
        document.update({key: [value, value] for key, value in dump_rotation.items()})
        documents.append(document)
    for index, point in enumerate(bulldozer, 1):
        documents.append(_position(plan.model(task), record_name(plan, task, f"bulldozer_node_{index}"),
                                   point, bulldozer_rotation))
    for index, (center, volume) in enumerate(zip(centers, volumes), 1):
        documents.append({"type": "static", "record_name": record_name(plan, task, f"block_{index}"),
                          "block_size": deepcopy(block_size), "block_center": center,
                          "soil_volume": volume})
    return documents


def _context_documents(plan: Plan, graph: GeoGraph) -> list[dict]:
    from .behavior_tree import context_record_name

    documents = []
    for task in plan.ordered_tasks():
        if task.name not in {"leveling", "transport"}:
            continue
        name = context_record_name(plan, task)
        if not name:
            continue
        document = {"type": "dynamic", "model_name": plan.model(task),
                    "task_type": task.name, "record_name": name}
        if task.name == "leveling":
            count = len(_values(task, "block_center"))
            document.update({"leveling_height": task.parameters["leveling_height"],
                             "block_records": [record_name(plan, task, f"block_{index}")
                                               for index in range(1, count + 1)],
                             "bulldozer_records": [record_name(plan, task, f"bulldozer_node_{index}")
                                                   for index in range(1, count + 1)],
                             "dump_records": [record_name(plan, task, f"dump_node_{index}")
                                              for index in range(1, count + 1)],
                             "entry_record": record_name(plan, task, "dumps_entry_point_leveling_area")})
        else:
            excavation = plan.tasks[str(task.parameters["excavation_loading_task"])]
            leveling = plan.tasks[str(task.parameters["leveling_task"])]
            document.update({"excavation_loading_task": excavation.id,
                             "leveling_task": leveling.id,
                             "excavation_loading_model_name": plan.model(excavation),
                             "leveling_model_name": plan.model(leveling),
                             "excavation_loading_record": record_name(plan, excavation, "excavation_loading_params"),
                             "loading_position_records": [record_name(plan, excavation, f"loading_position_{index}")
                                                          for index in range(1, len(_values(excavation, "dump_node")) + 1)],
                             "dump_records": [record_name(plan, leveling, f"dump_node_{index}")
                                              for index in range(1, len(_values(leveling, "dump_node")) + 1)],
                             "entry_record": record_name(plan, leveling, "dumps_entry_point_leveling_area"),
                             "route_section_ids": transport_section_ids(plan, graph, task)})
        documents.append(document)
    return documents


def build_parameter_documents(plan: Plan, graph: GeoGraph) -> list[dict]:
    """Produce final parameter collection documents with no database side effects."""
    from .behavior_tree import synchronization_documents

    documents: list[dict] = []
    if graph.geofences:
        documents.append({"type": "static", "record_name": "geo_fence",
                          "coordinates": [[{"x": x, "y": y} for x, y in ring]
                                          for polygon in graph.geofences for ring in polygon]})
    dump_models = _dump_models(plan)
    for task in plan.ordered_tasks():
        if task.name == "initialize":
            documents.extend(_initial_documents(plan, graph, task))
        elif task.name == "excavation_loading":
            documents.extend(_excavation_documents(plan, graph, task, dump_models))
        elif task.name == "leveling":
            documents.extend(_leveling_documents(plan, graph, task, dump_models))
    route_documents = build_route_documents(plan, graph)
    documents.extend(route_documents)
    documents.extend(_context_documents(plan, graph))
    documents.extend(synchronization_documents(plan))
    return documents
