"""MongoDB imports add missing records and preserve every existing document."""

from copy import deepcopy
from types import SimpleNamespace
import unittest

from tms_if_input.compiler import Compilation
from tms_if_input.database import parameter_filter, write_database


MISSING = object()
LEGACY_METADATA_FIELD = "_tms_if_input"


def field(document, path):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def matches(document, query):
    """Match the writer's MongoDB subset, including exact array-valued scopes.

    A plain scalar query also matches an array member; aggregation $eq compares
    the whole value. Missing model_name and explicit null remain distinct.
    """
    for key, expected in query.items():
        if key == "$expr":
            left, right = expected["$eq"]
            actual = field(document, left.removeprefix("$"))
            if actual is MISSING or actual != right["$literal"]:
                return False
            continue
        actual = field(document, key)
        if isinstance(expected, dict):
            unsupported = set(expected) - {"$exists", "$ne"}
            if unsupported:
                raise AssertionError(f"Unsupported fake query operators: {unsupported}")
            if "$exists" in expected and (actual is not MISSING) != expected["$exists"]:
                return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
        elif actual is MISSING:
            if expected is not None:
                return False
        elif actual != expected and not (isinstance(actual, list) and expected in actual):
            return False
    return True


def projected(document, projection):
    if projection is None:
        return deepcopy(document)
    included = {key for key, value in projection.items() if value and key != "_id"}
    if included:
        return {key: deepcopy(value) for key, value in document.items()
                if key in included or (key == "_id" and projection.get("_id", 1))}
    return {key: deepcopy(value) for key, value in document.items()
            if projection.get(key, 1)}


class FakeCollection:
    def __init__(self, documents=(), fail_insert_at=None):
        self.documents = deepcopy(list(documents))
        self.fail_insert_at = fail_insert_at
        self.insert_calls = []
        self.find_one_calls = []

    def find(self, query=None, projection=None):
        return [projected(document, projection) for document in self.documents
                if matches(document, query or {})]

    def find_one(self, query=None, projection=None):
        self.find_one_calls.append(deepcopy(query or {}))
        for document in self.documents:
            if matches(document, query or {}):
                return projected(document, projection)
        return None

    def insert_one(self, document):
        self.insert_calls.append(deepcopy(document))
        if len(self.insert_calls) == self.fail_insert_at:
            raise RuntimeError("Injected insertion failure")
        self.documents.append(deepcopy(document))
        return SimpleNamespace(inserted_id=len(self.documents))

    def _forbidden_mutation(self, *args, **kwargs):
        raise AssertionError("Imports must never replace, update, delete or drop existing data")

    replace_one = _forbidden_mutation
    update_one = _forbidden_mutation
    update_many = _forbidden_mutation
    delete_one = _forbidden_mutation
    delete_many = _forbidden_mutation
    find_one_and_replace = _forbidden_mutation
    find_one_and_update = _forbidden_mutation
    find_one_and_delete = _forbidden_mutation
    bulk_write = _forbidden_mutation
    drop = _forbidden_mutation
    remove = _forbidden_mutation
    save = _forbidden_mutation


class FakeDatabase:
    def __init__(self, parameters=(), tasks=(), parameter_failure=None, task_failure=None):
        self.collections = {
            "parameter": FakeCollection(parameters, parameter_failure),
            "task": FakeCollection(tasks, task_failure),
        }

    def __getitem__(self, name):
        return self.collections[name]

    def drop_collection(self, *args, **kwargs):
        raise AssertionError("Imports must never drop existing collections")


class FakeClient:
    def __init__(self, database, ping_failure=False):
        self.database = database
        self.closed = False
        self.database_names = []
        self.ping_failure = ping_failure
        self.admin = self

    def command(self, name):
        if name != "ping":
            raise AssertionError(name)
        if self.ping_failure:
            raise RuntimeError("Injected connection failure")

    def __getitem__(self, name):
        self.database_names.append(name)
        return self.database

    def close(self):
        self.closed = True

    def drop_database(self, *args, **kwargs):
        raise AssertionError("Imports must never drop existing databases")


class ClientFactory:
    def __init__(self, database, ping_failure=False):
        self.database = database
        self.ping_failure = ping_failure
        self.clients = []
        self.arguments = []

    def __call__(self, uri, **options):
        self.arguments.append((uri, options))
        client = FakeClient(self.database, self.ping_failure)
        self.clients.append(client)
        return client


