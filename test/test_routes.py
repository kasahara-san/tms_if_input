"""Transport topology checks, including terminal leveling-column fanout."""
from pathlib import Path
import unittest

from tms_if_input.model import GeoGraph, Plan, Point, Section, Task, load_inputs
from tms_if_input.routes import build_route_documents, leveling_entry_node


ROOT = Path(__file__).resolve().parents[1]


def branch_scenario(columns=2, extended=False):
    machines = {"excavator": "custom_excavator", "dump": "custom_dump",
                "bulldozer": "custom_bulldozer"}
    tasks = {
        "exc": Task("exc", "excavation_loading", {"machine": "excavator", "dump_node": ["load"]}),
        "level": Task("level", "leveling", {"machine": "bulldozer",
                                            "connection_node": [f"entry-{i}" for i in range(columns)]}),
        "haul": Task("haul", "transport", {"machine": "dump",
                                             "excavation_loading_task": "exc", "leveling_task": "level"}),
    }
    plan = Plan(machines, tasks, ())
    points = {key: Point(key, x, y, 0) for key, x, y in
              (("load", 0, 0), ("approach", 0, 1), ("hub", 0, 2))}
    sections = [Section("approach-road", "approach", "load", ((0, 1), (0, .5), (0, 0))),
                Section("trunk", "approach", "hub", ((0, 1), (0, 2)))]
    for i in range(columns):
        entry = f"entry-{i}"
        points[entry] = Point(entry, i + 1, 4, 0)
        if extended:
            middle = f"middle-{i}"
            points[middle] = Point(middle, i + 1, 3, 0)
            sections.append(Section(f"branch-{i}", "hub", middle, ((0, 2), (i + 1, 3))))
            sections.append(Section(f"connector-{i}", middle, entry, ((i + 1, 3), (i + 1, 4))))
        else:
            sections.append(Section(f"connector-{i}", "hub", entry, ((0, 2), (i + 1, 4))))
    return plan, GeoGraph(points, tuple(sections), ())


def disconnected_scenarios():
    plan, graph = branch_scenario(1)
    tasks = dict(plan.tasks)
    points = dict(graph.points)
    sections = list(graph.sections)
    for point in graph.points.values():
        points["second-" + point.id] = Point("second-" + point.id,
                                             point.x + 100, point.y + 100, point.route_rank)
    for section in graph.sections:
        sections.append(Section("second-" + section.id, "second-" + section.start,
                                "second-" + section.end,
                                tuple((x + 100, y + 100) for x, y in section.coordinates)))
    tasks["second-exc"] = Task("second-exc", "excavation_loading",
                               {"machine": "excavator", "dump_node": ["second-load"]})
    tasks["second-level"] = Task("second-level", "leveling",
                                 {"machine": "bulldozer", "connection_node": ["second-entry-0"]})
    tasks["second-haul"] = Task("second-haul", "transport", {"machine": "dump",
                                                               "excavation_loading_task": "second-exc",
                                                               "leveling_task": "second-level"})
    return Plan(plan.machines, tasks, ()), GeoGraph(points, tuple(sections), ())


