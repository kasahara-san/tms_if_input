"""Behavioral checks for flow synchronization and replaceable XML inputs."""

from collections import Counter
import unittest
import xml.etree.ElementTree as ET

from tms_if_input.behavior_tree import (
    build_task_documents, completion_flag, context_record_name, synchronization_documents,
)
from tms_if_input.model import Plan, Task, record_name


def sample_plan():
    machines = {"hoe_alias": "zx200_9", "truck_alias": "mst110cr_4", "blade_alias": "d37pxi_2"}
    entries = [
        Task("5000000000", "start", {}),
        Task("5000000001", "initialize", {"machine": "hoe_alias", "swing": 1.5}),
        Task("5000000002", "initialize", {"machine": "truck_alias", "swing": 2.5, "vessel": 0.0}),
        Task("5000000003", "initialize", {"machine": "blade_alias"}),
        Task("5000000004", "excavation_loading", {"machine": "hoe_alias"}),
        Task("5000000005", "transport", {"machine": "truck_alias"}),
        Task("5000000006", "leveling", {"machine": "blade_alias"}),
        Task("5000000007", "end", {}),
    ]
    edges = [(entries[0].id, entry.id) for entry in entries[1:4]]
    edges.extend((first.id, second.id) for first in entries[1:4] for second in entries[4:7])
    edges.extend((entry.id, entries[-1].id) for entry in entries[4:7])
    return Plan(machines, {entry.id: entry for entry in entries}, tuple(edges))


class TreeRuntime:
    """Small tick harness matching the emitted nodes' observable semantics.

    In particular, the repository's KeepRunningUntilFlgup only succeeds when
    its child succeeds and its local blackboard key contains boolean true.
    Sequence retains its running child, as BehaviorTree.CPP does.
    """

    def __init__(self, document, database, work_log):
        root = ET.fromstring(document["task_sequence"])
        self.tree = root.find("BehaviorTree/Sequence")
        self.database = database
        self.work_log = work_log
        self.blackboard = {}
        self.positions = {}
        self.actions = []
        self.done = False

    def tick(self):
        if not self.done:
            self.done = self._tick(self.tree)
        return self.done

    def _tick(self, node):
        children = list(node)
        if node.tag == "Sequence":
            index = self.positions.get(id(node), 0)
            while index < len(children):
                if not self._tick(children[index]):
                    self.positions[id(node)] = index
                    return False
                index += 1
            self.positions.pop(id(node), None)
            return True
        if node.tag == "Decorator":
            child_success = self._tick(children[0])
            value = self.blackboard.get(node.attrib["key"])
            return child_success and type(value) is bool and value
        if node.tag == "IfThenElse":
            condition = children[0].attrib["conditional_expression"]
            keys = [part.removesuffix(" == true") for part in condition.split(" and ")]
            outcome = all(self.blackboard.get(key) is True for key in keys)
            return self._tick(children[1 if outcome else 2])
        if node.tag == "AlwaysSuccess":
            return True
        attributes = node.attrib
        node_id = attributes["ID"]
        if node_id == "SetLocalBlackboard":
            self.blackboard[attributes["output_key"]] = attributes["value"] == "true"
        elif node_id == "MongoValueReader":
            self.blackboard[attributes["output_port"]] = self.database[
                attributes["mongo_record_name"]][attributes["mongo_param_name"]]
        elif node_id == "MongoValueWriter":
            if attributes["input_type"] != "bool":
                raise AssertionError("Completion flags must be booleans")
            self.database[attributes["mongo_record_name"]][attributes["mongo_param_name"]] = (
                attributes["input_value"] == "true")
        elif node_id.startswith("LeafNode"):
            primitive = attributes["primitive_name"]
            self.actions.append(primitive)
            if primitive in {"excavation_loading", "transport", "leveling"}:
                flags = self.database["initialize_flgs"]
                if not all(value is True for key, value in flags.items() if key.startswith("initialize_flg_")):
                    raise AssertionError("Work started before every initialization finished")
                self.work_log.append((attributes["model_name"], primitive))
        else:
            raise AssertionError(f"Unexpected runtime node: {node_id}")
        return True