def task(model, task_id=1):
    return {"type": "task", "model_name": model, "task_id": task_id,
            "description": model + "_task.xml", "task_sequence": "<root />"}


def owned(document, key="scenario", generation="old"):
    """Represent old stored metadata without depending on writer internals."""
    return {**document, LEGACY_METADATA_FIELD: {"import_key": key, "generation": generation}}


class DatabaseTests(unittest.TestCase):
    def assert_no_inserts(self, database):
        self.assertEqual(database["parameter"].insert_calls, [])
        self.assertEqual(database["task"].insert_calls, [])

    def test_existing_scalar_array_global_and_other_model_records_are_preserved(self):
        documents = [
            owned({"_id": 1, "record_name": "same", "model_name": "zx200", "value": "old scalar"}),
            owned({"_id": 2, "record_name": "same", "model_name": ["zx200", "mst110cr"],
                   "value": "old shared"}),
            {"_id": 3, "record_name": "same", "value": "old global", "custom": {"keep": True}},
            {"_id": 4, "record_name": "same", "model_name": "mst110cr", "value": "other"},
        ]
        self.assertTrue(matches(documents[1], {"record_name": "same", "model_name": "zx200"}))
        self.assertFalse(matches(documents[1], parameter_filter(documents[0])))
        database = FakeDatabase(parameters=documents)
        factory = ClientFactory(database)
        compilation = Compilation([
            {"record_name": "same", "model_name": "zx200", "value": "new scalar"},
            {"record_name": "same", "model_name": ["zx200", "mst110cr"], "value": "new shared"},
            {"record_name": "same", "value": "new global"},
        ], [])
        original = deepcopy(compilation)
        result = write_database(compilation, client_factory=factory)
        self.assertEqual(database["parameter"].documents, documents)
        self.assert_no_inserts(database)
        self.assertEqual(result["parameter_count"], 3)
        self.assertEqual(result["inserted_parameters"], 0)
        self.assertEqual(result["skipped_parameters"], 3)
        self.assertEqual(compilation, original)
        self.assertTrue(factory.clients[0].closed)

    def test_missing_scalar_scope_does_not_match_existing_shared_array(self):
        shared = owned({"record_name": "same", "model_name": ["zx200", "mst110cr"], "value": "old"})
        database = FakeDatabase(parameters=[shared])
        compilation = Compilation([{"record_name": "same", "model_name": "zx200", "value": "new"}], [])
        result = write_database(compilation, client_factory=ClientFactory(database))
        self.assertEqual(database["parameter"].documents[0], shared)
        self.assertEqual(database["parameter"].documents[1]["model_name"], "zx200")
        self.assertEqual(result["inserted_parameters"], 1)
        self.assertEqual(result["skipped_parameters"], 0)

    def test_array_order_and_membership_are_part_of_exact_parameter_scope(self):
        old = {"record_name": "same", "model_name": ["zx200", "mst110cr"], "value": "old"}
        database = FakeDatabase(parameters=[old])
        compilation = Compilation([
            {"record_name": "same", "model_name": ["mst110cr", "zx200"], "value": "reversed"},
            {"record_name": "same", "model_name": ["zx200"], "value": "single"},
        ], [])
        result = write_database(compilation, client_factory=ClientFactory(database))
        self.assertEqual(database["parameter"].documents[0], old)
        self.assertEqual(len(database["parameter"].documents), 3)
        self.assertEqual(result["inserted_parameters"], 2)

    def test_absent_and_null_model_scopes_are_distinct(self):
        for existing, incoming in (
            ({"record_name": "same", "model_name": None, "value": "old"},
             {"record_name": "same", "value": "new"}),
            ({"record_name": "same", "value": "old"},
             {"record_name": "same", "model_name": None, "value": "new"}),
        ):
            with self.subTest(existing=existing):
                database = FakeDatabase(parameters=[existing])
                result = write_database(Compilation([incoming], []), client_factory=ClientFactory(database))
                self.assertEqual(database["parameter"].documents[0], existing)
                self.assertEqual(len(database["parameter"].documents), 2)
                self.assertEqual(result["inserted_parameters"], 1)

    def test_new_records_are_inserted_once_without_metadata(self):
        database = FakeDatabase()
        factory = ClientFactory(database)
        compilation = Compilation([
            {"record_name": "scalar", "model_name": "zx200", "value": 1},
            {"record_name": "shared", "model_name": ["zx200", "mst110cr"]},
            {"record_name": "global"},
        ], [task("zx200", 99), task("mst110cr", 100)])
        original = deepcopy(compilation)
        first = write_database(compilation, client_factory=factory)
        self.assertEqual(first, {
            "parameter_count": 3, "task_count": 2, "task_ids": {"zx200": 1, "mst110cr": 2},
            "inserted_parameters": 3, "skipped_parameters": 0, "inserted_tasks": 2, "skipped_tasks": 0,
        })
        written = deepcopy(database["parameter"].documents + database["task"].documents)
        for document in written:
            self.assertNotIn(LEGACY_METADATA_FIELD, document)
        self.assertEqual(database["parameter"].documents, compilation.parameters)
        self.assertEqual(database["task"].documents, [
            {**document, "task_id": index}
            for index, document in enumerate(compilation.tasks, 1)])
        second = write_database(compilation, client_factory=factory)
        self.assertEqual(second["task_ids"], first["task_ids"])
        self.assertEqual(second["inserted_parameters"], 0)
        self.assertEqual(second["skipped_parameters"], 3)
        self.assertEqual(second["inserted_tasks"], 0)
        self.assertEqual(second["skipped_tasks"], 2)
        self.assertEqual(database["parameter"].documents + database["task"].documents, written)
        self.assertEqual(len(database["parameter"].insert_calls), 3)
        self.assertEqual(len(database["task"].insert_calls), 2)
        self.assertEqual(compilation, original)
        self.assertTrue(all(client.closed for client in factory.clients))

    def test_changed_input_keeps_existing_parameter_and_task_content(self):
        old_parameter = owned({"record_name": "route", "model_name": "truck", "points": [[1, 2, 3]],
                               "runtime": {"progress": 2}})
        old_task = owned({**task("truck", 42), "task_sequence": "<old />", "enabled": False})
        database = FakeDatabase(parameters=[old_parameter], tasks=[old_task])
        result = write_database(Compilation([
            {"record_name": "route", "model_name": "truck", "points": [[9, 8, 7]]},
        ], [{**task("truck", 99), "task_sequence": "<new />"}]),
            client_factory=ClientFactory(database))
        self.assertEqual(database["parameter"].documents, [old_parameter])
        self.assertEqual(database["task"].documents, [old_task])
        self.assert_no_inserts(database)
        self.assertEqual(result["task_ids"], {"truck": 42})
        self.assertEqual(result["skipped_tasks"], 1)

    def test_stale_managed_and_manual_documents_remain_when_input_changes(self):
        old_parameters = [
            owned({"record_name": "old_area"}),
            owned({"record_name": "old_initial", "model_name": "zx200"}),
            owned({"record_name": "other_project"}, key="another"),
            {"record_name": "manual"},
        ]
        old_tasks = [owned(task("old_machine", 7)), owned(task("zx200", 9)),
                     owned(task("other_project", 10), key="another"), task("manual", 11)]
        database = FakeDatabase(parameters=old_parameters, tasks=old_tasks)
        factory = ClientFactory(database)
        result = write_database(Compilation([{"record_name": "current_area"}], [task("zx200")]),
                                client_factory=factory,
                                mongo_uri="mongodb://test:27018", mongo_db="existing_db", timeout_ms=1234)
        self.assertEqual(database["parameter"].documents[:len(old_parameters)], old_parameters)
        self.assertEqual(database["task"].documents, old_tasks)
        self.assertEqual(result["task_ids"], {"zx200": 9})
        self.assertEqual(result["inserted_parameters"], 1)
        self.assertEqual(result["skipped_tasks"], 1)
        self.assertNotIn("removed_parameters", result)
        self.assertNotIn("removed_tasks", result)
        self.assertEqual(factory.clients[0].database_names, ["existing_db"])
        self.assertEqual(factory.arguments, [("mongodb://test:27018", {
            "serverSelectionTimeoutMS": 1234, "connectTimeoutMS": 1234, "socketTimeoutMS": 1234,
        })])

    def test_existing_initialization_and_work_flags_retain_runtime_values(self):
        completed = [
            owned({"record_name": "initialize_flgs", "initialize_flg_zx200": True,
                   "initialize_flg_mst110cr": True, "extra_runtime_flag": True}),
            {"record_name": "task_flgs", "task_flg_done": True, "task_flg_next": False},
        ]
        database = FakeDatabase(parameters=completed)
        incoming = [
            {"record_name": "initialize_flgs", "initialize_flg_zx200": False,
             "initialize_flg_mst110cr": False},
            {"record_name": "task_flgs", "task_flg_done": False, "task_flg_next": False},
        ]
        result = write_database(Compilation(incoming, []), client_factory=ClientFactory(database))
        self.assertEqual(database["parameter"].documents, completed)
        self.assert_no_inserts(database)
        self.assertEqual(result["skipped_parameters"], 2)

    def test_task_ids_use_smallest_unused_positive_id_and_remain_stable(self):
        old_tasks = [task("unrelated", 1), task("zx200", 42), task("another", 3),
                     {"type": "other", "model_name": "other_type", "task_id": 2},
                     task("legacy", "invalid")]
        database = FakeDatabase(tasks=old_tasks)
        factory = ClientFactory(database)
        compilation = Compilation([], [task("zx200", 900), task("mst110cr", 999), task("blade", 1000)])
        original = deepcopy(compilation)
        first = write_database(compilation, client_factory=factory)
        self.assertEqual(first["task_ids"], {"zx200": 42, "mst110cr": 4, "blade": 5})
        self.assertEqual(first["inserted_tasks"], 2)
        self.assertEqual(first["skipped_tasks"], 1)
        self.assertEqual(database["task"].documents[:len(old_tasks)], old_tasks)
        second = write_database(Compilation([], list(reversed(compilation.tasks))), client_factory=factory)
        self.assertEqual(second["task_ids"], first["task_ids"])
        self.assertEqual(second["inserted_tasks"], 0)
        self.assertEqual(second["skipped_tasks"], 3)
        self.assertEqual(len(database["task"].documents), len(old_tasks) + 2)
        self.assertEqual(compilation, original)

    def test_other_task_type_with_same_model_does_not_prevent_new_task(self):
        old = {"type": "other", "model_name": "zx200", "task_id": 1, "value": "keep"}
        database = FakeDatabase(tasks=[old])
        result = write_database(Compilation([], [task("zx200")]), client_factory=ClientFactory(database))
        self.assertEqual(database["task"].documents[0], old)
        self.assertEqual(result["task_ids"], {"zx200": 2})
        self.assertEqual(result["inserted_tasks"], 1)

    def test_empty_compilation_preserves_all_documents(self):
        parameters = [owned({"record_name": "old"}), {"record_name": "manual"}]
        tasks = [owned(task("old", 8)), task("manual", 9)]
        database = FakeDatabase(parameters=parameters, tasks=tasks)
        result = write_database(Compilation([], []), client_factory=ClientFactory(database))
        self.assertEqual(database["parameter"].documents, parameters)
        self.assertEqual(database["task"].documents, tasks)
        self.assert_no_inserts(database)
        self.assertEqual(result, {
            "parameter_count": 0, "task_count": 0, "task_ids": {},
            "inserted_parameters": 0, "skipped_parameters": 0, "inserted_tasks": 0, "skipped_tasks": 0,
        })

    def test_parameter_insert_failure_preserves_existing_and_retry_adds_missing_only(self):
        old_parameters = [owned({"record_name": "stale"})]
        old_tasks = [owned(task("old", 8))]
        database = FakeDatabase(parameters=old_parameters, tasks=old_tasks, parameter_failure=2)
        factory = ClientFactory(database)
        compilation = Compilation([{"record_name": "first"}, {"record_name": "second"}], [task("new")])
        with self.assertRaisesRegex(RuntimeError, "insertion failure"):
            write_database(compilation, client_factory=factory)
        self.assertEqual(database["parameter"].documents[0], old_parameters[0])
        self.assertEqual(database["task"].documents, old_tasks)
        self.assertEqual([document["record_name"] for document in database["parameter"].documents],
                         ["stale", "first"])
        first_inserted = deepcopy(database["parameter"].documents[1])
        self.assertTrue(factory.clients[0].closed)
        database["parameter"].fail_insert_at = None
        result = write_database(compilation, client_factory=factory)
        self.assertEqual(result["inserted_parameters"], 1)
        self.assertEqual(result["skipped_parameters"], 1)
        self.assertEqual(database["parameter"].documents[:2], [old_parameters[0], first_inserted])
        self.assertEqual(database["task"].documents[0], old_tasks[0])
        self.assertEqual(result["task_ids"], {"new": 1})
        self.assertTrue(all(client.closed for client in factory.clients))

    def test_task_insert_failure_preserves_existing_and_retry_keeps_assigned_id(self):
        old_parameter = owned({"record_name": "stale"})
        old_task = owned(task("old", 8))
        database = FakeDatabase(parameters=[old_parameter], tasks=[old_task], task_failure=2)
        factory = ClientFactory(database)
        compilation = Compilation([{"record_name": "new_parameter"}], [task("zx200"), task("mst110cr")])
        with self.assertRaisesRegex(RuntimeError, "insertion failure"):
            write_database(compilation, client_factory=factory)
        self.assertEqual(database["parameter"].documents[0], old_parameter)
        self.assertEqual(database["task"].documents[0], old_task)
        inserted_task = deepcopy(database["task"].documents[1])
        inserted_parameter = deepcopy(database["parameter"].documents[1])
        self.assertTrue(factory.clients[0].closed)
        database["task"].fail_insert_at = None
        result = write_database(compilation, client_factory=factory)
        self.assertEqual(result["task_ids"], {"zx200": inserted_task["task_id"], "mst110cr": 2})
        self.assertEqual(result["inserted_tasks"], 1)
        self.assertEqual(result["skipped_tasks"], 1)
        self.assertEqual(result["inserted_parameters"], 0)
        self.assertEqual(result["skipped_parameters"], 1)
        self.assertEqual(database["parameter"].documents, [old_parameter, inserted_parameter])
        self.assertEqual(database["task"].documents[:2], [old_task, inserted_task])
        self.assertTrue(all(client.closed for client in factory.clients))

    def test_connection_failure_closes_client_without_mutation(self):
        old = {"record_name": "manual"}
        database = FakeDatabase(parameters=[old])
        factory = ClientFactory(database, ping_failure=True)
        with self.assertRaisesRegex(RuntimeError, "connection failure"):
            write_database(Compilation([{"record_name": "new"}], [task("new")]), client_factory=factory)
        self.assertEqual(database["parameter"].documents, [old])
        self.assert_no_inserts(database)
        self.assertTrue(factory.clients[0].closed)

    def test_invalid_parameter_compilation_is_rejected_before_connecting(self):
        factory = ClientFactory(FakeDatabase())
        with self.assertRaisesRegex(ValueError, "Conflicting parameter"):
            write_database(Compilation([{"record_name": "duplicate"}, {"record_name": "duplicate"}], []),
                           client_factory=factory)
        self.assertFalse(factory.clients)

    def test_duplicate_imported_models_are_rejected_before_connecting(self):
        database = FakeDatabase()
        factory = ClientFactory(database)
        with self.assertRaises(ValueError):
            write_database(Compilation([{"record_name": "should_not_be_written"}],
                                       [task("zx200", 1), task("zx200", 2)]), client_factory=factory)
        self.assert_no_inserts(database)
        self.assertFalse(factory.clients)

    def test_invalid_existing_matching_task_id_is_rejected_before_any_write(self):
        for invalid_id in (None, "invalid", 0, -1, True, 1.5):
            with self.subTest(task_id=invalid_id):
                old = owned(task("zx200", invalid_id))
                database = FakeDatabase(tasks=[old])
                factory = ClientFactory(database)
                with self.assertRaises(ValueError):
                    write_database(Compilation([{"record_name": "should_not_be_written"}], [task("zx200")]),
                                   client_factory=factory)
                self.assertEqual(database["task"].documents, [old])
                self.assert_no_inserts(database)
                self.assertTrue(factory.clients[0].closed)

    def test_duplicate_existing_matching_models_are_rejected_before_any_write(self):
        old_tasks = [task("zx200", 9), task("zx200", 10)]
        database = FakeDatabase(tasks=old_tasks)
        factory = ClientFactory(database)
        with self.assertRaises(ValueError):
            write_database(Compilation([{"record_name": "should_not_be_written"}], [task("zx200")]),
                           client_factory=factory)
        self.assertEqual(database["task"].documents, old_tasks)
        self.assert_no_inserts(database)
        self.assertTrue(factory.clients[0].closed)

    def test_duplicate_existing_positive_task_ids_are_rejected_before_any_write(self):
        old_tasks = [task("zx200", 9), task("mst110cr", 9)]
        database = FakeDatabase(tasks=old_tasks)
        factory = ClientFactory(database)
        with self.assertRaises(ValueError):
            write_database(Compilation([{"record_name": "should_not_be_written"}], [task("zx200")]),
                           client_factory=factory)
        self.assertEqual(database["task"].documents, old_tasks)
        self.assert_no_inserts(database)
        self.assertTrue(factory.clients[0].closed)


if __name__ == "__main__":
    unittest.main()
