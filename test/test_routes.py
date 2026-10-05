"""Logical driving sections follow XML roles/order and preserve source geometry."""

import json
from pathlib import Path
import tempfile
import unittest

from tms_if_input.model import GeoGraph, Plan, Point, Section, Task, load_inputs
from tms_if_input.routes import build_route_documents, leveling_entry_node, transport_section_ids

ROOT = Path(__file__).resolve().parents[1]


def by_representative(documents):
    return {document["section_id"].split("_", 2)[2]: document for document in documents}


def replace_task(plan, identifier, **parameters):
    task = plan.tasks[identifier]
    return Plan(plan.machines, {**plan.tasks, identifier: Task(identifier, task.name, {**task.parameters, **parameters})}, plan.edges)


def branch_scenario(columns=2, extended=False):
    machines = {"excavator": "custom_excavator", "dump": "custom_dump", "bulldozer": "custom_bulldozer"}
    tasks = {
        "exc": Task("exc", "excavation_loading", {"machine": "excavator", "dump_node": ["load"]}),
        "level": Task("level", "leveling", {"machine": "bulldozer", "connection_node": [f"entry-{i}" for i in range(columns)]}),
        "haul": Task("haul", "transport", {"machine": "dump", "excavation_loading_task": "exc", "leveling_task": "level",
                                             "sub_node": [], "main_node": ["approach", "hub"] +
                                             ([f"middle-{i}" for i in range(columns)] if extended else [])}),
    }
    points = {name: Point(name, x, y) for name, x, y in (("load", 0, 0), ("approach", 0, 1), ("hub", 0, 2))}
    sections = [Section("loading-boundary", "approach", "load", ((0, 1), (0, .5), (0, 0))),
                Section("trunk", "approach", "hub", ((0, 1), (0, 2)))]
    for i in range(columns):
        entry = f"entry-{i}"
        points[entry] = Point(entry, i + 1, 4)
        if extended:
            middle = f"middle-{i}"
            points[middle] = Point(middle, i + 1, 3)
            sections.append(Section(f"branch-{i}", "hub", middle, ((0, 2), (i + 1, 3))))
            sections.append(Section(f"leveling-boundary-{i}", middle, entry, ((i + 1, 3), (i + 1, 4))))
        else:
            sections.append(Section(f"leveling-boundary-{i}", "hub", entry, ((0, 2), (i + 1, 4))))
    return Plan(machines, tasks, ()), GeoGraph(points, tuple(sections), ())


def passing_scenario(lanes=((1, 2, ("P", "Q")),), main_count=6):
    plan, _ = branch_scenario(1)
    main_nodes = [f"road-{i}" for i in range(main_count)]
    plan = replace_task(plan, "haul", main_node=main_nodes, sub_node=[node for _, _, nodes in lanes for node in nodes])
    points = {"load": Point("load", -10, 0), "entry-0": Point("entry-0", main_count * 10, 0)}
    points.update({node: Point(node, i * 10, 0) for i, node in enumerate(main_nodes)})
    sections = []

    def road(name, start, end):
        a, b = points[start], points[end]
        coordinates = ((a.x, a.y), ((a.x + b.x) / 2, (a.y + b.y) / 2 + .5), (b.x, b.y))
        if len(sections) // 2 % 2:
            start, end, coordinates = end, start, tuple(reversed(coordinates))
        sections.append(Section(name, start, end, coordinates))
        sections.append(Section("reverse-" + name, end, start, tuple(reversed(coordinates))))

    chain = ["load"] + main_nodes + ["entry-0"]
    for i, (start, end) in enumerate(zip(chain, chain[1:])):
        road(f"main-{i}", start, end)
    for lane_index, (low, high, nodes) in enumerate(lanes):
        for i, node in enumerate(nodes):
            points[node] = Point(node, (low + (high - low) * (i + 1) / (len(nodes) + 1)) * 10, 3 + lane_index * 2)
        chain = [main_nodes[low]] + list(nodes) + [main_nodes[high]]
        for i, (start, end) in enumerate(zip(chain, chain[1:])):
            road(f"lane-{lane_index}-{i}", start, end)
    return plan, GeoGraph(points, tuple(sections), ())