class BehaviorTreeTests(unittest.TestCase):
    def test_replacement_models_generate_correct_machine_interfaces(self):
        plan = sample_plan()
        documents = build_task_documents(plan)
        self.assertEqual([document["task_id"] for document in documents], [1, 2, 3])
        self.assertEqual([document["model_name"] for document in documents], list(plan.machines.values()))
        expected = ["LeafNodeExcavator", "LeafNodeCrawlerDump", "LeafNodeBulldozer"]
        for document, node_id in zip(documents, expected):
            root = ET.fromstring(document["task_sequence"])
            actions = root.findall("BehaviorTree//Action")
            leaves = [action for action in actions if action.attrib["ID"].startswith("LeafNode")]
            self.assertTrue(leaves)
            self.assertTrue(all(leaf.attrib["ID"] == node_id for leaf in leaves))
            self.assertTrue(all(leaf.attrib["model_name"] == document["model_name"] for leaf in leaves))
            self.assertEqual(document["description"], document["model_name"] + "_task.xml")
            declarations = {
                declaration.attrib["ID"]: {port.attrib["name"] for port in declaration}
                for declaration in root.find("TreeNodesModel")
            }
            for action in root.findall("BehaviorTree//Action") + root.findall("BehaviorTree//Decorator"):
                self.assertLessEqual(set(action.attrib) - {"ID"}, declarations[action.attrib["ID"]])
            for decorator in root.findall("BehaviorTree//Decorator"):
                self.assertTrue(decorator.attrib["key"])
                self.assertFalse(decorator.findall(".//Action[@ID='" + node_id + "']"))

    def test_all_machines_cross_barrier_and_work_runs_once(self):
        plan = sample_plan()
        database = {document["record_name"]: document.copy() for document in synchronization_documents(plan)}
        work_log = []
        runtimes = [TreeRuntime(document, database, work_log) for document in build_task_documents(plan)]
        for _ in range(6):
            for runtime in reversed(runtimes):
                runtime.tick()
        self.assertTrue(all(runtime.done for runtime in runtimes))
        self.assertEqual(Counter(primitive for _, primitive in work_log), {
            "excavation_loading": 1, "transport": 1, "leveling": 1,
        })
        self.assertTrue(all(value is True for key, value in database["initialize_flgs"].items()
                            if key.startswith("initialize_flg_")))

    def test_inter_machine_work_dependency_is_preserved(self):
        original = sample_plan()
        plan = Plan(original.machines, original.tasks,
                    original.edges + (("5000000004", "5000000005"),))
        database = {document["record_name"]: document.copy() for document in synchronization_documents(plan)}
        self.assertIs(database["task_completion_flgs"]["completed_flg_5000000004"], False)
        work_log = []
        runtimes = [TreeRuntime(document, database, work_log) for document in build_task_documents(plan)]
        for _ in range(6):
            for runtime in reversed(runtimes):
                runtime.tick()
        self.assertTrue(all(runtime.done for runtime in runtimes))
        primitives = [primitive for _, primitive in work_log]
        self.assertLess(primitives.index("excavation_loading"), primitives.index("transport"))
        self.assertEqual(primitives.count("excavation_loading"), 1)
        self.assertEqual(primitives.count("transport"), 1)

    def test_repeated_initialize_and_work_use_distinct_records(self):
        tasks = [
            Task("start-new", "start", {}),
            Task("first-init", "initialize", {"machine": "hoe", "swing": 0.0}),
            Task("first-area", "excavation_loading", {"machine": "hoe"}),
            Task("second-init", "initialize", {"machine": "hoe", "swing": 1.0}),
            Task("second-area", "excavation_loading", {"machine": "hoe"}),
            Task("end-new", "end", {}),
        ]
        plan = Plan({"hoe": "zx200_44"}, {task.id: task for task in tasks},
                    tuple((first.id, second.id) for first, second in zip(tasks, tasks[1:])))
        document = build_task_documents(plan)[0]
        root = ET.fromstring(document["task_sequence"])
        follow_records = [action.attrib["target_record_name"] for action in root.findall("BehaviorTree//Action")
                          if action.attrib.get("primitive_name") == "subtask_excavator_follow_waypoints"]
        self.assertEqual(follow_records, ["initial_position_first-init", "initial_position_second-init"])
        work_records = [action.attrib["target_record_name"] for action in root.findall("BehaviorTree//Action")
                        if action.attrib.get("primitive_name") == "excavation_loading"]
        self.assertEqual(work_records, ["excavation_loading_params_first-area", "excavation_loading_params_second-area"])
        flags = {document["record_name"]: document for document in synchronization_documents(plan)}
        self.assertIs(flags["initialize_flgs"]["initialize_flg_zx200_44_first-init"], False)
        self.assertIs(flags["initialize_flgs"]["initialize_flg_zx200_44_second-init"], False)
        self.assertIs(flags["task_completion_flgs"]["completed_flg_first-area"], False)

    def test_context_records_are_only_added_for_multiple_work_areas(self):
        original = sample_plan()
        self.assertEqual(context_record_name(original, original.tasks["5000000005"]), "")
        self.assertEqual(context_record_name(original, original.tasks["5000000006"]), "")
        extra = Task("area-two", "leveling", {"machine": "blade_alias"})
        plan = Plan(original.machines, {**original.tasks, extra.id: extra}, original.edges)
        self.assertEqual(context_record_name(plan, plan.tasks["5000000005"]), "transport_params")
        self.assertEqual(context_record_name(plan, extra), record_name(plan, extra, "leveling_params"))

    def test_mongo_unsafe_names_are_encoded_without_literal_name_collisions(self):
        first = Task("init.$one", "initialize", {"machine": "first"})
        other = Task("work.$one", "transport", {"machine": "first"})
        plan = Plan({"first": ".foo$bar"}, {first.id: first, other.id: other}, ())
        initial_field = completion_flag(plan, first)[1]
        work_field = completion_flag(plan, other)[1]
        for field_name in (initial_field, work_field):
            self.assertFalse(any(character in field_name for character in ".$\0"))
        literal_model = initial_field.removeprefix("initialize_flg_")
        literal_task_id = work_field.removeprefix("completed_flg_")
        second = Task("safe-init", "initialize", {"machine": "second"})
        second_work = Task(literal_task_id, "transport", {"machine": "second"})
        other_plan = Plan({"second": literal_model},
                          {second.id: second, second_work.id: second_work}, ())
        self.assertNotEqual(completion_flag(other_plan, second)[1], initial_field)
        self.assertNotEqual(completion_flag(other_plan, second_work)[1], work_field)

    def test_repeated_initialization_cannot_collide_with_instance_model_name(self):
        tasks = [
            Task("1", "initialize", {"machine": "base"}),
            Task("2", "initialize", {"machine": "base"}),
            Task("3", "initialize", {"machine": "instance"}),
        ]
        plan = Plan({"base": "zx200", "instance": "zx200_1"},
                    {task.id: task for task in tasks}, ())
        fields = [completion_flag(plan, task)[1] for task in tasks]
        self.assertEqual(len(fields), len(set(fields)))
        ordinary = sample_plan()
        self.assertEqual(completion_flag(ordinary, ordinary.tasks["5000000001"])[1],
                         "initialize_flg_zx200_9")


if __name__ == "__main__":
    unittest.main()