class TransportRouteTests(unittest.TestCase):
    def test_sample_leveling_entry_is_the_first_matching_xml_connection_node(self):
        plan, graph = load_inputs(ROOT / "json_samples/261001-kyoto.geojson",
                                  ROOT / "json_samples/261001-kyoto.xml")
        leveling = next(task for task in plan.tasks.values() if task.name == "leveling")
        self.assertEqual(leveling_entry_node(plan, graph, leveling), "919561426")

    def test_leveling_entry_uses_xml_order_when_multiple_endpoints_match(self):
        plan, graph = branch_scenario(3)
        level = Task("level", "leveling", {"machine": "bulldozer",
                                            "connection_node": ["entry-2", "entry-0", "entry-1"]})
        tasks = {**plan.tasks, "level": level}
        plan = Plan(plan.machines, tasks, ())
        self.assertEqual(leveling_entry_node(plan, graph, level), "entry-2")

    def test_leveling_entry_uses_only_the_candidate_matching_an_up_endpoint(self):
        plan, graph = branch_scenario(1)
        level = Task("level", "leveling", {"machine": "bulldozer",
                                            "connection_node": ["load", "entry-0"]})
        plan = Plan(plan.machines, {**plan.tasks, "level": level}, ())
        # The loading root is an input connection candidate but starts every
        # outgoing section, so the only valid up-direction endpoint is entry-0.
        self.assertEqual(leveling_entry_node(plan, graph, level), "entry-0")

    def test_leveling_entry_does_not_require_a_terminal_of_the_entire_network(self):
        plan, graph = branch_scenario(1)
        level = Task("level", "leveling", {"machine": "bulldozer",
                                            "connection_node": ["hub", "entry-0"]})
        plan = Plan(plan.machines, {**plan.tasks, "level": level}, ())
        points = {**graph.points, "bridge": Point("bridge", .5, 3, 0)}
        sections = (*graph.sections[:-1],
                    Section("connector-in", "hub", "bridge", ((0, 2), (.5, 3))),
                    Section("connector-out", "bridge", "entry-0", ((.5, 3), (1, 4))))
        graph = GeoGraph(points, sections, ())
        self.assertEqual(leveling_entry_node(plan, graph, level), "hub")

    def test_leveling_entry_is_independent_of_input_section_direction_and_inner_vertices(self):
        plan, graph = branch_scenario(1)
        sections = tuple(Section(section.id, section.end, section.start,
                                 tuple(reversed(section.coordinates)))
                         for section in graph.sections)
        # This interior coordinate coincides with another Point and must never
        # become an endpoint candidate merely because its coordinates match.
        sections = (*sections[:-1],
                    Section("connector-0", "entry-0", "hub", ((1, 4), (0, 0), (0, 2))))
        graph = GeoGraph(graph.points, sections, ())
        level = Task("level", "leveling", {"machine": "bulldozer",
                                            "connection_node": ["load", "entry-0"]})
        plan = Plan(plan.machines, {**plan.tasks, "level": level}, ())
        self.assertEqual(leveling_entry_node(plan, graph, level), "entry-0")

    def test_leveling_entries_stay_in_their_own_work_areas(self):
        plan, graph = disconnected_scenarios()
        self.assertEqual(leveling_entry_node(plan, graph, plan.tasks["level"]), "entry-0")
        self.assertEqual(leveling_entry_node(plan, graph, plan.tasks["second-level"]), "second-entry-0")

    def test_leveling_entry_has_no_fallback_when_candidates_do_not_end_a_road(self):
        plan, graph = branch_scenario(1)
        level = Task("unconnected-level", "leveling", {"machine": "bulldozer",
                                                        "connection_node": ["load"]})
        plan = Plan(plan.machines, {**plan.tasks, level.id: level}, ())
        with self.assertRaisesRegex(ValueError, "Task unconnected-level: no connection_node.*endpoint"):
            leveling_entry_node(plan, graph, level)

    def test_leveling_entry_without_transport_is_an_explicit_input_error(self):
        plan, graph = branch_scenario(1)
        tasks = {identifier: task for identifier, task in plan.tasks.items() if task.name != "transport"}
        plan = Plan(plan.machines, tasks, ())
        with self.assertRaisesRegex(ValueError, "Task level: leveling entry requires a transport road"):
            leveling_entry_node(plan, graph, tasks["level"])

    def test_all_sample_main_and_passing_sections_are_saved(self):
        plan, graph = load_inputs(ROOT / "json_samples/261001-kyoto.geojson",
                                  ROOT / "json_samples/261001-kyoto.xml")
        documents = build_route_documents(plan, graph)
        by_id = {document["section_id"]: document for document in documents}
        self.assertEqual(len(by_id), 18)
        self.assertEqual(sum(document["label"] == "main" for document in documents), 12)
        self.assertEqual(sum(document["label"] == "sub" for document in documents), 6)
        expected_fields = {"model_name", "type", "x", "y", "z", "qx", "qy", "qz", "qw",
                           "record_name", "section_id", "label", "preferred_direction",
                           "related_point_up_main", "related_point_up_sub",
                           "related_point_down_main", "related_point_down_sub"}
        for document in documents:
            self.assertEqual(set(document), expected_fields)
            self.assertEqual(document["record_name"], document["section_id"])
            self.assertEqual(document["model_name"], ["mst110cr", "mst2200vdr"])
            self.assertEqual(document["type"], "static")
            self.assertEqual(document["preferred_direction"], "up")
            count = len(document["x"])
            self.assertEqual(len(document["y"]), count)
            for key in ("z", "qx", "qy", "qz"):
                self.assertEqual(document[key], [0.0] * count)
            self.assertEqual(document["qw"], [1.0] * count)
            for key in ("related_point_up_main", "related_point_up_sub",
                        "related_point_down_main", "related_point_down_sub"):
                self.assertTrue(document[key] == "" or document[key] in by_id)

        # Both passing lanes are oriented up despite differing input edge orders.
        self.assertEqual(by_id["3260195895"]["related_point_up_sub"], "768363352")
        self.assertEqual(by_id["768363352"]["related_point_down_main"], "3260195895")
        self.assertEqual(by_id["3851915284"]["related_point_down_sub"], "768363352")
        self.assertEqual(by_id["1769298385"]["related_point_up_main"], "3775605330")
        self.assertEqual(by_id["1285700315"]["related_point_up_sub"], "2192002421")
        self.assertEqual(by_id["2192002421"]["related_point_up_sub"], "1547089919")
        self.assertEqual(by_id["2758184951"]["related_point_up_main"], "1901058875")
        for identifier in ("840315598", "3159144693", "936963196"):
            self.assertEqual(by_id[identifier]["related_point_down_main"], "1901058875")
            self.assertEqual(by_id[identifier]["related_point_up_main"], "")
        self.assertEqual(by_id["1901058875"]["related_point_up_main"], "")

    def test_terminal_fanout_keeps_every_branch_with_singular_back_link(self):
        for columns in (2, 4, 7):
            with self.subTest(columns=columns):
                plan, graph = branch_scenario(columns)
                by_id = {document["section_id"]: document
                         for document in build_route_documents(plan, graph)}
                self.assertEqual(len(by_id), columns + 2)
                self.assertEqual(by_id["trunk"]["related_point_up_main"], "")
                for i in range(columns):
                    branch = by_id[f"connector-{i}"]
                    self.assertEqual(branch["related_point_down_main"], "trunk")
                    self.assertEqual(branch["related_point_up_main"], "")
                    self.assertEqual(branch["x"], [0, i + 1])
                    self.assertEqual(branch["y"], [2, 4])

    def test_single_terminal_connector_is_linked_forward(self):
        plan, graph = branch_scenario(1)
        by_id = {document["section_id"]: document
                 for document in build_route_documents(plan, graph)}
        self.assertEqual(by_id["trunk"]["related_point_up_main"], "connector-0")
        self.assertEqual(by_id["connector-0"]["related_point_down_main"], "trunk")

    def test_nonterminal_fanout_is_rejected_instead_of_dropping_routes(self):
        plan, graph = branch_scenario(2, extended=True)
        with self.assertRaisesRegex(ValueError, "multiple up main links"):
            build_route_documents(plan, graph)

    def test_reversed_input_retains_every_linestring_vertex(self):
        plan, graph = branch_scenario(1)
        document = next(document for document in build_route_documents(plan, graph)
                        if document["section_id"] == "approach-road")
        self.assertEqual(document["x"], [0, 0, 0])
        self.assertEqual(document["y"], [0, .5, 1])
        self.assertEqual(document["qw"], [1.0, 1.0, 1.0])

    def test_reverse_endpoint_duplicate_is_removed(self):
        plan, graph = branch_scenario(1)
        first = graph.sections[0]
        duplicate = Section("opposite-road", first.end, first.start,
                            tuple(reversed(first.coordinates)))
        graph = GeoGraph(graph.points, (*graph.sections, duplicate), ())
        documents = build_route_documents(plan, graph)
        self.assertEqual(len(documents), 3)
        self.assertNotIn("opposite-road", {document["section_id"] for document in documents})

    def test_same_direction_parallel_sections_are_rejected(self):
        plan, graph = branch_scenario(1)
        first = graph.sections[0]
        duplicate = Section("parallel-road", first.start, first.end, first.coordinates)
        graph = GeoGraph(graph.points, (*graph.sections, duplicate), ())
        with self.assertRaisesRegex(ValueError, "terminal|parallel"):
            build_route_documents(plan, graph)

    def test_each_transport_must_reach_its_referenced_leveling_area(self):
        plan, graph = disconnected_scenarios()
        tasks = dict(plan.tasks)
        tasks["haul"] = Task("haul", "transport", {"machine": "dump",
                                                     "excavation_loading_task": "exc",
                                                     "leveling_task": "second-level"})
        tasks["second-haul"] = Task("second-haul", "transport", {"machine": "dump",
                                                                   "excavation_loading_task": "second-exc",
                                                                   "leveling_task": "level"})
        plan = Plan(plan.machines, tasks, ())
        with self.assertRaisesRegex(ValueError, "Task haul: no transport path"):
            build_route_documents(plan, graph)

    def test_passing_lane_cannot_bridge_separate_main_roads(self):
        plan, graph = disconnected_scenarios()
        points = dict(graph.points)
        points["passing"] = Point("passing", 50, 50, 1)
        sections = (*graph.sections,
                    Section("passing-in", "approach", "passing", ((0, 1), (50, 50))),
                    Section("passing-out", "passing", "second-hub", ((50, 50), (100, 102))))
        graph = GeoGraph(points, sections, ())
        with self.assertRaisesRegex(ValueError, "within one connected main road"):
            build_route_documents(plan, graph)


if __name__ == "__main__":
    unittest.main()
