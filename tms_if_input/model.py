"""Validated input model, independent of ROS and MongoDB."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET


TASK_NAMES = {'start', 'initialize', 'excavation_loading', 'transport', 'leveling', 'end'}
MODEL_KINDS = {
    'ic120': 'crawler_dump', 'mst110cr': 'crawler_dump',
    'mst2200vdr': 'crawler_dump', 'zx120': 'excavator',
    'zx200': 'excavator', 'd37pxi': 'bulldozer',
}


def identifier(value) -> str:
    """Keep IDs opaque; never truncate the unsigned IDs used in the input."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f'ID must be a nonempty string or integer: {value!r}')
    result = str(value).strip()
    if not result:
        raise ValueError('ID must not be empty')
    return result


def number(value, context: str):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{context}: expected a number, got {value!r}')
    if not math.isfinite(value):
        raise ValueError(f'{context}: number must be finite')
    return value


def vector(value, keys: str, context: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f'{context}: expected an object')
    for key in keys:
        if key not in value:
            raise ValueError(f'{context}: missing {key}')
        number(value[key], f'{context}.{key}')
    return value


def parse_parameter(value: str):
    """Parse JSON-like vectors with bare keys without evaluating input code."""
    value = value.strip()
    if not value:
        raise ValueError('Empty parameter value')
    if value.startswith(('{', '[')):
        quoted = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z_0-9]*)\s*:',
                        r'\1"\2":', value)
        try:
            return json.loads(quoted)
        except ValueError as exc:
            raise ValueError(f'Invalid parameter value {value!r}: {exc}') from exc
    if re.fullmatch(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?', value):
        return float(value) if any(c in value for c in '.eE') else int(value)
    return value


@dataclass(frozen=True)
class Task:
    id: str
    name: str
    parameters: dict

    @property
    def machine(self):
        return self.parameters.get('machine')


@dataclass(frozen=True)
class Plan:
    machines: dict[str, str]
    tasks: dict[str, Task]
    edges: tuple[tuple[str, str], ...]

    def model(self, task: Task) -> str:
        return self.machines[task.machine]

    def predecessors(self, task_id: str) -> list[Task]:
        return [self.tasks[source] for source, target in self.edges if target == task_id]

    def ordered_tasks(self) -> list[Task]:
        # Input order breaks ties, but execution dependencies come from flow.
        indegree = {key: 0 for key in self.tasks}
        children = {key: [] for key in self.tasks}
        for source, target in self.edges:
            indegree[target] += 1
            children[source].append(target)
        ready = [key for key in self.tasks if indegree[key] == 0]
        ordered = []
        while ready:
            key = ready.pop(0)
            ordered.append(self.tasks[key])
            for target in children[key]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        if len(ordered) != len(self.tasks):
            raise ValueError('Task flow contains a cycle')
        return ordered

    def machine_kind(self, alias: str) -> str:
        tasks = [task for task in self.tasks.values() if task.machine == alias]
        roles = {task.name for task in tasks} & {'excavation_loading', 'transport', 'leveling'}
        inferred = {'excavation_loading': 'excavator', 'transport': 'crawler_dump',
                    'leveling': 'bulldozer'}
        model = self.machines[alias].lower()
        known = MODEL_KINDS.get(model) or MODEL_KINDS.get(re.sub(r'_\d+$', '', model))
        kinds = {inferred[role] for role in roles}
        if known:
            kinds.add(known)
        if not kinds:
            if not tasks:
                return 'unused'
            joints = {key for task in tasks for key in task.parameters}
            if joints & {'boom', 'arm', 'bucket'}:
                kinds.add('excavator')
            elif joints & {'swing', 'vessel'}:
                kinds.add('crawler_dump')
        if len(kinds) != 1:
            raise ValueError(f'Machine {alias!r}: unknown or conflicting machine kind')
        return kinds.pop()


def record_name(plan: Plan, task: Task, base: str) -> str:
    """Preserve the specified names; disambiguate repeated work areas/tasks."""
    shared = base.startswith(('loading_position_', 'dump_node_', 'block_')) or (
        base == 'dumps_entry_point_leveling_area')
    matches = [other for other in plan.tasks.values()
               if other.name == task.name and
               (shared or other.machine == task.machine)]
    return f'{base}_{task.id}' if len(matches) > 1 else base


@dataclass(frozen=True)
class Point:
    id: str
    x: float
    y: float

    def xy(self) -> dict:
        return {'x': self.x, 'y': self.y}


@dataclass(frozen=True)
class Section:
    id: str
    start: str
    end: str
    coordinates: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class GeoGraph:
    points: dict[str, Point]
    sections: tuple[Section, ...]
    geofences: tuple

    def point(self, point_id) -> Point:
        key = identifier(point_id)
        if key not in self.points:
            raise ValueError(f'GeoJSON Point ID {key!r} was not found')
        return self.points[key]


def _array(value, context: str) -> list:
    if not isinstance(value, list) or not value:
        raise ValueError(f'{context}: expected a nonempty array')
    return value


def _node_ids(value, context: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        requirement = 'an array' if allow_empty else 'a nonempty array'
        raise ValueError(f'{context}: expected {requirement}')
    values = [identifier(node) for node in value]
    if len(values) != len(set(values)):
        raise ValueError(f'{context}: duplicate node IDs')
    return values


def validate_plan(plan: Plan) -> None:
    if not plan.tasks:
        raise ValueError('XML has no tasks')
    starts = [task for task in plan.tasks.values() if task.name == 'start']
    ends = [task for task in plan.tasks.values() if task.name == 'end']
    if len(starts) != 1 or len(ends) != 1:
        raise ValueError('Task flow requires exactly one start and one end')
    ordered = plan.ordered_tasks()
    ancestors = {}
    for task in ordered:
        parents = plan.predecessors(task.id)
        ancestors[task.id] = {parent.id for parent in parents}
        for parent in parents:
            ancestors[task.id].update(ancestors[parent.id])
        if task.name != 'start' and starts[0].id not in ancestors[task.id]:
            raise ValueError(f'Task {task.id}: unreachable from start')
    if plan.predecessors(starts[0].id) or any(s == ends[0].id for s, _ in plan.edges):
        raise ValueError('Start must have no incoming edges; end must have no outgoing edges')
    for task in ordered:
        if task.name != 'end' and task.id not in ancestors[ends[0].id]:
            raise ValueError(f'Task {task.id}: cannot reach end')
        if task.name in {'start', 'end'}:
            continue
        if task.machine not in plan.machines:
            raise ValueError(f'Task {task.id}: unknown machine {task.machine!r}')
        kind = plan.machine_kind(task.machine)
        params = task.parameters
        context = f'Task {task.id} ({task.name})'
        required = {
            'initialize': {'target_node', 'rotation'},
            'excavation_loading': {'block_size', 'block_angle', 'block_center',
                                   'block_vector', 'backhoe_node', 'dump_node', 'rotation'},
            'leveling': {'leveling_height', 'block_size', 'block_center', 'block_vector',
                         'soil_volume', 'dump_node', 'bulldozer_node', 'connection_node'},
            'transport': {'excavation_loading_task', 'leveling_task', 'main_node', 'sub_node'},
        }[task.name]
        missing = required - params.keys()
        if missing:
            raise ValueError(f'{context}: missing parameters {sorted(missing)}')
        if task.name == 'initialize':
            identifier(params['target_node'])
            rotation = vector(params['rotation'], 'xyzw', f'{context}.rotation')
            if not any(rotation[axis] for axis in 'xyzw'):
                raise ValueError(f'{context}: rotation quaternion must not be zero')
            for joint in ('swing', 'boom', 'arm', 'bucket', 'vessel'):
                if joint in params:
                    number(params[joint], f'{context}.{joint}')
            if kind == 'excavator' and not any(k in params for k in ('swing', 'boom', 'arm', 'bucket')):
                raise ValueError(f'{context}: excavator initial pose has no joint angles')
            if kind == 'crawler_dump' and not {'swing', 'vessel'} <= params.keys():
                raise ValueError(f'{context}: crawler dump requires swing and vessel')
        elif task.name in {'excavation_loading', 'leveling'}:
            axes = 'xyz' if task.name == 'excavation_loading' else 'xy'
            size = vector(params['block_size'], axes, f'{context}.block_size')
            if any(size[axis] <= 0 for axis in axes):
                raise ValueError(f'{context}: block dimensions must be positive')
            direction = vector(params['block_vector'], 'xyz', f'{context}.block_vector')
            if math.hypot(direction['x'], direction['y']) == 0:
                raise ValueError(f'{context}: block_vector must have a nonzero XY direction')
            centers = _array(params['block_center'], f'{context}.block_center')
            for center in centers:
                vector(center, 'xyz', f'{context}.block_center')
            node_fields = ('backhoe_node', 'dump_node') if task.name == 'excavation_loading' else (
                'dump_node', 'bulldozer_node')
            for field in node_fields:
                nodes = _array(params[field], f'{context}.{field}')
                for node in nodes:
                    identifier(node)
                if len(nodes) != len(centers):
                    raise ValueError(f'{context}: {field} and block_center lengths differ')
            if task.name == 'excavation_loading':
                number(params['block_angle'], f'{context}.block_angle')
                rotations = _array(params['rotation'], f'{context}.rotation')
                if len(rotations) != len(params['dump_node']):
                    raise ValueError(f'{context}: rotation and dump_node lengths differ')
                for index, value in enumerate(rotations):
                    rotation_context = f'{context}.rotation[{index}]'
                    rotation = vector(value, 'xyzw', rotation_context)
                    if set(rotation) != set('xyzw'):
                        raise ValueError(f'{rotation_context}: quaternion requires exactly x, y, z and w')
                    if not any(rotation[axis] for axis in 'xyzw'):
                        raise ValueError(f'{rotation_context}: rotation quaternion must not be zero')
            else:
                number(params['leveling_height'], f'{context}.leveling_height')
                volumes = _array(params['soil_volume'], f'{context}.soil_volume')
                if len(volumes) != len(centers):
                    raise ValueError(f'{context}: soil_volume and block_center lengths differ')
                for volume in volumes:
                    if number(volume, f'{context}.soil_volume') < 0:
                        raise ValueError(f'{context}: soil_volume must not be negative')
                for node in _array(params['connection_node'], f'{context}.connection_node'):
                    identifier(node)
        else:
            main = _node_ids(params['main_node'], f'{context}.main_node')
            sub = _node_ids(params['sub_node'], f'{context}.sub_node', allow_empty=True)
            if set(main) & set(sub):
                raise ValueError(f'{context}: main_node and sub_node must not overlap')
            for field, expected in [('excavation_loading_task', 'excavation_loading'),
                                    ('leveling_task', 'leveling')]:
                target = identifier(params[field])
                if target not in plan.tasks or plan.tasks[target].name != expected:
                    raise ValueError(f'{context}: {field} references no {expected} task: {target}')
    for alias in plan.machines:
        machine_tasks = [task for task in ordered if task.machine == alias]
        for previous, task in zip(machine_tasks, machine_tasks[1:]):
            if previous.id not in ancestors[task.id]:
                raise ValueError(f'Machine {alias}: tasks {previous.id} and {task.id} run in parallel')
        for task in machine_tasks:
            if task.name != 'initialize' and not any(
                    parent.name == 'initialize' and parent.machine == alias
                    for parent in ordered if parent.id in ancestors[task.id]):
                raise ValueError(f'Task {task.id}: machine has no preceding initialize task')


def parse_plan(xml_text: str) -> Plan:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f'Invalid construction XML: {exc}') from exc
    # Support namespaced documents without binding to a sample namespace.
    for element in root.iter():
        element.tag = element.tag.rsplit('}', 1)[-1]
    machines = {}
    for element in root.findall('./machines/machine'):
        alias = identifier(element.get('id'))
        model = identifier(element.get('type'))
        if alias in machines or model in machines.values():
            raise ValueError(f'Duplicate machine alias/type: {alias}/{model}; use unique instance types')
        machines[alias] = model
    tasks = {}
    for element in root.findall('./procedure/tasks/task'):
        task_id = identifier(element.get('id'))
        name = element.get('name')
        if name not in TASK_NAMES:
            raise ValueError(f'Task {task_id}: unsupported task name {name!r}')
        if task_id in tasks:
            raise ValueError(f'Duplicate task ID: {task_id}')
        params = {}
        for param in element.findall('./parameter'):
            key = identifier(param.get('name'))
            if key in params or param.get('value') is None:
                raise ValueError(f'Task {task_id}: duplicate or missing parameter {key}')
            raw = param.get('value')
            # Scalar references are opaque strings, even if they look numeric.
            params[key] = identifier(raw) if key in {
                'machine', 'target_node', 'excavation_loading_task', 'leveling_task'
            } else parse_parameter(raw)
        tasks[task_id] = Task(task_id, name, params)
    edges = []
    for edge in root.findall('./procedure/flow/edge'):
        pair = (identifier(edge.get('from')), identifier(edge.get('to')))
        if any(key not in tasks for key in pair):
            raise ValueError(f'Flow edge references unknown task: {pair}')
        if pair not in edges:
            edges.append(pair)
    plan = Plan(machines, tasks, tuple(edges))
    validate_plan(plan)
    return plan


def parse_geojson(data: dict) -> GeoGraph:
    if not isinstance(data, dict) or data.get('type') != 'FeatureCollection':
        raise ValueError('GeoJSON root must be a FeatureCollection')
    if not isinstance(data.get('features'), list):
        raise ValueError('GeoJSON features must be an array')
    points, sections, geofences, ids = {}, [], [], set()

    def coordinates(raw, context):
        if not isinstance(raw, list) or len(raw) < 2:
            raise ValueError(f'{context}: coordinates require x and y')
        return (number(raw[0], context), number(raw[1], context))

    for i, feature in enumerate(data['features']):
        if not isinstance(feature, dict) or feature.get('type') != 'Feature':
            raise ValueError(f'GeoJSON features[{i}] must be a Feature')
        props, geometry = feature.get('properties'), feature.get('geometry')
        if not isinstance(props, dict) or not isinstance(geometry, dict):
            raise ValueError(f'GeoJSON features[{i}]: missing properties/geometry')
        kind = geometry.get('type')
        if props.get('name') == 'geo_fence' and kind != 'Polygon':
            raise ValueError('geo_fence geometry must be a Polygon')
        if kind in {'Point', 'LineString'}:
            key = identifier(props.get('id', feature.get('id')))
            if key in ids:
                raise ValueError(f'Duplicate GeoJSON feature ID: {key}')
            ids.add(key)
        if kind == 'Point':
            x, y = coordinates(geometry.get('coordinates'), f'Point {key}')
            points[key] = Point(key, x, y)
        elif kind == 'LineString':
            raw = _array(geometry.get('coordinates'), f'LineString {key}')
            if len(raw) < 2:
                raise ValueError(f'LineString {key}: requires at least two coordinates')
            sections.append(Section(key, identifier(props.get('startid')),
                                    identifier(props.get('endid')),
                                    tuple(coordinates(v, f'LineString {key}') for v in raw)))
        elif props.get('name') == 'geo_fence':
            if kind != 'Polygon':
                raise ValueError('geo_fence geometry must be a Polygon')
            rings = []
            for raw in _array(geometry.get('coordinates'), 'geo_fence'):
                ring = tuple(coordinates(v, 'geo_fence ring') for v in _array(raw, 'geo_fence ring'))
                if len(ring) < 4 or ring[0] != ring[-1]:
                    raise ValueError('geo_fence ring must have at least four points and be closed')
                rings.append(ring)
            geofences.append(tuple(rings))
    graph = GeoGraph(points, tuple(sections), tuple(geofences))
    for section in sections:
        start, end = graph.point(section.start), graph.point(section.end)
        if start.id == end.id:
            raise ValueError(f'Section {section.id}: self loops are unsupported')
        for actual, expected in [(section.coordinates[0], (start.x, start.y)),
                                 (section.coordinates[-1], (end.x, end.y))]:
            if not all(math.isclose(a, b, rel_tol=0, abs_tol=0.001)
                       for a, b in zip(actual, expected)):
                raise ValueError(f'Section {section.id}: coordinates disagree with startid/endid')
    return graph


def load_inputs(geojson_path, xml_path) -> tuple[Plan, GeoGraph]:
    try:
        data = json.loads(Path(geojson_path).read_text(encoding='utf-8-sig'))
    except ValueError as exc:
        raise ValueError(f'Invalid GeoJSON {geojson_path}: {exc}') from exc
    plan = parse_plan(Path(xml_path).read_text(encoding='utf-8-sig'))
    graph = parse_geojson(data)
    for task in plan.tasks.values():
        for name, value in task.parameters.items():
            if name == 'target_node':
                graph.point(value)
            elif name in {'backhoe_node', 'dump_node', 'bulldozer_node', 'connection_node',
                          'main_node', 'sub_node'}:
                for node_id in value:
                    graph.point(node_id)
    return plan, graph
