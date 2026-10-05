"""Insert missing compiled records without modifying existing MongoDB data."""

from __future__ import annotations

from copy import deepcopy


def parameter_filter(document: dict) -> dict:
    query = {'record_name': document['record_name']}
    if 'model_name' in document:
        # Plain scalar equality also matches array elements in MongoDB; $expr
        # keeps model-specific and shared array-valued records distinct.
        query['$expr'] = {'$eq': ['$model_name', {'$literal': document['model_name']}]}
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


def write_database(compilation, *, mongo_uri='mongodb://localhost:27017',
                   mongo_db='rostmsdb', timeout_ms=30000,
                   client_factory=None) -> dict:
    """Insert missing parameters/tasks and preserve every existing document.

    Parameters with matching record names/model scopes are skipped, even when
    their contents differ. Tasks are skipped only when type, model_name,
    description and task_sequence match. Different tasks for the same machine
    receive unused IDs. Imports never replace, reset or delete stored data.
    A retry adds only records that were not inserted before a failure.
    """
    from .compiler import validate_documents
    validate_documents(compilation)
    if timeout_ms <= 0:
        raise ValueError('MongoDB timeout must be positive')
    from pymongo.errors import PyMongoError
    if client_factory is None:
        from pymongo import MongoClient
        client_factory = MongoClient
    client = client_factory(mongo_uri, serverSelectionTimeoutMS=timeout_ms,
                            connectTimeoutMS=timeout_ms, socketTimeoutMS=timeout_ms)
    operation = 'ping'
    try:
        client.admin.command('ping')
        database = client[mongo_db]
        parameters, tasks = database['parameter'], database['task']
        operation = 'read task collection'
        current_tasks = list(tasks.find({}, {'task_id': 1, 'model_name': 1, 'type': 1,
                                             'description': 1, 'task_sequence': 1}))
        positive_ids = [doc['task_id'] for doc in current_tasks
                        if _valid_task_id(doc.get('task_id'))]
        used_ids = set(positive_ids)
        if len(positive_ids) != len(used_ids):
            raise ValueError('Existing task records contain duplicate positive task IDs')
        assigned = {}
        planned_tasks = []
        skipped_tasks = 0
        for source in compilation.tasks:
            existing = [doc for doc in current_tasks
                        if all(doc.get(key) == source[key] for key in
                               ('type', 'model_name', 'description', 'task_sequence'))]
            if existing:
                matching_ids = [doc['task_id'] for doc in existing
                                if _valid_task_id(doc.get('task_id'))]
                if not matching_ids:
                    raise ValueError(f"Existing task for {source['model_name']} has an invalid task ID")
                # Existing duplicates are preserved; retries choose the same
                # usable ID regardless of MongoDB's document iteration order.
                task_id = min(matching_ids)
                assigned[source['model_name']] = task_id
                skipped_tasks += 1
                continue
            task_id = _next_id(used_ids)
            document = deepcopy(source)
            document['task_id'] = task_id
            planned_tasks.append(document)
            assigned[source['model_name']] = task_id
        inserted_parameters = 0
        skipped_parameters = 0
        for source in compilation.parameters:
            operation = f"find parameter {source['record_name']}"
            if parameters.find_one(parameter_filter(source), {'_id': 1}) is not None:
                skipped_parameters += 1
                continue
            document = deepcopy(source)
            operation = f"insert parameter {source['record_name']}"
            parameters.insert_one(document)
            inserted_parameters += 1
        for document in planned_tasks:
            operation = f"insert task {document['model_name']}"
            tasks.insert_one(document)
        return {'parameter_count': len(compilation.parameters),
                'task_count': len(compilation.tasks), 'task_ids': assigned,
                'inserted_parameters': inserted_parameters,
                'skipped_parameters': skipped_parameters,
                'inserted_tasks': len(planned_tasks), 'skipped_tasks': skipped_tasks}
    except PyMongoError as exc:
        raise RuntimeError(
            f'MongoDB {mongo_db}: {operation} failed '
            f'({type(exc).__name__}, timeout={timeout_ms} ms): {exc}') from exc
    finally:
        client.close()
