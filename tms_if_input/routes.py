"""Extract transport sections using the node roles and order declared in XML."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .model import GeoGraph, Plan, Section, Task, identifier


@dataclass(frozen=True)
class _DirectedSection:
    section: Section
    start: str
    end: str
    label: str


def _nodes(task: Task, name: str, *, allow_empty=False) -> list[str]:
    values = task.parameters.get(name)
    if not isinstance(values, list) or (not values and not allow_empty):
        raise ValueError(f"Task {task.id}: {name} must be an {'array' if allow_empty else 'nonempty array'}")
    result = [identifier(value) for value in values]
    if len(result) != len(set(result)):
        raise ValueError(f"Task {task.id}: {name} contains duplicate node IDs")
    return result


def _related_task(plan: Plan, task: Task, name: str, expected: str) -> Task:
    related = plan.tasks.get(identifier(task.parameters.get(name)))
    if related is None or related.name != expected:
        raise ValueError(f"Task {task.id}: {name} must reference a {expected} task")
    return related


def _task_sections(plan: Plan, graph: GeoGraph, task: Task) -> tuple[list[_DirectedSection], set[str]]:
    main_nodes = _nodes(task, "main_node")
    sub_nodes = _nodes(task, "sub_node", allow_empty=True)
    main, sub = set(main_nodes), set(sub_nodes)
    if main & sub:
        raise ValueError(f"Task {task.id}: main_node and sub_node must be disjoint")
    excavation = _related_task(plan, task, "excavation_loading_task", "excavation_loading")
    leveling = _related_task(plan, task, "leveling_task", "leveling")
    loading = set(_nodes(excavation, "dump_node"))
    connections = set(_nodes(leveling, "connection_node"))
    allowed = main | sub | loading | connections
    for node in allowed:
        graph.point(node)
    main_order = {node: index for index, node in enumerate(main_nodes)}
    for node in loading:
        main_order.setdefault(node, -1)
    for node in connections:
        main_order.setdefault(node, len(main_nodes))
    sub_order = {node: index for index, node in enumerate(sub_nodes)}

    # Work-area roads between two boundary nodes are not transport sections.
    # Retain the first GeoJSON ID when opposite directions describe one road.
    roads = {}
    for section in graph.sections:
        endpoints = {section.start, section.end}
        if not endpoints <= allowed or not endpoints & (main | sub):
            continue
        key = frozenset(endpoints)
        if key in roads:
            previous = roads[key]
            if (previous.start, previous.end) != (section.end, section.start):
                raise ValueError(f"Task {task.id}: parallel sections share endpoints {sorted(endpoints)}")
            continue
        roads[key] = section

    directed = []
    internal = []
    attachments = defaultdict(list)
    for section in roads.values():
        start, end = section.start, section.end
        if start in sub and end in sub:
            if sub_order[start] > sub_order[end]:
                start, end = end, start
            internal.append(_DirectedSection(section, start, end, "sub"))
        elif start in sub or end in sub:
            node, anchor = (start, end) if start in sub else (end, start)
            attachments[node].append((anchor, section))
        else:
            if main_order[start] == main_order[end]:
                raise ValueError(f"Task {task.id}: main section {section.id} has no XML direction")
            if main_order[start] > main_order[end]:
                start, end = end, start
            directed.append(_DirectedSection(section, start, end, "main"))
    directed.sort(key=lambda section: (main_order[section.start], main_order[section.end]))

    # Validate the declared main order directly, without discovering a root or
    # calculating graph depths to infer the loading-to-leveling direction.
    reached = set(loading)
    for section in directed:
        if section.start not in reached:
            raise ValueError(f"Task {task.id}: main section {section.section.id} is disconnected from its loading area")
        reached.add(section.end)
    missing = (main | connections) - reached
    if missing:
        raise ValueError(f"Task {task.id}: main roads do not reach declared nodes {sorted(missing)}")

    incoming, outgoing = defaultdict(list), defaultdict(list)
    for section in internal:
        outgoing[section.start].append(section)
        incoming[section.end].append(section)
    origins = {}
    for node in sub_nodes:
        before, after, anchors = incoming[node], outgoing[node], attachments[node]
        if len(before) > 1 or len(after) > 1 or len(before) + len(after) + len(anchors) != 2:
            raise ValueError(f"Task {task.id}: passing node {node} must form an unbranched path between main-road nodes")
        if before:
            origins[node] = origins[before[0].start]
        else:
            anchor, section = min(anchors, key=lambda item: main_order[item[0]])
            origins[node] = main_order[anchor]
            directed.append(_DirectedSection(section, anchor, node, "sub"))
            anchors = [(point, road) for point, road in anchors if road.id != section.id]
        if after:
            if anchors:
                raise ValueError(f"Task {task.id}: passing node {node} has an intermediate main-road attachment")
            directed.append(after[0])
        else:
            if len(anchors) != 1:
                raise ValueError(f"Task {task.id}: passing node {node} has no ending main-road attachment")
            anchor, section = anchors[0]
            if main_order[anchor] <= origins[node]:
                raise ValueError(f"Task {task.id}: sub_node order conflicts with main_node order")
            directed.append(_DirectedSection(section, node, anchor, "sub"))
    return directed, connections


def transport_section_ids(plan: Plan, graph: GeoGraph, task: Task) -> list[str]:
    """Return only the sections explicitly selected for this transport task."""
    directed, _ = _task_sections(plan, graph, task)
    return [section.section.id for section in directed]


def leveling_entry_node(plan: Plan, graph: GeoGraph, task: Task) -> str:
    """Select an XML connection node ending a road of this leveling area's haul."""
    transports = [candidate for candidate in plan.tasks.values()
                  if candidate.name == "transport" and
                  identifier(candidate.parameters["leveling_task"]) == task.id]
    if not transports:
        raise ValueError(f"Task {task.id}: leveling entry requires a transport road")
    endpoints = {section.end for transport in transports
                 for section in _task_sections(plan, graph, transport)[0]}
    for node in _nodes(task, "connection_node"):
        if node in endpoints:
            return node
    raise ValueError(f"Task {task.id}: no connection_node is an up-direction transport or passing section endpoint")