def disconnected_scenarios():
    plan, graph = branch_scenario(1)
    points = {**graph.points, **{"second-" + node: Point("second-" + node, point.x + 100, point.y + 50)
                               for node, point in graph.points.items()}}
    sections = (*graph.sections, *(Section("second-" + section.id, "second-" + section.start, "second-" + section.end,
                                          tuple((x + 100, y + 50) for x, y in section.coordinates)) for section in graph.sections))
    tasks = {**plan.tasks}
    for identifier, task in plan.tasks.items():
        parameters = {key: ["second-" + node for node in value] if key.endswith("_node") else
                      "second-" + value if key in {"excavation_loading_task", "leveling_task"} else value
                      for key, value in task.parameters.items()}
        tasks["second-" + identifier] = Task("second-" + identifier, task.name, parameters)
    return Plan(plan.machines, tasks, ()), GeoGraph(points, sections, ())


def source_chain(graph, nodes):
    """Independent expectation from the supplied source endpoints/vertices."""
    coordinates = []
    for start, end in zip(nodes, nodes[1:]):
        section = next(section for section in graph.sections if {section.start, section.end} == {start, end})
        edge = section.coordinates if section.start == start else tuple(reversed(section.coordinates))
        coordinates.extend(edge[1:] if coordinates and coordinates[-1] == edge[0] else edge)
    return coordinates


