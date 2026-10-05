"""Extract transport sections using the node roles and order declared in XML."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json

from .model import GeoGraph, Plan, Section, Task, identifier


_Endpoint = str | tuple[str, str]


@dataclass(frozen=True)
class _DirectedSection:
    section: Section
    start: _Endpoint
    end: _Endpoint
    label: str
    source_ids: tuple[str, ...] = ()


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


def _group_task_sections(plan: Plan, graph: GeoGraph, task: Task) -> list[_DirectedSection]:
    """Join travel roads between junctions, excluding loading/dumping connectors."""
    directed, _ = _task_sections(plan, graph, task)
    main_nodes = _nodes(task, "main_node")
    sub_nodes = _nodes(task, "sub_node", allow_empty=True)
    main = set(main_nodes)
    allowed = main | set(sub_nodes)
    travel = [road for road in directed if {road.start, road.end} <= allowed]
    incoming, outgoing = defaultdict(list), defaultdict(list)
    for road in travel:
        incoming[road.end].append(road)
        outgoing[road.start].append(road)
    for node in allowed:
        for direction, neighbors in (("up", outgoing[node]), ("down", incoming[node])):
            for label in ("main", "sub"):
                if sum(road.label == label for road in neighbors) > 1:
                    raise ValueError(f"Task {task.id}: multiple {direction} {label} links at {node} cannot fit the MongoDB schema")
    # Each passing lane starts/ends on the main road. Those junctions split
    # the main road even when it has exactly one incoming and outgoing edge.
    junctions = {node for road in travel if road.label == "sub"
                 for node in (road.start, road.end) if node in main}
    junctions.update(node for node in allowed
                     if len(incoming[node]) != 1 or len(outgoing[node]) != 1)
    groups, visited = [], set()
    for first in travel:
        if first.start not in junctions or first.section.id in visited:
            continue
        path, coordinates = [], []
        road = first
        while True:
            if road.section.id in visited:
                raise ValueError(f"Task {task.id}: cyclic travel section {road.section.id}")
            visited.add(road.section.id)
            path.append(road.section.id)
            vertices = road.section.coordinates
            if road.start != road.section.start:
                vertices = tuple(reversed(vertices))
            # Keep all source vertices except the repeated joining coordinate.
            coordinates.extend(vertices[1:] if coordinates and coordinates[-1] == vertices[0]
                               else vertices)
            if road.end in junctions:
                break
            following = outgoing[road.end]
            if len(following) != 1 or following[0].label != first.label:
                raise ValueError(f"Task {task.id}: travel section changes role outside a junction")
            road = following[0]
        groups.append(_DirectedSection(
            Section(first.section.id, first.start, road.end, tuple(coordinates)),
            first.start, road.end, first.label, tuple(path)))
    if len(visited) != len(travel):
        raise ValueError(f"Task {task.id}: travel roads contain a cycle without a junction")

    # A route containing only one main waypoint has no internal LineString.
    # Loading/leveling connectors still validate it, but are not travel data.
    covered = {node for road in travel for node in (road.start, road.end)}
    singletons = set()
    for label, nodes in (("main", main_nodes), ("sub", sub_nodes)):
        for node in nodes:
            before_sub = any(road.label == "sub" for road in incoming[node])
            after_sub = any(road.label == "sub" for road in outgoing[node])
            before_main = any(road.label == "main" for road in incoming[node])
            after_main = any(road.label == "main" for road in outgoing[node])
            # A junction at an area's boundary, or two consecutive passing
            # lanes, still needs a main section to make the routing choice.
            main_junction = label == "main" and (
                (after_sub and not before_main) or (before_sub and not after_main)
                or (before_sub and after_sub))
            if node not in covered or main_junction:
                point = graph.point(node)
                groups.append(_DirectedSection(Section(node, node, node, ((point.x, point.y),)),
                                               node, node, label, (node,)))
                if label == "main":
                    singletons.add(node)
    order = {node: index for index, node in enumerate(main_nodes)}
    groups.sort(key=lambda road: (road.label != "main", order.get(road.start, -1),
                                  order.get(road.end, len(main_nodes)), road.section.id))

    # Separate the two sides of a one-point junction. A bypass must link to
    # the intervening main section before considering the next passing lane.
    groups = [_DirectedSection(
        road.section,
        (road.start, "down" if road.start == road.end and road.label == "main" else "up")
        if road.start in singletons else road.start,
        (road.end, "up" if road.start == road.end and road.label == "main" else "down")
        if road.end in singletons else road.end,
        road.label, road.source_ids) for road in groups]

    # Insert-only MongoDB imports must not mistake a new grouping or changed
    # geometry for an old record. Version the complete graph so every link
    # refers to the same import, including otherwise unchanged neighbors.
    contents = [(road.label, road.start, road.end, road.source_ids, road.section.coordinates)
                for road in groups]
    digest = hashlib.sha256(json.dumps(contents, ensure_ascii=False, separators=(',', ':'),
                                       allow_nan=False).encode('utf-8')).hexdigest()[:16]
    return [_DirectedSection(
        Section(f"route_{digest}_{road.section.id}", road.section.start, road.section.end,
                road.section.coordinates),
        road.start, road.end, road.label, road.source_ids) for road in groups]


def transport_section_ids(plan: Plan, graph: GeoGraph, task: Task) -> list[str]:
    """Return the grouped travel IDs used in this transport task's documents."""
    return [road.section.id for road in _group_task_sections(plan, graph, task)]


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


def _neighbor(identifier: str, point: _Endpoint, label: str, direction: str,
              adjacency: dict[_Endpoint, list[_DirectedSection]]) -> str:
    others = [section for section in adjacency[point]
              if section.section.id != identifier and section.label == label and
              (section.start == point if direction == "up" else section.end == point)]
    if len(others) > 1:
        raise ValueError(f"Section {identifier}: multiple {direction} {label} links cannot fit the MongoDB schema")
    return others[0].section.id if others else ""


def build_route_documents(plan: Plan, graph: GeoGraph) -> list[dict]:
    """Compile one waypoint array per travel section between junctions."""
    models = list(dict.fromkeys(model for alias, model in plan.machines.items()
                                if plan.machine_kind(alias) == "crawler_dump"))
    documents = {}
    for task in plan.tasks.values():
        if task.name != "transport":
            continue
        sections = _group_task_sections(plan, graph, task)
        adjacency = defaultdict(list)
        for section in sections:
            adjacency[section.start].append(section)
            adjacency[section.end].append(section)
        for section in sections:
            coordinates = section.section.coordinates
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
                        section.section.id, point, label, direction, adjacency)
            previous = documents.setdefault(section.section.id, document)
            if previous != document:
                raise ValueError(f"Transport tasks disagree on section {section.section.id}")
    return list(documents.values())