def _neighbor(identifier: str, point: str, label: str, direction: str,
              adjacency: dict[str, list[_DirectedSection]], connections: set[str]) -> str:
    others = [section for section in adjacency[point]
              if section.section.id != identifier and section.label == label]
    if label == "main":
        others = [section for section in others
                  if (section.start == point if direction == "up" else section.end == point)]
    elif len(others) > 1:
        # Consecutive passing lanes can share one main-road anchor. Preserve
        # a single lane's endpoint link; distinguish incoming/outgoing lanes
        # when both exist at that anchor.
        directional = [section for section in others
                       if (section.start == point if direction == "up" else section.end == point)]
        if len(directional) == 1:
            others = directional
    if len(others) > 1:
        # Scalar link fields cannot list all terminal leveling-column branches;
        # each branch retains its link back to the shared preceding section.
        if direction == "up" and label == "main" and all(section.end in connections for section in others):
            return ""
        raise ValueError(f"Section {identifier}: multiple {direction} {label} links cannot fit the MongoDB schema")
    return others[0].section.id if others else ""


def build_route_documents(plan: Plan, graph: GeoGraph) -> list[dict]:
    """Extract every declared main/passing section, oriented loading -> leveling."""
    sections, connections = {}, set()
    for task in plan.tasks.values():
        if task.name != "transport":
            continue
        directed, ends = _task_sections(plan, graph, task)
        connections.update(ends)
        for section in directed:
            previous = sections.setdefault(section.section.id, section)
            if previous != section:
                raise ValueError(f"Transport tasks disagree on section {section.section.id} direction or label")
    adjacency = defaultdict(list)
    for section in sections.values():
        adjacency[section.start].append(section)
        adjacency[section.end].append(section)
    models = list(dict.fromkeys(model for alias, model in plan.machines.items()
                                if plan.machine_kind(alias) == "crawler_dump"))
    documents = []
    for section in sections.values():
        coordinates = section.section.coordinates
        if section.start != section.section.start:
            coordinates = tuple(reversed(coordinates))
        size = len(coordinates)
        document = {"model_name": models, "type": "static",
                    "x": [point[0] for point in coordinates],
                    "y": [point[1] for point in coordinates], "z": [0.0] * size,
                    "qx": [0.0] * size, "qy": [0.0] * size,
                    "qz": [0.0] * size, "qw": [1.0] * size,
                    "record_name": section.section.id, "section_id": section.section.id,
                    "label": section.label, "preferred_direction": "up"}
        for direction, point in (("up", section.end), ("down", section.start)):
            for label in ("main", "sub"):
                document[f"related_point_{direction}_{label}"] = _neighbor(
                    section.section.id, point, label, direction, adjacency, connections)
        documents.append(document)
    return documents