class TransportRouteTests(unittest.TestCase):
    def assert_geometry(self, document, expected):
        self.assertEqual(document["x"], [point[0] for point in expected])
        self.assertEqual(document["y"], [point[1] for point in expected])
        for field in ("z", "qx", "qy", "qz"):
            self.assertEqual(document[field], [0] * len(expected))
        self.assertEqual(document["qw"], [1] * len(expected))

    def test_sample_has_five_main_two_sub_groups_and_stable_content_ids(self):
        plan, graph = load_inputs(ROOT / "json_samples/261004-kyoto.geojson", ROOT / "json_samples/261004-kyoto.xml")
        documents = build_route_documents(plan, graph)
        self.assertEqual(len(documents), 7)
        self.assertEqual(sum(document["label"] == "main" for document in documents), 5)
        self.assertEqual(sum(document["label"] == "sub" for document in documents), 2)
        self.assertEqual(set(by_representative(documents)),
                         {"3260195895", "3453947199", "3775605330", "3483617579", "1901058875", "768363352", "2192002421"})
        self.assertEqual(build_route_documents(plan, graph), documents)
        for document in documents:
            self.assertRegex(document["section_id"], r"^route_[0-9a-f]{16}_")
            self.assertEqual(document["record_name"], document["section_id"])
            self.assertEqual(document["preferred_direction"], "up")
            self.assertEqual(document["type"], "static")
            self.assertEqual(document["model_name"], ["mst110cr", "mst2200vdr"])
            self.assert_geometry(document, list(zip(document["x"], document["y"])))
        for task in plan.tasks.values():
            if task.name == "transport":
                self.assertEqual(set(transport_section_ids(plan, graph, task)), {document["section_id"] for document in documents})

    def test_loading_and_leveling_boundaries_are_outside_driving_sections(self):
        for columns in (1, 2, 9, 17):
            with self.subTest(columns=columns):
                plan, graph = branch_scenario(columns)
                documents = build_route_documents(plan, graph)
                self.assertEqual(set(by_representative(documents)), {"trunk"})
                self.assert_geometry(documents[0], [(0, 1), (0, 2)])
                self.assertTrue(all(documents[0][f"related_point_{direction}_{label}"] == ""
                                    for direction in ("up", "down") for label in ("main", "sub")))
                self.assertEqual(leveling_entry_node(plan, graph, plan.tasks["level"]), "entry-0")

    def test_leveling_entry_retains_xml_order_of_raw_boundary_endpoints(self):
        plan, graph = branch_scenario(3)
        plan = replace_task(plan, "level", connection_node=["entry-2", "entry-0", "entry-1"])
        self.assertEqual(leveling_entry_node(plan, graph, plan.tasks["level"]), "entry-2")
        plan = replace_task(plan, "level", connection_node=["load", "entry-0", "entry-1", "entry-2"])
        self.assertEqual(leveling_entry_node(plan, graph, plan.tasks["level"]), "entry-0")

    def test_leveling_without_its_own_transport_has_no_fallback(self):
        plan, graph = branch_scenario(1)
        extra = Task("other-level", "leveling", {"machine": "bulldozer", "connection_node": ["approach"]})
        plan = Plan(plan.machines, {**plan.tasks, extra.id: extra}, ())
        with self.assertRaises(ValueError):
            leveling_entry_node(plan, graph, extra)

    def test_one_main_node_is_a_one_waypoint_section_with_no_internal_edge(self):
        plan, graph = passing_scenario(lanes=(), main_count=1)
        documents = build_route_documents(plan, graph)
        self.assertEqual(set(by_representative(documents)), {"road-0"})
        self.assert_geometry(documents[0], [(0, 0)])
        self.assertEqual(transport_section_ids(plan, graph, plan.tasks["haul"]), [documents[0]["section_id"]])
        self.assertEqual(leveling_entry_node(plan, graph, plan.tasks["level"]), "entry-0")

    def test_empty_sub_array_combines_all_contiguous_main_edges(self):
        plan, graph = passing_scenario(lanes=(), main_count=7)
        documents = build_route_documents(plan, graph)
        self.assertEqual(set(by_representative(documents)), {"main-1"})
        self.assert_geometry(documents[0], source_chain(graph, [f"road-{i}" for i in range(7)]))

    def test_one_sub_node_with_only_work_boundaries_is_a_one_waypoint_sub_section(self):
        plan, graph = passing_scenario(lanes=(), main_count=1)
        plan = replace_task(plan, "haul", sub_node=["single-sub"])
        points = {**graph.points, "single-sub": Point("single-sub", 0, 2)}
        sections = (*graph.sections,
                    Section("sub-loading-boundary", "load", "single-sub", ((-10, 0), (0, 2))),
                    Section("sub-leveling-boundary", "single-sub", "entry-0", ((0, 2), (10, 0))))
        documents = by_representative(build_route_documents(plan, GeoGraph(points, sections, ())))
        self.assertEqual(set(documents), {"road-0", "single-sub"})
        self.assertEqual(documents["single-sub"]["label"], "sub")
        self.assert_geometry(documents["single-sub"], [(0, 2)])

    def test_single_sub_node_combines_both_attachment_edges_into_one_lane(self):
        plan, graph = passing_scenario(lanes=((1, 3, ("single",)),))
        documents = by_representative(build_route_documents(plan, graph))
        self.assertEqual(set(documents), {"main-1", "main-2", "main-4", "lane-0-0"})
        self.assert_geometry(documents["lane-0-0"], source_chain(graph, ["road-1", "single", "road-3"]))
        self.assertEqual(documents["main-1"]["related_point_up_sub"], documents["lane-0-0"]["section_id"])
        self.assertEqual(documents["main-4"]["related_point_down_sub"], documents["lane-0-0"]["section_id"])
        self.assertEqual(documents["main-2"]["related_point_up_sub"], "")
        self.assertEqual(documents["main-2"]["related_point_down_sub"], "")

    def test_multiple_interleaved_lanes_remain_separate_groups(self):
        plan, graph = passing_scenario(lanes=((1, 2, ("P", "Q")), (3, 4, ("R", "S"))))
        expected = build_route_documents(plan, graph)
        plan = replace_task(plan, "haul", sub_node=["P", "R", "Q", "S"])
        documents = build_route_documents(plan, graph)
        self.assertEqual(by_representative(documents), by_representative(expected))
        self.assertEqual(len(documents), 7)
        self.assert_geometry(by_representative(documents)["lane-0-0"], source_chain(graph, ["road-1", "P", "Q", "road-2"]))
        self.assert_geometry(by_representative(documents)["lane-1-0"], source_chain(graph, ["road-3", "R", "S", "road-4"]))

    def test_boundary_and_shared_lane_anchors_have_one_point_main_sections(self):
        plan, graph = passing_scenario(lanes=((0, 1, ("a",)), (1, 2, ("b",))), main_count=3)
        documents = by_representative(build_route_documents(plan, graph))
        self.assertEqual(set(documents), {"road-0", "main-1", "road-1", "main-2", "road-2", "lane-0-0", "lane-1-0"})
        for node, x in (("road-0", 0), ("road-1", 10), ("road-2", 20)):
            self.assert_geometry(documents[node], [(x, 0)])
        names = {"sec1": "road-0", "sec2": "main-1", "sec3": "road-1", "sec4": "main-2", "sec5": "road-2",
                 "sec6": "lane-0-0", "sec7": "lane-1-0"}
        expected = {"sec1": ("sec2", "sec6", "", ""), "sec2": ("sec3", "", "sec1", ""),
                    "sec3": ("sec4", "sec7", "sec2", "sec6"), "sec4": ("sec5", "", "sec3", ""),
                    "sec5": ("", "", "sec4", "sec7"), "sec6": ("sec3", "", "sec1", ""), "sec7": ("sec5", "", "sec3", "")}
        section_names = {documents[representative]["section_id"]: name for name, representative in names.items()}
        actual = {name: tuple(section_names.get(documents[representative][f"related_point_{direction}_{label}"], "")
                              for direction in ("up", "down") for label in ("main", "sub")) for name, representative in names.items()}
        self.assertEqual(actual, expected)

    def test_xml_reversal_changes_group_ids_but_preserves_geometry_and_link_topology(self):
        plan, graph = passing_scenario(lanes=((1, 2, ("P", "Q")), (3, 4, ("R", "S"))))
        expected = build_route_documents(plan, graph)
        changed = replace_task(plan, "exc", dump_node=["entry-0"])
        changed = replace_task(changed, "level", connection_node=["load"])
        changed = replace_task(changed, "haul", main_node=list(reversed(plan.tasks["haul"].parameters["main_node"])),
                               sub_node=list(reversed(plan.tasks["haul"].parameters["sub_node"])))
        documents = build_route_documents(changed, graph)
        self.assertEqual(len(documents), len(expected))
        matching = {old["section_id"]: next(new for new in documents if new["label"] == old["label"]
                                            and new["x"] == list(reversed(old["x"])) and new["y"] == list(reversed(old["y"]))) for old in expected}
        self.assertTrue({document["section_id"] for document in documents}.isdisjoint(matching))
        for old in expected:
            new = matching[old["section_id"]]
            for label in ("main", "sub"):
                for direction, opposite in (("up", "down"), ("down", "up")):
                    link = old[f"related_point_{opposite}_{label}"]
                    self.assertEqual(new[f"related_point_{direction}_{label}"], matching[link]["section_id"] if link else "")

    def test_changed_geometry_generates_new_ids_and_repeat_is_stable(self):
        plan, graph = passing_scenario()
        original = build_route_documents(plan, graph)
        sections = list(graph.sections)
        index = next(i for i, section in enumerate(sections) if section.id == "main-1")
        previous = sections[index]
        coordinates = (previous.coordinates[0], (previous.coordinates[1][0], previous.coordinates[1][1] + .25), previous.coordinates[-1])
        sections[index] = Section(previous.id, previous.start, previous.end, coordinates)
        sections[index + 1] = Section(sections[index + 1].id, previous.end, previous.start, tuple(reversed(coordinates)))
        changed = GeoGraph(graph.points, tuple(sections), ())
        documents = build_route_documents(plan, changed)
        self.assertTrue({document["section_id"] for document in original}.isdisjoint({document["section_id"] for document in documents}))
        self.assertEqual(build_route_documents(plan, changed), documents)

    def test_join_removes_only_identical_shared_coordinates(self):
        plan, graph = passing_scenario(lanes=(), main_count=3)
        sections = []
        for section in graph.sections:
            coordinates = list(section.coordinates)
            if section.id in {"main-2", "reverse-main-2"}:
                coordinates[0 if section.start == "road-1" else -1] = (10.0001, 0)
            sections.append(Section(section.id, section.start, section.end, tuple(coordinates)))
        graph = GeoGraph(graph.points, tuple(sections), ())
        documents = build_route_documents(plan, graph)
        expected = source_chain(graph, ["road-0", "road-1", "road-2"])
        self.assertEqual(len(expected), 6)
        self.assertEqual(len(documents), 1)
        self.assert_geometry(documents[0], expected)

    def test_unlisted_and_boundary_only_roads_do_not_change_driving_content(self):
        plan, graph = branch_scenario(2)
        expected = build_route_documents(plan, graph)
        plan = replace_task(plan, "exc", dump_node=["load", "load-work"])
        points = {**graph.points, "load-work": Point("load-work", -1, 0), "outside": Point("outside", 20, 20)}
        sections = (*graph.sections, Section("outside", "approach", "outside", ((0, 1), (20, 20))),
                    Section("loading-only", "load", "load-work", ((0, 0), (-1, 0))),
                    Section("connection-only", "entry-0", "entry-1", ((1, 4), (2, 4))))
        self.assertEqual(build_route_documents(plan, GeoGraph(points, sections, ())), expected)

    def test_rank_metadata_and_other_work_references_do_not_override_xml_roles(self):
        plan, graph = branch_scenario(1)
        expected = build_route_documents(plan, graph)
        self.assertEqual(build_route_documents(replace_task(plan, "exc", backhoe_node=["approach"]), graph), expected)
        geojson, xml = ROOT / "json_samples/261004-kyoto.geojson", ROOT / "json_samples/261004-kyoto.xml"
        original_plan, original_graph = load_inputs(geojson, xml)
        source = json.loads(geojson.read_text())
        for feature in source["features"]:
            if feature["geometry"]["type"] == "Point":
                feature["properties"].setdefault("metadata", {})["route_rank"] = "ignored"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "changed.geojson"
            path.write_text(json.dumps(source))
            changed_plan, changed_graph = load_inputs(path, xml)
            self.assertEqual(build_route_documents(changed_plan, changed_graph), build_route_documents(original_plan, original_graph))

    def test_multiple_work_areas_keep_group_ids_and_entry_context_separate(self):
        plan, graph = disconnected_scenarios()
        graph = GeoGraph(graph.points, (*graph.sections, Section("cross-area", "approach", "second-hub", ((0, 1), (100, 52)))), ())
        documents = by_representative(build_route_documents(plan, graph))
        self.assertEqual(set(documents), {"trunk", "second-trunk"})
        self.assert_geometry(documents["trunk"], [(0, 1), (0, 2)])
        self.assert_geometry(documents["second-trunk"], [(100, 51), (100, 52)])
        self.assertEqual(transport_section_ids(plan, graph, plan.tasks["haul"]), [documents["trunk"]["section_id"]])
        self.assertEqual(transport_section_ids(plan, graph, plan.tasks["second-haul"]), [documents["second-trunk"]["section_id"]])
        self.assertEqual(leveling_entry_node(plan, graph, plan.tasks["second-level"]), "second-entry-0")

    def test_duplicate_transports_share_identical_groups_once(self):
        plan, graph = passing_scenario()
        expected = build_route_documents(plan, graph)
        task = plan.tasks["haul"]
        other = Task("haul-other", task.name, task.parameters.copy())
        plan = Plan(plan.machines, {**plan.tasks, other.id: other}, ())
        self.assertEqual(build_route_documents(plan, graph), expected)
        self.assertEqual(transport_section_ids(plan, graph, task), transport_section_ids(plan, graph, other))

    def test_variable_node_ids_counts_and_curved_vertices(self):
        for main_count, lane_size in ((2, 1), (7, 5), (13, 9)):
            with self.subTest(main_count=main_count, lane_size=lane_size):
                nodes = tuple(f"P-{i}" for i in range(lane_size))
                plan, graph = passing_scenario(lanes=((0, main_count - 1, nodes),), main_count=main_count)
                mapping = {node: f"site_node_{12345 + i * 97}" for i, node in enumerate(graph.points)}
                points = {mapping[node]: Point(mapping[node], point.x + 123.25, point.y - 55.75) for node, point in graph.points.items()}
                sections = tuple(Section(f"replacement_section_{i}", mapping[section.start], mapping[section.end],
                                         tuple((x + 123.25, y - 55.75) for x, y in section.coordinates)) for i, section in enumerate(graph.sections))
                tasks = {identifier: Task(identifier, task.name, {key: [mapping[node] for node in value] if key.endswith("_node") else value
                                                                for key, value in task.parameters.items()}) for identifier, task in plan.tasks.items()}
                plan, graph = Plan(plan.machines, tasks, ()), GeoGraph(points, sections, ())
                documents = build_route_documents(plan, graph)
                self.assertEqual(len(documents), 4)
                main = next(document for document in documents if document["label"] == "main" and len(document["x"]) > 1)
                sub = next(document for document in documents if document["label"] == "sub")
                self.assert_geometry(main, source_chain(graph, [mapping[f"road-{i}"] for i in range(main_count)]))
                self.assert_geometry(sub, source_chain(graph, [mapping["road-0"]] + [mapping[node] for node in nodes] + [mapping[f"road-{main_count - 1}"]]))

    def test_invalid_direction_or_missing_boundary_reachability_remains_an_error(self):
        plan, graph = branch_scenario(1)
        with self.assertRaises(ValueError):
            build_route_documents(plan, GeoGraph(graph.points, graph.sections[:-1], ()))
        with self.assertRaises(ValueError):
            build_route_documents(replace_task(plan, "haul", main_node=["hub", "approach"]), graph)
        plan, graph = passing_scenario()
        with self.assertRaises(ValueError):
            build_route_documents(replace_task(plan, "haul", sub_node=["Q", "P"]), graph)

    def test_duplicate_roles_and_ambiguous_internal_links_are_errors(self):
        plan, graph = passing_scenario(lanes=((1, 2, ("P", "Q", "R")),))
        with self.assertRaises(ValueError):
            build_route_documents(replace_task(plan, "haul", sub_node=["road-1"]), graph)
        with self.assertRaises(ValueError):
            build_route_documents(replace_task(plan, "haul", sub_node=["P", "R", "Q"]), graph)
        plan, graph = branch_scenario(1)
        duplicate = Section("duplicate", "approach", "hub", ((0, 1), (0, 2)))
        with self.assertRaises(ValueError):
            build_route_documents(plan, GeoGraph(graph.points, (*graph.sections, duplicate), ()))

    def test_main_fanout_that_cannot_fit_scalar_links_is_rejected(self):
        plan, graph = branch_scenario(2, extended=True)
        with self.assertRaises(ValueError):
            build_route_documents(plan, graph)


if __name__ == "__main__":
    unittest.main()
