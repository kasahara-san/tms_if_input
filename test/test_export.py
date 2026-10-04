"""Review artifacts preserve compiled content and reject unsafe output names."""

import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from tms_if_input.compiler import Compilation, export_compilation


def sample_compilation():
    return Compilation([
        {"type": "static", "record_name": "geo_fence",
         "coordinates": [[{"x": 1.0, "y": 2.0}, {"x": 4.0, "y": 2.0},
                          {"x": 4.0, "y": 5.0}, {"x": 1.0, "y": 2.0}]]},
        {"record_name": "施工パラメータ", "model_name": ["mst110cr_3", "mst2200vdr_7"],
         "x": [5.5, 8.75], "y": [-2.0, -9.25]},
    ], [{
        "task_id": 1, "type": "task", "model_name": "zx200_8",
        "description": "zx200_8_task.xml",
        "task_sequence": '<root main_tree_to_execute="BehaviorTree"><BehaviorTree ID="BehaviorTree"><Sequence /></BehaviorTree></root>',
    }])


class ExportTests(unittest.TestCase):
    def test_outputs_match_compilation_and_leave_no_temporary_artifacts(self):
        compilation = sample_compilation()
        with tempfile.TemporaryDirectory(prefix="tms_if_input_export_") as temporary:
            output = Path(temporary) / "review"
            self.assertEqual(export_compilation(compilation, output), output)
            self.assertEqual(json.loads((output / "parameters.json").read_text()), compilation.parameters)
            self.assertEqual(json.loads((output / "tasks.json").read_text()), compilation.tasks)
            xml_path = output / compilation.tasks[0]["description"]
            self.assertEqual(xml_path.read_text(), compilation.tasks[0]["task_sequence"] + "\n")
            self.assertEqual(ET.parse(xml_path).getroot().tag, "root")
            self.assertEqual({path.name for path in output.iterdir()}, {
                "parameters.json", "tasks.json", "zx200_8_task.xml",
            })
            self.assertIn("施工パラメータ", (output / "parameters.json").read_text())

    def test_unsafe_task_filename_does_not_modify_existing_artifacts(self):
        compilation = sample_compilation()
        compilation.tasks[0]["description"] = "../outside.xml"
        with tempfile.TemporaryDirectory(prefix="tms_if_input_export_") as temporary:
            output = Path(temporary) / "review"
            output.mkdir()
            existing = output / "parameters.json"
            existing.write_text("previous artifact")
            with self.assertRaisesRegex(ValueError, "Unsafe output filename"):
                export_compilation(compilation, output)
            self.assertEqual(existing.read_text(), "previous artifact")
            self.assertEqual(list(output.iterdir()), [existing])
            self.assertFalse((Path(temporary) / "outside.xml").exists())

    def test_invalid_compilation_does_not_create_output_directory(self):
        compilation = Compilation([{"record_name": "same"}, {"record_name": "same"}], [])
        with tempfile.TemporaryDirectory(prefix="tms_if_input_export_") as temporary:
            output = Path(temporary) / "must_not_exist"
            with self.assertRaisesRegex(ValueError, "Conflicting parameter"):
                export_compilation(compilation, output)
            self.assertFalse(output.exists())

    def test_second_export_updates_current_artifacts_and_preserves_unrelated_files(self):
        compilation = sample_compilation()
        with tempfile.TemporaryDirectory(prefix="tms_if_input_export_") as temporary:
            output = Path(temporary)
            unrelated = output / "notes.txt"
            unrelated.write_text("keep this")
            export_compilation(compilation, output)
            compilation.parameters[1]["x"] = [999.0]
            export_compilation(compilation, output)
            self.assertEqual(json.loads((output / "parameters.json").read_text()), compilation.parameters)
            self.assertEqual(unrelated.read_text(), "keep this")

    def test_replacing_machine_removes_only_unchanged_previous_xml(self):
        compilation = sample_compilation()
        with tempfile.TemporaryDirectory(prefix="tms_if_input_export_") as temporary:
            output = Path(temporary)
            export_compilation(compilation, output)
            old = output / compilation.tasks[0]["description"]
            replacement = sample_compilation()
            replacement.tasks[0]["model_name"] = "zx120_2"
            replacement.tasks[0]["description"] = "zx120_2_task.xml"
            export_compilation(replacement, output)
            self.assertFalse(old.exists())
            self.assertTrue((output / "zx120_2_task.xml").exists())

    def test_stale_xml_with_user_changes_is_preserved(self):
        compilation = sample_compilation()
        with tempfile.TemporaryDirectory(prefix="tms_if_input_export_") as temporary:
            output = Path(temporary)
            export_compilation(compilation, output)
            old = output / compilation.tasks[0]["description"]
            old.write_text("user's changed task")
            export_compilation(Compilation([], []), output)
            self.assertEqual(old.read_text(), "user's changed task")


if __name__ == "__main__":
    unittest.main()
