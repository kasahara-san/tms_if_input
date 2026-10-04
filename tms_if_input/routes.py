"""Derive transport sections and passing-lane connections from GeoJSON topology."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable

from .model import GeoGraph, Plan, Section, Task


@dataclass(frozen=True)
class _DirectedSection:
    section: Section
    start: str
    end: str
    label: str


def _ids(value) -> set[str]:
    if isinstance(value, (list, tuple)):
        result: set[str] = set()
        for item in value:
            result.update(_ids(item))
        return result
    return {str(value)}


def _adjacency(sections: Iterable[Section]) -> dict[str, list[Section]]:
    adjacency: dict[str, list[Section]] = defaultdict(list)
    for section in sections:
        adjacency[section.start].append(section)
        adjacency[section.end].append(section)
    return adjacency


def _other(section: Section, point: str) -> str:
    return section.end if section.start == point else section.start


def _selected_sections(plan: Plan, graph: GeoGraph) -> list[Section]:
    used: set[str] = set()
    for task in plan.tasks.values():
        if task.name in {"start", "end", "initialize", "transport"}:
            continue
        for name, value in task.parameters.items():
            if name == "target_node" or name.endswith("_node"):
                used.update(_ids(value))
    transport_points = set(graph.points) - used
    sections: list[Section] = []
    first_direction: dict[frozenset[str], tuple[str, str]] = {}
    for section in graph.sections:
        if section.start not in transport_points and section.end not in transport_points:
            continue
        endpoints = frozenset((section.start, section.end))
        if section.start == section.end:
            raise ValueError(f"Transport section {section.id} is a self loop")
        first = first_direction.get(endpoints)
        if first == (section.end, section.start):
            # Opposite endpoint order explicitly represents the same road.
            continue
        first_direction.setdefault(endpoints, (section.start, section.end))
        sections.append(section)
    return sections


def _anchors(plan: Plan) -> tuple[set[str], set[str]]:
    loading: set[str] = set()
    connections: set[str] = set()
    for task in plan.tasks.values():
        if task.name != "transport":
            continue
        for key, name, destination in (("excavation_loading_task", "excavation_loading", loading),
                                        ("leveling_task", "leveling", connections)):
            identifier = str(task.parameters.get(key, ""))
            related = plan.tasks.get(identifier)
            if related is None or related.name != name:
                raise ValueError(f"Task {task.id}: {key} must reference a {name} task")
            parameter = "dump_node" if name == "excavation_loading" else "connection_node"
            values = related.parameters.get(parameter)
            if not isinstance(values, list) or not values:
                raise ValueError(f"Task {related.id}: {parameter} must be a nonempty array")
            destination.update(_ids(values))
    return loading, connections


def _orient_main(sections: list[Section], loading: set[str], connections: set[str]) -> tuple[list[_DirectedSection], dict[str, int], dict[str, str]]:
    adjacency = _adjacency(sections)
    missing = connections - set(adjacency)
    if missing:
        raise ValueError(f"Leveling connection nodes are missing from transport main roads: {', '.join(sorted(missing))}")
    depths: dict[str, int] = {}
    roots_by_point: dict[str, str] = {}
    directed: list[_DirectedSection] = []
    remaining = set(adjacency)
    while remaining:
        seed = next(iter(remaining))
        component: set[str] = set()
        pending = [seed]
        while pending:
            point = pending.pop()
            if point in component:
                continue
            component.add(point)
            pending.extend(_other(section, point) for section in adjacency[point])
        remaining -= component
        roots = component & loading
        if len(roots) != 1:
            raise ValueError("Each transport main-road component must have exactly one connected loading node")
        if not component & connections:
            raise ValueError("Transport main-road component does not reach a leveling connection node")
        root = next(iter(roots))
        roots_by_point.update({point: root for point in component})
        if len(adjacency[root]) != 1:
            raise ValueError(f"Loading node {root} must be a terminal of the transport main road")
        depths[root] = 0
        visited_sections: set[str] = set()
        queue = deque([root])
        while queue:
            point = queue.popleft()
            for section in adjacency[point]:
                if section.id in visited_sections:
                    continue
                visited_sections.add(section.id)
                other = _other(section, point)
                if other in depths:
                    raise ValueError("Transport main roads contain an ambiguous cycle or parallel section")
                depths[other] = depths[point] + 1
                directed.append(_DirectedSection(section, point, other, "main"))
                queue.append(other)
        for point in component:
            if len(adjacency[point]) == 1 and point != root and point not in connections:
                raise ValueError(f"Transport main road terminates at unrelated node {point}")
    return directed, depths, roots_by_point


def _orient_sub(sections: list[Section], depths: dict[str, int],
                roots_by_point: dict[str, str]) -> list[_DirectedSection]:
    adjacency = _adjacency(sections)
    remaining = {section.id: section for section in sections}
    directed: list[_DirectedSection] = []
    while remaining:
        seed = next(iter(remaining.values()))
        component_sections: dict[str, Section] = {}
        component_points: set[str] = set()
        pending = [seed.start]
        while pending:
            point = pending.pop()
            if point in component_points:
                continue
            component_points.add(point)
            for section in adjacency[point]:
                component_sections[section.id] = section
                pending.append(_other(section, point))
        for identifier in component_sections:
            remaining.pop(identifier)
        attachments = component_points & set(depths)
        if len(attachments) != 2:
            raise ValueError("Each passing road must connect exactly two main-road nodes")
        if any(len(adjacency[point]) != (1 if point in attachments else 2)
               for point in component_points):
            raise ValueError("Passing road must be an unbranched path between main-road nodes")
        start, end = sorted(attachments, key=depths.__getitem__)
        if roots_by_point[start] != roots_by_point[end]:
            raise ValueError("Passing road must bypass sections within one connected main road")
        if depths[start] == depths[end]:
            raise ValueError("Passing-road direction is ambiguous between equally distant main-road nodes")
        point = start
        used: set[str] = set()
        while point != end:
            candidates = [section for section in adjacency[point] if section.id not in used]
            if len(candidates) != 1:
                raise ValueError("Passing road contains a cycle or an ambiguous connection")
            section = candidates[0]
            used.add(section.id)
            other = _other(section, point)
            directed.append(_DirectedSection(section, point, other, "sub"))
            point = other
        if len(used) != len(component_sections):
            raise ValueError("Passing road contains sections outside its connected path")
    return directed


def _neighbor(identifier: str, point: str, label: str, direction: str,
              adjacency: dict[str, list[_DirectedSection]],
              connections: set[str]) -> str:
    others = [section for section in adjacency[point]
              if section.section.id != identifier and section.label == label]
    if label == "main":
        others = [section for section in others
                  if (section.start == point if direction == "up" else section.end == point)]
    if len(others) > 1:
        # The final road may fan out directly into several leveling columns.
        # That junction has no single continuation; each terminal branch points
        # back to the preceding section, matching the collection schema.
        if direction == "up" and label == "main" and all(section.end in connections for section in others):
            return ""
        raise ValueError(f"Section {identifier}: multiple {direction} {label} links cannot fit the MongoDB schema")
    return others[0].section.id if others else ""


def _validate_transport_pairs(plan: Plan, sections: list[_DirectedSection]) -> None:
    adjacency = _adjacency(section.section for section in sections)
    for task in plan.tasks.values():
        if task.name != "transport":
            continue
        excavation = plan.tasks[str(task.parameters["excavation_loading_task"])]
        leveling = plan.tasks[str(task.parameters["leveling_task"])]
        pending = list(_ids(excavation.parameters["dump_node"]) & set(adjacency))
        if not pending:
            raise ValueError(f"Task {task.id}: its loading area has no connected transport road")
        reached: set[str] = set()
        while pending:
            point = pending.pop()
            if point in reached:
                continue
            reached.add(point)
            pending.extend(_other(section, point) for section in adjacency[point])
        missing = _ids(leveling.parameters["connection_node"]) - reached
        if missing:
            raise ValueError(f"Task {task.id}: no transport path from its loading area to leveling connection nodes: {', '.join(sorted(missing))}")


def _directed_transport_sections(plan: Plan, graph: GeoGraph) -> tuple[list[_DirectedSection], set[str]]:
    """Share the same validated road selection and orientation across consumers."""
    sections = _selected_sections(plan, graph)
    if not sections:
        raise ValueError("Transport tasks require connected GeoJSON road sections")
    loading, connections = _anchors(plan)
    main = [section for section in sections
            if graph.point(section.start).route_rank != 1 and graph.point(section.end).route_rank != 1]
    sub = [section for section in sections if section not in main]
    directed, depths, roots_by_point = _orient_main(main, loading, connections)
    directed.extend(_orient_sub(sub, depths, roots_by_point))
    _validate_transport_pairs(plan, directed)
    return directed, connections


def leveling_entry_node(plan: Plan, graph: GeoGraph, task: Task) -> str:
    """Select the first XML connection node that ends an up-direction road section."""
    if not any(candidate.name == "transport" for candidate in plan.tasks.values()):
        raise ValueError(f"Task {task.id}: leveling entry requires a transport road")
    directed, _ = _directed_transport_sections(plan, graph)
    endpoints = {section.end for section in directed}
    for node in task.parameters["connection_node"]:
        identifier = str(node)
        if identifier in endpoints:
            return identifier
    raise ValueError(
        f"Task {task.id}: no connection_node is an up-direction transport or passing section endpoint")


def build_route_documents(plan: Plan, graph: GeoGraph) -> list[dict]:
    """Return every main and passing section, oriented loading -> leveling."""
    if not any(task.name == "transport" for task in plan.tasks.values()):
        return []
    directed, connections = _directed_transport_sections(plan, graph)
    adjacency: dict[str, list[_DirectedSection]] = defaultdict(list)
    for section in directed:
        adjacency[section.start].append(section)
        adjacency[section.end].append(section)
    models = list(dict.fromkeys(model for alias, model in plan.machines.items()
                                if plan.machine_kind(alias) == "crawler_dump"))
    documents: list[dict] = []
    for section in directed:
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
                document[f"related_point_{direction}_{label}"] = _neighbor(section.section.id,
                    point, label, direction, adjacency, connections)
        documents.append(document)
    return documents
