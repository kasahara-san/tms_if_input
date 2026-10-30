"""Insert missing compiled records without modifying existing MongoDB data."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone


def parameter_filter(document: dict) -> dict:
    query = {'record_name': document['record_name']}
    if 'model_name' in document:
        # Plain scalar equality also matches array elements in MongoDB; $expr
        # keeps model-specific and shared array-valued records distinct.
        query['$expr'] = {
            '$eq': ['$model_name', {'$literal': document['model_name']}]
        }
    else:
        query['model_name'] = {'$exists': False}
    return query


def _next_id(used: set[int]) -> int:
    candidate = 1
    while candidate in used:
        candidate += 1
    used.add(candidate)
    return candidate


def _valid_task_id(value) -> bool:
    # BSON Int64 is an int subclass; boolean values are not task IDs.
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _collect_summary(inserted_documents: list[dict]) -> dict:
    """Collect names from parameter documents newly inserted in this run."""

    machinery_names = []
    joint_angle_param_names = []
    machinery_position_param_names = []
    block_position_param_names = []
    geo_fence_param_names = []  # 追加
    machinery_connection_param_names = []

    for document in inserted_documents:
        # --------------------------------------------------
        # 1. Collect machinery names
        # --------------------------------------------------
        model_name = document.get('model_name')

        if isinstance(model_name, str):
            if model_name not in machinery_names:
                machinery_names.append(model_name)

        elif isinstance(model_name, list):
            for name in model_name:
                if isinstance(name, str) and name not in machinery_names:
                    machinery_names.append(name)

        record = document.get('record_name')

        if not isinstance(record, str):
            continue

        # --------------------------------------------------
        # 2. Collect joint-angle parameter names
        # --------------------------------------------------
        waypoints = document.get('waypoints', [])

        has_joint_values = (
            isinstance(waypoints, list)
            and any(
                isinstance(waypoint, dict)
                and waypoint.get('type') == 'joint_values_absolute'
                for waypoint in waypoints
            )
        )

        has_target_angle = 'target_angle' in document

        if has_joint_values or has_target_angle:
            if record not in joint_angle_param_names:
                joint_angle_param_names.append(record)

        # --------------------------------------------------
        # 3. Collect construction-block position parameter names
        # --------------------------------------------------
        # The generated block documents contain block_center.
        if 'block_center' in document:
            if record not in block_position_param_names:
                block_position_param_names.append(record)

        # --------------------------------------------------
        # 4. Collect machinery position parameter names
        # --------------------------------------------------
        # Position documents contain x, y and z.
        # Exclude documents classified as construction-block data.
        elif all(axis in document for axis in ('x', 'y', 'z')):
            if record not in machinery_position_param_names:
                machinery_position_param_names.append(record)

        # --------------------------------------------------
        # 5. Collect GeoFence parameter names
        # --------------------------------------------------
        if record == 'geo_fence' and 'coordinates' in document:
            if record not in geo_fence_param_names:
                geo_fence_param_names.append(record)

        # --------------------------------------------------
        # 5. Collect machinery connection parameter names
        # --------------------------------------------------
        if (
            'node_id' in document
            and isinstance(document.get('position'), dict)
            and isinstance(document.get('connect_node'), list)
        ):
            if record not in machinery_connection_param_names:
                machinery_connection_param_names.append(record)


    return {
        'machinery_names': machinery_names,
        'joint_angle_param_names': joint_angle_param_names,
        'machinery_position_param_names': machinery_position_param_names,
        'block_position_param_names': block_position_param_names,
        'geo_fence_param_names': geo_fence_param_names,
        'machinery_connection_param_names': machinery_connection_param_names,
    }


def write_database(compilation, *, mongo_uri='mongodb://localhost:27017',
                   mongo_db='rostmsdb', timeout_ms=30000,
                   client_factory=None) -> dict:
    """Insert missing parameters/tasks and preserve every existing document.

    Parameters with matching record names/model scopes are skipped, even when
    their contents differ. Tasks are skipped only when type, model_name,
    description and task_sequence match. Different tasks for the same machine
    receive unused IDs. Imports never replace, reset or delete stored data.

    A summary document is inserted after the normal parameter and task
    insertions. It lists names from parameter documents newly inserted
    during this run.
    """
    from .compiler import validate_documents

    validate_documents(compilation)

    if timeout_ms <= 0:
        raise ValueError('MongoDB timeout must be positive')

    from pymongo.errors import PyMongoError

    if client_factory is None:
        from pymongo import MongoClient
        client_factory = MongoClient

    client = client_factory(
        mongo_uri,
        serverSelectionTimeoutMS=timeout_ms,
        connectTimeoutMS=timeout_ms,
        socketTimeoutMS=timeout_ms
    )

    operation = 'ping'

    try:
        client.admin.command('ping')

        database = client[mongo_db]
        parameters = database['parameter']
        tasks = database['task']

        # --------------------------------------------------
        # Read existing tasks
        # --------------------------------------------------
        operation = 'read task collection'

        current_tasks = list(
            tasks.find(
                {},
                {
                    'task_id': 1,
                    'model_name': 1,
                    'type': 1,
                    'description': 1,
                    'task_sequence': 1
                }
            )
        )

        positive_ids = [
            doc['task_id']
            for doc in current_tasks
            if _valid_task_id(doc.get('task_id'))
        ]

        used_ids = set(positive_ids)

        if len(positive_ids) != len(used_ids):
            raise ValueError(
                'Existing task records contain duplicate positive task IDs'
            )

        # --------------------------------------------------
        # Plan task insertions
        # --------------------------------------------------
        assigned = {}
        planned_tasks = []
        skipped_tasks = 0

        for source in compilation.tasks:
            existing = [
                doc for doc in current_tasks
                if all(
                    doc.get(key) == source[key]
                    for key in (
                        'type',
                        'model_name',
                        'description',
                        'task_sequence'
                    )
                )
            ]

            if existing:
                matching_ids = [
                    doc['task_id']
                    for doc in existing
                    if _valid_task_id(doc.get('task_id'))
                ]

                if not matching_ids:
                    raise ValueError(
                        f"Existing task for {source['model_name']} "
                        f"has an invalid task ID"
                    )

                # Preserve existing duplicates and choose a stable ID.
                task_id = min(matching_ids)
                assigned[source['model_name']] = task_id
                skipped_tasks += 1
                continue

            task_id = _next_id(used_ids)

            document = deepcopy(source)
            document['task_id'] = task_id

            planned_tasks.append(document)
            assigned[source['model_name']] = task_id

        # --------------------------------------------------
        # Insert new parameters
        # --------------------------------------------------
        inserted_parameters = 0
        skipped_parameters = 0

        # Keep only the parameter documents actually inserted this run.
        inserted_parameter_documents = []

        for source in compilation.parameters:
            operation = f"find parameter {source['record_name']}"

            if parameters.find_one(
                parameter_filter(source),
                {'_id': 1}
            ) is not None:
                skipped_parameters += 1
                continue

            document = deepcopy(source)

            operation = f"insert parameter {source['record_name']}"

            parameters.insert_one(document)

            inserted_parameters += 1
            inserted_parameter_documents.append(document)

        # --------------------------------------------------
        # Insert new tasks
        # --------------------------------------------------
        for document in planned_tasks:
            operation = f"insert task {document['model_name']}"
            tasks.insert_one(document)

        # --------------------------------------------------
        # Create and insert the summary document
        # --------------------------------------------------
        operation = 'collect parameter summary'

        summary = _collect_summary(inserted_parameter_documents)

        timestamp = datetime.now(timezone.utc).strftime(
            '%Y%m%dT%H%M%S%f'
        )

        summary_record_name = f'parameter_import_summary_{timestamp}'

        summary_document = {
            'type': 'static',
            'model_name': 'all',
            'record_name': summary_record_name,
            'machinery_names': summary['machinery_names'],
            'joint_angle_param_names':
                summary['joint_angle_param_names'],
            'machinery_position_param_names':
                summary['machinery_position_param_names'],
            'block_position_param_names':
                summary['block_position_param_names'],
            'geo_fence_param_names':
                summary['geo_fence_param_names'],  # 追加
            'machinery_connection_param_names':
                summary['machinery_connection_param_names'],
        }

        operation = f'insert parameter summary {summary_record_name}'

        parameters.insert_one(summary_document)

        # --------------------------------------------------
        # Return the import result
        # --------------------------------------------------
        return {
            'parameter_count': len(compilation.parameters),
            'task_count': len(compilation.tasks),
            'task_ids': assigned,
            'inserted_parameters': inserted_parameters,
            'skipped_parameters': skipped_parameters,
            'inserted_tasks': len(planned_tasks),
            'skipped_tasks': skipped_tasks,
            'summary_record_name': summary_record_name,
            'machinery_names': summary['machinery_names'],
            'joint_angle_param_names': summary['joint_angle_param_names'],
            'machinery_position_param_names':
                summary['machinery_position_param_names'],
            'block_position_param_names':
                summary['block_position_param_names'],
        }

    except PyMongoError as exc:
        raise RuntimeError(
            f'MongoDB {mongo_db}: {operation} failed '
            f'({type(exc).__name__}, timeout={timeout_ms} ms): {exc}'
        ) from exc

    finally:
        client.close()