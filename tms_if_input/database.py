"""Insert missing compiled records without modifying existing MongoDB data."""

from __future__ import annotations

from copy import deepcopy


OWNER_FIELD = '_tms_if_input'


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


def write_database(compilation, *, mongo_uri='mongodb://localhost:27017',
                   mongo_db='rostmsdb', import_key='default', timeout_ms=5000,
                   client_factory=None) -> dict:
    """Insert missing parameters/tasks and preserve every existing document.

    Matching record names/model scopes are skipped, even when their contents
    differ from the compilation. Repeated or smaller imports never replace,
    reset or delete previously stored data. A retry adds only records that
    were not inserted before a failure.
    """
    from .compiler import validate_documents
    validate_documents(compilation)
    if not import_key.strip():
        raise ValueError('import_key must not be empty')
    if timeout_ms <= 0:
        raise ValueError('MongoDB timeout must be positive')
    if client_factory is None:
        from pymongo import MongoClient
        client_factory = MongoClient
    marker = {'import_key': import_key}
    client = client_factory(mongo_uri, serverSelectionTimeoutMS=timeout_ms,
                            connectTimeoutMS=timeout_ms, socketTimeoutMS=timeout_ms)
    try:
        client.admin.command('ping')
        database = client[mongo_db]
        parameters, tasks = database['parameter'], database['task']
        current_tasks = list(tasks.find({}, {'task_id': 1, 'model_name': 1, 'type': 1}))
        used_ids = {doc['task_id'] for doc in current_tasks
                    if type(doc.get('task_id')) is int and doc['task_id'] > 0}
        positive_ids = [doc['task_id'] for doc in current_tasks
                        if type(doc.get('task_id')) is int and doc['task_id'] > 0]
        if len(positive_ids) != len(used_ids):
            raise ValueError('Existing task records contain duplicate positive task IDs')
        assigned = {}
        planned_tasks = []
        skipped_tasks = 0
        for source in compilation.tasks:
            existing = [doc for doc in current_tasks
                        if doc.get('model_name') == source['model_name'] and
                        doc.get('type') == 'task']
            if len(existing) > 1:
                raise ValueError(f"Duplicate existing task records for {source['model_name']}")
            if existing:
                task_id = existing[0].get('task_id')
                if type(task_id) is not int or task_id <= 0:
                    raise ValueError(f"Existing task for {source['model_name']} has an invalid task ID")
                assigned[source['model_name']] = task_id
                skipped_tasks += 1
                continue
            task_id = _next_id(used_ids)
            document = deepcopy(source)
            document['task_id'] = task_id
            document[OWNER_FIELD] = marker.copy()
            planned_tasks.append(document)
            assigned[source['model_name']] = task_id
        inserted_parameters = 0
        skipped_parameters = 0
        for source in compilation.parameters:
            if parameters.find_one(parameter_filter(source), {'_id': 1}) is not None:
                skipped_parameters += 1
                continue
            document = deepcopy(source)
            document[OWNER_FIELD] = marker.copy()
            parameters.insert_one(document)
            inserted_parameters += 1
        for document in planned_tasks:
            tasks.insert_one(document)
        return {'parameter_count': len(compilation.parameters),
                'task_count': len(compilation.tasks), 'task_ids': assigned,
                'inserted_parameters': inserted_parameters,
                'skipped_parameters': skipped_parameters,
                'inserted_tasks': len(planned_tasks), 'skipped_tasks': skipped_tasks}
    finally:
        client.close()
