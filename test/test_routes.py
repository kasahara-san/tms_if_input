"""Transport topology checks, including terminal leveling-column fanout."""
import json
from pathlib import Path
import tempfile
import unittest

from tms_if_input.model import GeoGraph, Plan, Point, Section, Task, load_inputs
from tms_if_input.routes import build_route_documents, leveling_entry_node, transport_section_ids


ROOT = Path(__file__).resolve().parents[1]


def branch_scenario(columns=2, extended=False):
    machines = {"excavator": "custom_excavator", "dump": "custom_dump",
                "bulldozer": "custom_bulldozer"}
    tasks = {
        "exc": Task("exc", "excavation_loading", {"machine": "excavator", "dump_node": ["load"]}),
        "level": Task("level", "leveling", {"machine": "bulldozer",
                                            "connection_node": [f"entry-{i}" for i in range(columns)]}),
        "haul": Task("haul", "transport", {"machine": "dump",
                                             "excavation_loading_task": "exc", "leveling_task": "level",
                                             "main_node": ["approach", "hub"] +
                                                          ([f"middle-{i}" for i in range(columns)] if extended else []),
                                             "sub_node": []}),
    }
    plan = Plan(machines, tasks, ())
    points = {key: Point(key, x, y) for key, x, y in
              (("load", 0, 0), ("approach", 0, 1), ("hub", 0, 2))}
    sections = [Section("approach-road", "approach", "load", ((0, 1), (0, .5), (0, 0))),
                Section("trunk", "approach", "hub", ((0, 1), (0, 2)))]
    for i in range(columns):
        entry = f"entry-{i}"
        points[entry] = Point(entry, i + 1, 4)
        if extended:
            middle = f"middle-{i}"
            points[middle] = Point(middle, i + 1, 3)
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
                                             point.x + 100, point.y + 100)
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
                                                               "leveling_task": "second-level",
                                                               "main_node": ["second-approach", "second-hub"],
                                                               "sub_node": []})
    return Plan(plan.machines, tasks, ()), GeoGraph(points, tuple(sections), ())


def passing_scenario(lanes=((0, 1, ("pass-A", "pass-B")),), main_count=4):
    """Make roads with explicit XML order and no route-rank inference."""
    plan, _ = branch_scenario(1)
    main_nodes = [f"road-{index}" for index in range(main_count)]
    sub_nodes = [node for _, _, nodes in lanes for node in nodes]
    haul = Task("haul", "transport", {**plan.tasks["haul"].parameters,
                                      "main_node": main_nodes, "sub_node": sub_nodes})
    plan = Plan(plan.machines, {**plan.tasks, "haul": haul}, ())
    points = {"load": Point("load", -10, 0), "entry-0": Point("entry-0", main_count * 10, 0)}
    points.update({node: Point(node, index * 10, 0) for index, node in enumerate(main_nodes)})
    sections = []

    def road(identifier, start, end):
        a, b = points[start], points[end]
        coordinates = ((a.x, a.y), ((a.x + b.x) / 2, (a.y + b.y) / 2 + .5), (b.x, b.y))
        # Half the representative LineStrings face the opposite way, and
        # both source directions are present for every explicitly listed road.
        if len(sections) // 2 % 2:
            start, end, coordinates = end, start, tuple(reversed(coordinates))
        sections.append(Section(identifier, start, end, coordinates))
        sections.append(Section("reverse-" + identifier, end, start, tuple(reversed(coordinates))))

    chain = ["load"] + main_nodes + ["entry-0"]
    for index, (start, end) in enumerate(zip(chain, chain[1:])):
        road(f"main-{index}", start, end)
    for lane_index, (low, high, nodes) in enumerate(lanes):
        for index, node in enumerate(nodes):
            points[node] = Point(node, (low + (high - low) * (index + 1) / (len(nodes) + 1)) * 10,
                                 3 + lane_index * 2)
        chain = [main_nodes[low]] + list(nodes) + [main_nodes[high]]
        for index, (start, end) in enumerate(zip(chain, chain[1:])):
            road(f"lane-{lane_index}-{index}", start, end)
    return plan, GeoGraph(points, tuple(sections), ())


