"""All-node imports preserve stored data and collapse directed duplicate edges."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from test_database import ClientFactory, CommittedInsertTimeoutCollection, FakeDatabase, expected_summary
from tms_if_input.compiler import Compilation, compile_scenario, export_compilation
from tms_if_input.database import write_database
from tms_if_input.model import parse_geojson
from tms_if_input.node_graph import build_node_documents


SAMPLES = Path(__file__).resolve().parents[1] / 'json_samples'


def graph_data():
    points = {4294967295: [1.25, -2.5], 'B': [3, 4], 'C': [5, 6],
              'isolated': [7, 8], 'same-position': [3, 4]}
    features = [
        {'type': 'Feature', 'properties': {'id': node_id},
         'geometry': {'type': 'Point', 'coordinates': coordinates}}
        for node_id, coordinates in points.items()
    ]
    edges = [(4294967295, 'B'), ('B', 4294967295),
             (4294967295, 'B'), ('C', 'B')]
    for index, (start, end) in enumerate(edges):
        features.append({
            'type': 'Feature',
            'properties': {'id': f'edge-{index}', 'startid': start, 'endid': end},
            'geometry': {'type': 'LineString',
                         'coordinates': [points[start], points[end]]},
        })
    return {'type': 'FeatureCollection', 'features': features}


def test_all_points_have_one_document_and_deduplicated_undirected_neighbors():
    source = graph_data()
    original_source = deepcopy(source)
    graph = parse_geojson(source)
    original_graph = deepcopy(graph)
    documents = build_node_documents(graph)
    index, *nodes = documents
    graph_id = index['graph_id']
    assert index == {
        'type': 'static', 'record_name': f'geojson_node_ids_{graph_id}',
        'graph_id': graph_id,
        'node_ids': ['4294967295', 'B', 'C', 'isolated', 'same-position'],
    }
    expected = {
        '4294967295': ([1.25, -2.5], ['B']),
        'B': ([3, 4], ['4294967295', 'C']),
        'C': ([5, 6], ['B']),
        'isolated': ([7, 8], []),
        'same-position': ([3, 4], []),
    }
    assert len(nodes) == len(expected)
    for node in nodes:
        node_id = node['node_id']
        coordinates, neighbors = expected[node_id]
        assert node == {
            'type': 'static', 'record_name': f'geojson_node_{graph_id}_{node_id}',
            'graph_id': graph_id, 'node_id': node_id,
            'position': {'x': coordinates[0], 'y': coordinates[1], 'z': 0.0},
            'connect_node': neighbors,
        }
    assert source == original_source
    assert graph == original_graph


@pytest.mark.parametrize('change', ['order', 'reverse', 'one-way', 'vertices'])
def test_equivalent_connectivity_keeps_identical_documents(change):
    source = graph_data()
    expected = build_node_documents(parse_geojson(source))
    if change == 'order':
        source['features'].reverse()
    elif change == 'one-way':
        source['features'] = [feature for feature in source['features']
                              if feature['properties']['id'] not in {'edge-1', 'edge-2'}]
    else:
        for feature in source['features']:
            if feature['geometry']['type'] != 'LineString':
                continue
            props = feature['properties']
            props['id'] = 'renamed-' + props['id']
            if change == 'reverse':
                props['startid'], props['endid'] = props['endid'], props['startid']
                feature['geometry']['coordinates'].reverse()
            else:
                feature['geometry']['coordinates'].insert(1, [99, 100])
    assert build_node_documents(parse_geojson(source)) == expected


@pytest.mark.parametrize('count', [0, 1, 3])
def test_graphs_without_lines_still_have_an_id_list_and_all_points(count):
    source = graph_data()
    source['features'] = source['features'][:count]
    documents = build_node_documents(parse_geojson(source))
    assert len(documents) == count + 1
    assert documents[0]['node_ids'] == [node['node_id'] for node in documents[1:]]
    assert all(node['connect_node'] == [] for node in documents[1:])


@pytest.mark.parametrize('filename, count, connections', [
    ('261004-kyoto.geojson', 42, 45),
    ('261008-kyoto.geojson', 42, 45),
    ('261019-dxfield_2.geojson', 15, 13),
])
def test_every_sample_point_and_connection_is_preserved(filename, count, connections):
    source = json.loads((SAMPLES / filename).read_text(encoding='utf-8-sig'))
    points = {str(feature['properties']['id']): feature['geometry']['coordinates']
              for feature in source['features'] if feature['geometry']['type'] == 'Point'}
    edges = {frozenset(str(feature['properties'][key]) for key in ('startid', 'endid'))
             for feature in source['features'] if feature['geometry']['type'] == 'LineString'}
    index, *nodes = build_node_documents(parse_geojson(source))
    assert len(nodes) == len(points) == count
    assert index['node_ids'] == sorted(points)
    assert len(edges) == connections
    assert sum(len(node['connect_node']) for node in nodes) == 2 * connections
    for node in nodes:
        node_id = node['node_id']
        assert node['position'] == {'x': points[node_id][0], 'y': points[node_id][1], 'z': 0.0}
        assert node['connect_node'] == sorted(
            next(iter(edge - {node_id})) for edge in edges if node_id in edge)


def test_existing_scenario_gets_only_node_data_and_reimport_keeps_nodes(tmp_path):
    geojson, xml = SAMPLES / '261008-kyoto.geojson', SAMPLES / '261008-kyoto.xml'
    source_bytes = geojson.read_bytes(), xml.read_bytes()
    compilation = compile_scenario(geojson, xml)
    original_compilation = deepcopy(compilation)
    previous_parameters = [deepcopy(document) for document in compilation.parameters
                           if 'graph_id' not in document]
    previous_parameters.append({'record_name': 'runtime-state', 'completed': True})
    next(document for document in previous_parameters
         if document['record_name'] == 'initialize_flgs')['initialize_flg_zx200'] = True
    previous_tasks = deepcopy(compilation.tasks)
    database = FakeDatabase(parameters=previous_parameters, tasks=previous_tasks)
    factory = ClientFactory(database)
    expected_nodes = build_node_documents(parse_geojson(json.loads(source_bytes[0])))
    assert compilation.parameters[-43:] == expected_nodes

    first = write_database(compilation, client_factory=factory)
    assert (first['inserted_parameters'], first['inserted_tasks']) == (43, 0)
    assert database['parameter'].documents[:len(previous_parameters)] == previous_parameters
    assert database['parameter'].documents[len(previous_parameters):-1] == expected_nodes
    # Keep the existing import-summary behavior, without classifying graph
    # positions as machinery position parameters.
    assert database['parameter'].documents[-1]['record_name'] == first['summary_record_name']
    assert first['machinery_position_param_names'] == []
    saved = deepcopy(database['parameter'].documents)

    repeated = write_database(compilation, client_factory=factory)
    assert (repeated['inserted_parameters'], repeated['inserted_tasks']) == (0, 0)
    assert repeated['skipped_parameters'] == len(compilation.parameters)
    assert database['parameter'].documents[:-1] == saved
    assert [document for document in database['parameter'].documents
            if 'graph_id' in document] == expected_nodes
    assert database['task'].documents == previous_tasks
    assert compilation == original_compilation
    assert all(client.closed for client in factory.clients)
    assert (geojson.read_bytes(), xml.read_bytes()) == source_bytes

    export_compilation(compilation, tmp_path)
    assert json.loads((tmp_path / 'parameters.json').read_text()) == compilation.parameters


@pytest.mark.parametrize('change', ['position', 'connection', 'new-node'])
def test_changed_graph_is_appended_and_existing_generations_are_preserved(change):
    source = graph_data()
    first_nodes = build_node_documents(parse_geojson(source))
    database = FakeDatabase(parameters=[{'record_name': 'manual', 'keep': True}])
    factory = ClientFactory(database)
    write_database(Compilation(first_nodes, []), client_factory=factory)
    saved = deepcopy(database['parameter'].documents)

    if change == 'position':
        source['features'][3]['geometry']['coordinates'] = [70, 80]
    elif change == 'connection':
        source['features'].append({
            'type': 'Feature',
            'properties': {'id': 'new-edge', 'startid': 'C', 'endid': 'isolated'},
            'geometry': {'type': 'LineString', 'coordinates': [[5, 6], [7, 8]]},
        })
    else:
        source['features'].append({
            'type': 'Feature', 'properties': {'id': 'new-node'},
            'geometry': {'type': 'Point', 'coordinates': [90, 100]},
        })
    second_nodes = build_node_documents(parse_geojson(source))
    assert first_nodes[0]['graph_id'] != second_nodes[0]['graph_id']
    assert {document['record_name'] for document in first_nodes}.isdisjoint(
        document['record_name'] for document in second_nodes)
    result = write_database(Compilation(second_nodes, []), client_factory=factory)
    assert result['inserted_parameters'] == len(second_nodes)
    assert database['parameter'].documents[:len(saved)] == saved
    assert database['parameter'].documents[len(saved):-1] == second_nodes

    repeated = write_database(Compilation(second_nodes, []), client_factory=factory)
    assert repeated['inserted_parameters'] == 0
    assert [document for document in database['parameter'].documents
            if 'graph_id' in document] == first_nodes + second_nodes


@pytest.mark.parametrize('failure', ['before-insert', 'after-commit'])
def test_partial_graph_import_retries_only_missing_documents(failure):
    documents = build_node_documents(parse_geojson(graph_data()))
    original = deepcopy(documents)
    legacy = {'record_name': 'manual', 'state': {'keep': True}}
    database = FakeDatabase(parameters=[legacy], parameter_failure=3)
    if failure == 'after-commit':
        database.collections['parameter'] = CommittedInsertTimeoutCollection([legacy], timeout_at=3)
    factory = ClientFactory(database)
    with pytest.raises(RuntimeError):
        write_database(Compilation(documents, []), client_factory=factory)
    committed = deepcopy(database['parameter'].documents)
    assert committed[0] == legacy
    assert committed[1:] == documents[:len(committed) - 1]
    assert database['task'].documents == []
    assert all(client.closed for client in factory.clients)

    database['parameter'].fail_insert_at = None
    result = write_database(Compilation(documents, []), client_factory=factory)
    summary = expected_summary(
        result, connections=[document['record_name']
                             for document in documents[len(committed) - 1:]])
    assert result['inserted_parameters'] == len(documents) - (len(committed) - 1)
    assert result['skipped_parameters'] == len(committed) - 1
    assert database['parameter'].documents == [legacy] + documents + [summary]
    assert documents == original
    assert all(client.closed for client in factory.clients)