class TransportRouteTests(unittest.TestCase):
    def test_sample_leveling_entry_is_the_first_matching_xml_connection_node(self):
        plan, graph = load_inputs(ROOT / "json_samples/261004-kyoto.geojson",
                                  ROOT / "json_samples/261004-kyoto.xml")
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
        # The loading boundary starts an up-direction section and cannot
        # become an entrance just because it appears first in connection_node.
        self.assertEqual(leveling_entry_node(plan, graph, level), "entry-0")

    def test_leveling_entry_does_not_require_a_terminal_of_the_entire_network(self):
        plan, graph = branch_scenario(1)
        level = Task("level", "leveling", {"machine": "bulldozer",
                                            "connection_node": ["hub", "entry-0"]})
        plan = Plan(plan.machines, {**plan.tasks, "level": level}, ())
        haul = Task("haul", "transport", {**plan.tasks["haul"].parameters,
                                          "main_node": ["approach", "hub", "bridge"]})
        plan = Plan(plan.machines, {**plan.tasks, "haul": haul}, ())
        points = {**graph.points, "bridge": Point("bridge", .5, 3)}
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
                                            "connection_node": ["entry-0"]})
        plan = Plan(plan.machines, {**plan.tasks, "level": level}, ())
        self.assertEqual(leveling_entry_node(plan, graph, level), "entry-0")

    def test_leveling_entries_stay_in_their_own_work_areas(self):
        plan, graph = disconnected_scenarios()
        self.assertEqual(leveling_entry_node(plan, graph, plan.tasks["level"]), "entry-0")
        self.assertEqual(leveling_entry_node(plan, graph, plan.tasks["second-level"]), "second-entry-0")

    def test_leveling_entry_has_no_fallback_when_candidates_do_not_end_a_road(self):
        plan, graph = branch_scenario(1)
        level = Task("unconnected-level", "leveling", {"machine": "bulldozer",
                                                        "connection_node": ["approach"]})
        plan = Plan(plan.machines, {**plan.tasks, level.id: level}, ())
        # Other tasks' transport endpoints cannot supply this task's entrance.
        with self.assertRaises(ValueError):
            leveling_entry_node(plan, graph, level)

    def test_leveling_entry_without_transport_is_an_explicit_input_error(self):
        plan, graph = branch_scenario(1)
        tasks = {identifier: task for identifier, task in plan.tasks.items() if task.name != "transport"}
        plan = Plan(plan.machines, tasks, ())
        with self.assertRaises(ValueError):
            leveling_entry_node(plan, graph, tasks["level"])

    def test_all_sample_main_and_passing_sections_are_saved(self):
        plan, graph = load_inputs(ROOT / "json_samples/261004-kyoto.geojson",
                                  ROOT / "json_samples/261004-kyoto.xml")
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

        # These expected directions and links are read from the input/XML
        # chains, independently of the implementation's direction analysis.
        expected = {
            "2206133296": ("1924610930", "7609442", "3260195895", "", "", ""),
            "3260195895": ("7609442", "1755144311", "3453947199", "768363352", "2206133296", ""),
            "3453947199": ("1755144311", "365138918", "3775605330", "1769298385", "3260195895", "768363352"),
            "3775605330": ("365138918", "2943157174", "2109419383", "", "3453947199", "1769298385"),
            "2109419383": ("2943157174", "95297176", "944236149", "", "3775605330", ""),
            "944236149": ("95297176", "3887281787", "1285700315", "", "2109419383", ""),
            "1285700315": ("3887281787", "1327926290", "3483617579", "2192002421", "944236149", ""),
            "3483617579": ("1327926290", "2357889456", "1901058875", "2758184951", "1285700315", "2192002421"),
            "1901058875": ("2357889456", "758911660", "", "", "3483617579", "2758184951"),
            "840315598": ("758911660", "919561426", "", "", "1901058875", ""),
            "3159144693": ("758911660", "1880652658", "", "", "1901058875", ""),
            "936963196": ("758911660", "575106410", "", "", "1901058875", ""),
            "768363352": ("1755144311", "2657600551", "", "3851915284", "3260195895", ""),
            "3851915284": ("2657600551", "4025910820", "", "1769298385", "", "768363352"),
            "1769298385": ("4025910820", "365138918", "3775605330", "", "", "3851915284"),
            "2192002421": ("1327926290", "3813604511", "", "1547089919", "1285700315", ""),
            "1547089919": ("3813604511", "2995202696", "", "2758184951", "", "2192002421"),
            "2758184951": ("2995202696", "2357889456", "1901058875", "", "", "1547089919"),
        }
        source = json.loads((ROOT / "json_samples/261004-kyoto.geojson").read_text())
        lines = {str(feature["properties"]["id"]): feature for feature in source["features"]
                 if feature["geometry"]["type"] == "LineString"}
        fields = ("related_point_up_main", "related_point_up_sub",
                  "related_point_down_main", "related_point_down_sub")
        self.assertEqual(set(by_id), set(expected))
        for section_id, (start, end, *links) in expected.items():
            with self.subTest(section_id=section_id):
                document = by_id[section_id]
                feature = lines[section_id]
                coordinates = feature["geometry"]["coordinates"]
                if str(feature["properties"]["startid"]) != start:
                    coordinates = list(reversed(coordinates))
                self.assertEqual(document["x"], [point[0] for point in coordinates])
                self.assertEqual(document["y"], [point[1] for point in coordinates])
                self.assertEqual([document[field] for field in fields], links)
                for key in links:
                    if key:
                        self.assertTrue({start, end} & set(expected[key][:2]))
        for transport in (task for task in plan.tasks.values() if task.name == "transport"):
            self.assertEqual(set(transport_section_ids(plan, graph, transport)), set(expected))

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
        with self.assertRaises(ValueError):
            build_route_documents(plan, graph)

    def test_each_transport_must_reach_its_referenced_leveling_area(self):
        plan, graph = disconnected_scenarios()
        tasks = dict(plan.tasks)
        tasks["haul"] = Task("haul", "transport", {"machine": "dump",
                                                     "excavation_loading_task": "exc",
                                                     "leveling_task": "second-level",
                                                     "main_node": ["approach", "hub"], "sub_node": []})
        tasks["second-haul"] = Task("second-haul", "transport", {"machine": "dump",
                                                                   "excavation_loading_task": "second-exc",
                                                                   "leveling_task": "level",
                                                                   "main_node": ["second-approach", "second-hub"],
                                                                   "sub_node": []})
        plan = Plan(plan.machines, tasks, ())
        with self.assertRaises(ValueError):
            build_route_documents(plan, graph)

    def test_unlisted_cross_area_road_is_excluded_per_transport_task(self):
        plan, graph = disconnected_scenarios()
        sections = (*graph.sections,
                    Section("cross-area", "approach", "second-hub", ((0, 1), (100, 102))))
        graph = GeoGraph(graph.points, sections, ())
        self.assertNotIn("cross-area", {document["section_id"] for document in build_route_documents(plan, graph)})

    def test_unlisted_point_roads_are_excluded_even_when_one_endpoint_is_explicit(self):
        plan, graph = branch_scenario(1)
        points = {**graph.points, "outside": Point("outside", 20, 20)}
        section = Section("outside-road", "approach", "outside", ((0, 1), (20, 20)))
        graph = GeoGraph(points, (*graph.sections, section), ())
        self.assertEqual({document["section_id"] for document in build_route_documents(plan, graph)},
                         {"approach-road", "trunk", "connector-0"})

    def test_boundary_only_work_area_roads_are_excluded(self):
        plan, graph = branch_scenario(2)
        exc = Task("exc", "excavation_loading", {"machine": "excavator", "dump_node": ["load", "load-work"]})
        plan = Plan(plan.machines, {**plan.tasks, "exc": exc}, ())
        points = {**graph.points, "load-work": Point("load-work", -1, 0)}
        sections = (*graph.sections,
                    Section("loading-only", "load", "load-work", ((0, 0), (-1, 0))),
                    Section("connection-only", "entry-0", "entry-1", ((1, 4), (2, 4))))
        graph = GeoGraph(points, sections, ())
        self.assertEqual({document["section_id"] for document in build_route_documents(plan, graph)},
                         {"approach-road", "trunk", "connector-0", "connector-1"})

    def test_explicit_route_nodes_are_not_excluded_by_other_work_references(self):
        plan, graph = branch_scenario(1)
        exc = Task("exc", "excavation_loading", {**plan.tasks["exc"].parameters, "backhoe_node": ["approach"]})
        plan = Plan(plan.machines, {**plan.tasks, "exc": exc}, ())
        self.assertIn("approach-road", {document["section_id"] for document in build_route_documents(plan, graph)})

    def test_route_rank_metadata_does_not_change_explicit_route_roles(self):
        geojson = ROOT / "json_samples/261004-kyoto.geojson"
        xml = ROOT / "json_samples/261004-kyoto.xml"
        plan, graph = load_inputs(geojson, xml)
        expected = build_route_documents(plan, graph)
        source = json.loads(geojson.read_text())
        for feature in source["features"]:
            if feature["geometry"]["type"] == "Point":
                # Deliberately contradict every old rank-based role and use an
                # arbitrary metadata value: only XML lists define transport.
                feature["properties"].setdefault("metadata", {})["route_rank"] = "ignored-xml-is-authoritative"
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / "changed-metadata.geojson"
            changed.write_text(json.dumps(source))
            changed_plan, changed_graph = load_inputs(changed, xml)
            self.assertEqual(build_route_documents(changed_plan, changed_graph), expected)

    def test_empty_sub_array_supports_a_single_main_node(self):
        plan, graph = passing_scenario(lanes=(), main_count=1)
        documents = build_route_documents(plan, graph)
        self.assertEqual({document["section_id"] for document in documents}, {"main-0", "main-1"})
        self.assertTrue(all(document["label"] == "main" for document in documents))

    def test_single_sub_node_has_two_main_attachments_in_xml_order(self):
        plan, graph = passing_scenario(lanes=((1, 2, ("single-pass",)),))
        documents = {document["section_id"]: document for document in build_route_documents(plan, graph)}
        first, last = documents["lane-0-0"], documents["lane-0-1"]
        self.assertEqual([first["x"][0], first["x"][-1]], [10, 15])
        self.assertEqual([last["x"][0], last["x"][-1]], [15, 20])
        self.assertEqual(first["related_point_down_main"], "main-1")
        self.assertEqual(first["related_point_up_sub"], "lane-0-1")
        self.assertEqual(last["related_point_up_main"], "main-3")
        self.assertEqual(last["related_point_down_sub"], "lane-0-0")
        self.assertEqual(documents["main-2"]["related_point_down_sub"], "lane-0-0")
        self.assertEqual(documents["main-2"]["related_point_up_sub"], "lane-0-1")

    def test_multiple_passing_lanes_allow_interleaved_xml_sub_nodes(self):
        plan, graph = passing_scenario(lanes=((0, 1, ("P", "Q")), (2, 3, ("R", "S"))))
        expected = {document["section_id"]: document for document in build_route_documents(plan, graph)}
        haul = Task("haul", "transport", {**plan.tasks["haul"].parameters, "sub_node": ["P", "R", "Q", "S"]})
        plan = Plan(plan.machines, {**plan.tasks, "haul": haul}, ())
        documents = {document["section_id"]: document for document in build_route_documents(plan, graph)}
        self.assertEqual(documents, expected)
        self.assertEqual(sum(document["label"] == "sub" for document in documents.values()), 6)
        self.assertEqual(documents["lane-0-0"]["related_point_up_sub"], "lane-0-1")
        self.assertEqual(documents["lane-1-1"]["related_point_up_sub"], "lane-1-2")

    def test_consecutive_passing_lanes_share_a_main_anchor_without_losing_direction(self):
        plan, graph = passing_scenario(lanes=((0, 1, ("a",)), (1, 2, ("b",))))
        documents = {document["section_id"]: document for document in build_route_documents(plan, graph)}
        self.assertEqual(len(documents), 9)
        self.assertEqual(sum(document["label"] == "main" for document in documents.values()), 5)
        self.assertEqual(sum(document["label"] == "sub" for document in documents.values()), 4)
        # At road-1, lane a arrives and lane b leaves. The scalar link must
        # choose the leaving lane in up direction and the arriving lane down.
        self.assertEqual(documents["main-1"]["related_point_up_sub"], "lane-1-0")
        self.assertEqual(documents["main-2"]["related_point_down_sub"], "lane-0-1")
        self.assertEqual(documents["lane-0-1"]["related_point_up_main"], "main-2")
        self.assertEqual(documents["lane-0-1"]["related_point_up_sub"], "lane-1-0")
        self.assertEqual(documents["lane-1-0"]["related_point_down_main"], "main-1")
        self.assertEqual(documents["lane-1-0"]["related_point_down_sub"], "lane-0-1")
        expected_endpoints = {"lane-0-0": ((0, 0), (5, 3)),
                              "lane-0-1": ((5, 3), (10, 0)),
                              "lane-1-0": ((10, 0), (15, 5)),
                              "lane-1-1": ((15, 5), (20, 0))}
        for identifier, (start, end) in expected_endpoints.items():
            document = documents[identifier]
            self.assertEqual((document["x"][0], document["y"][0]), start)
            self.assertEqual((document["x"][-1], document["y"][-1]), end)

    def test_xml_order_reversal_changes_direction_without_changing_source_geometry(self):
        plan, graph = passing_scenario(lanes=((0, 1, ("P", "Q")), (2, 3, ("R", "S"))))
        expected = {document["section_id"]: document for document in build_route_documents(plan, graph)}
        tasks = {**plan.tasks,
                 "exc": Task("exc", "excavation_loading", {"machine": "excavator", "dump_node": ["entry-0"]}),
                 "level": Task("level", "leveling", {"machine": "bulldozer", "connection_node": ["load"]}),
                 "haul": Task("haul", "transport", {**plan.tasks["haul"].parameters,
                                                        "main_node": list(reversed(plan.tasks["haul"].parameters["main_node"])),
                                                        "sub_node": list(reversed(plan.tasks["haul"].parameters["sub_node"]))})}
        reversed_plan = Plan(plan.machines, tasks, ())
        documents = {document["section_id"]: document for document in build_route_documents(reversed_plan, graph)}
        self.assertEqual(set(documents), set(expected))
        for identifier, document in documents.items():
            with self.subTest(section_id=identifier):
                previous = expected[identifier]
                self.assertEqual(document["x"], list(reversed(previous["x"])))
                self.assertEqual(document["y"], list(reversed(previous["y"])))
                for label in ("main", "sub"):
                    self.assertEqual(document[f"related_point_up_{label}"], previous[f"related_point_down_{label}"])
                    self.assertEqual(document[f"related_point_down_{label}"], previous[f"related_point_up_{label}"])

    def test_sub_xml_order_conflicting_with_main_attachments_is_rejected(self):
        plan, graph = passing_scenario()
        haul = Task("haul", "transport", {**plan.tasks["haul"].parameters,
                                          "sub_node": list(reversed(plan.tasks["haul"].parameters["sub_node"]))})
        plan = Plan(plan.machines, {**plan.tasks, "haul": haul}, ())
        with self.assertRaisesRegex(ValueError, "sub_node order conflicts with main_node order"):
            build_route_documents(plan, graph)

    def test_sub_xml_order_cannot_create_two_incoming_internal_sections(self):
        plan, graph = passing_scenario(lanes=((0, 1, ("P", "Q", "R")),))
        haul = Task("haul", "transport", {**plan.tasks["haul"].parameters, "sub_node": ["P", "R", "Q"]})
        plan = Plan(plan.machines, {**plan.tasks, "haul": haul}, ())
        with self.assertRaises(ValueError):
            build_route_documents(plan, graph)

    def test_missing_connection_in_declared_main_route_is_rejected(self):
        plan, graph = branch_scenario(1)
        graph = GeoGraph(graph.points, graph.sections[:-1], ())
        with self.assertRaisesRegex(ValueError, "do not reach declared nodes"):
            build_route_documents(plan, graph)

    def test_main_and_sub_node_membership_must_be_disjoint(self):
        plan, graph = branch_scenario(1)
        haul = Task("haul", "transport", {**plan.tasks["haul"].parameters, "sub_node": ["approach"]})
        plan = Plan(plan.machines, {**plan.tasks, "haul": haul}, ())
        with self.assertRaisesRegex(ValueError, "must be disjoint"):
            build_route_documents(plan, graph)

    def test_conflicting_main_xml_order_is_not_silently_reoriented(self):
        plan, graph = branch_scenario(1)
        haul = Task("haul", "transport", {**plan.tasks["haul"].parameters, "main_node": ["hub", "approach"]})
        plan = Plan(plan.machines, {**plan.tasks, "haul": haul}, ())
        with self.assertRaisesRegex(ValueError, "disconnected"):
            build_route_documents(plan, graph)

    def test_transport_section_ids_are_scoped_to_their_work_area(self):
        plan, graph = disconnected_scenarios()
        sections = (*graph.sections,
                    Section("cross-area", "approach", "second-hub", ((0, 1), (100, 102))))
        graph = GeoGraph(graph.points, sections, ())
        self.assertEqual(set(transport_section_ids(plan, graph, plan.tasks["haul"])),
                         {"approach-road", "trunk", "connector-0"})
        self.assertEqual(set(transport_section_ids(plan, graph, plan.tasks["second-haul"])),
                         {"second-approach-road", "second-trunk", "second-connector-0"})

    def test_variable_node_ids_and_counts_preserve_input_vertices_and_xml_direction(self):
        for main_count, lane_size in ((2, 1), (7, 5), (13, 9)):
            with self.subTest(main_count=main_count, lane_size=lane_size):
                nodes = tuple(f"P-{index}" for index in range(lane_size))
                plan, graph = passing_scenario(lanes=((0, main_count - 1, nodes),), main_count=main_count)
                mapping = {node: f"site_node_{12345 + index * 97}" for index, node in enumerate(graph.points)}
                points = {mapping[node]: Point(mapping[node], point.x + 123.25, point.y - 55.75)
                          for node, point in graph.points.items()}
                sections = tuple(Section(f"replacement-section-{index}", mapping[section.start], mapping[section.end],
                                         tuple((x + 123.25, y - 55.75) for x, y in section.coordinates))
                                 for index, section in enumerate(graph.sections))
                tasks = {}
                for identifier, task in plan.tasks.items():
                    parameters = {key: [mapping[node] for node in value] if key.endswith("_node") else value
                                  for key, value in task.parameters.items()}
                    tasks[identifier] = Task(identifier, task.name, parameters)
                plan, graph = Plan(plan.machines, tasks, ()), GeoGraph(points, sections, ())
                documents = build_route_documents(plan, graph)
                self.assertEqual(len(documents), main_count + 1 + lane_size + 1)
                self.assertEqual(sum(document["label"] == "main" for document in documents), main_count + 1)
                self.assertEqual(sum(document["label"] == "sub" for document in documents), lane_size + 1)
                main_order = {mapping["load"]: -1, mapping["entry-0"]: main_count,
                              **{mapping[f"road-{index}"]: index for index in range(main_count)}}
                sub_order = {mapping[node]: index for index, node in enumerate(nodes)}
                for document in documents:
                    section = next(section for section in sections if section.id == document["section_id"])
                    start, end = section.start, section.end
                    if document["label"] == "main":
                        forward = main_order[start] < main_order[end]
                    elif start in sub_order and end in sub_order:
                        forward = sub_order[start] < sub_order[end]
                    else:
                        forward = start == mapping["road-0"] or end == mapping[f"road-{main_count - 1}"]
                    coordinates = section.coordinates if forward else tuple(reversed(section.coordinates))
                    self.assertEqual(document["x"], [point[0] for point in coordinates])
                    self.assertEqual(document["y"], [point[1] for point in coordinates])


if __name__ == "__main__":
    unittest.main()
