"""The ROS entry point exits after importing and cleans up every lifecycle."""

from contextlib import ExitStack
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tms_if_input import importer


class FakeTrigger:
    class Request:
        pass

    class Response:
        def __init__(self):
            self.success = False
            self.message = ''


class ExternalShutdownException(Exception):
    pass


class FakeROS:
    """Provide only the ROS lifecycle APIs used by the entry point."""

    def __init__(self, **overrides):
        self.overrides = {
            'geojson_path': '/samples/scenario.geojson',
            'xml_path': '/samples/scenario.xml',
            **overrides,
        }
        self.active = False
        self.nodes = []
        self.init_arguments = []
        self.shutdown_calls = 0
        self.spin_calls = 0
        self.on_spin = None
        self.spin_error = KeyboardInterrupt()
        self.setup_error = None
        self.init_error = None
        self.context = ModuleType('rclpy')
        self.context.init = self.init
        self.context.ok = lambda: self.active
        self.context.spin = self.spin
        self.context.shutdown = self.shutdown
        owner = self

        class FakeNode:
            def __init__(self, name):
                self.name = name
                self.parameters = {}
                self.services = []
                self.destroy_calls = 0
                self.logs = SimpleNamespace(info=Mock(), error=Mock())
                owner.nodes.append(self)

            def declare_parameter(self, name, default):
                self.parameters[name] = owner.overrides.get(name, default)

            def get_parameter(self, name):
                return SimpleNamespace(value=self.parameters[name])

            def create_service(self, service_type, name, callback):
                if owner.setup_error:
                    raise owner.setup_error
                service = SimpleNamespace(type=service_type, name=name, callback=callback)
                self.services.append(service)
                return service

            def get_logger(self):
                return self.logs

            def destroy_node(self):
                self.destroy_calls += 1

        node_module = ModuleType('rclpy.node')
        node_module.Node = FakeNode
        executors = ModuleType('rclpy.executors')
        executors.ExternalShutdownException = ExternalShutdownException
        srv_module = ModuleType('std_srvs.srv')
        srv_module.Trigger = FakeTrigger
        self.modules = {
            'rclpy': self.context,
            'rclpy.node': node_module,
            'rclpy.executors': executors,
            'std_srvs': ModuleType('std_srvs'),
            'std_srvs.srv': srv_module,
        }

    def init(self, args=None):
        self.init_arguments.append(args)
        if self.init_error:
            raise self.init_error
        self.active = True

    def spin(self, node):
        self.spin_calls += 1
        if self.on_spin:
            self.on_spin(node)
        if self.spin_error:
            raise self.spin_error

    def shutdown(self):
        self.shutdown_calls += 1
        self.active = False


class ImporterLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        compilation = SimpleNamespace(parameters=[{}, {}], tasks=[{}])
        self.compile = self.stack.enter_context(patch.object(
            importer, 'compile_scenario', return_value=compilation))
        self.export = self.stack.enter_context(patch.object(
            importer, 'export_compilation', return_value=Path('/tmp/importer-review')))
        self.write = self.stack.enter_context(patch.object(importer, 'write_database', return_value={
            'inserted_parameters': 2, 'inserted_tasks': 1,
            'skipped_parameters': 0, 'skipped_tasks': 0,
        }))

    def run_main(self, ros, args=None):
        with patch.dict('sys.modules', ros.modules):
            return importer.main(args)

    def assert_cleaned(self, ros):
        self.assertFalse(ros.active)
        self.assertEqual(ros.shutdown_calls, 1)
        self.assertTrue(ros.nodes)
        self.assertTrue(all(node.destroy_calls == 1 for node in ros.nodes))

    def test_default_import_exits_zero_without_spinning_and_reports_database(self):
        ros = FakeROS(mongo_db='target_database', mongo_uri='mongodb://user:secret@localhost:27017')
        self.assertEqual(self.run_main(ros, ['--ros-args']), 0)
        self.assertEqual(ros.init_arguments, [['--ros-args']])
        self.assertEqual(ros.spin_calls, 0)
        self.compile.assert_called_once_with('/samples/scenario.geojson', '/samples/scenario.xml')
        self.export.assert_called_once()
        self.write.assert_called_once()
        self.assertEqual(self.write.call_args.kwargs['mongo_db'], 'target_database')
        self.assertNotIn('import_key', self.write.call_args.kwargs)
        self.assertNotIn('import_key', ros.nodes[0].parameters)
        message = ros.nodes[0].logs.info.call_args.args[0]
        self.assertIn('Database target_database: Inserted 2 parameters and 1 tasks', message)
        self.assertNotIn('secret', message)
        self.assert_cleaned(ros)

    def test_missing_input_exits_one_before_compiling_or_writing(self):
        ros = FakeROS(geojson_path='')
        self.assertEqual(self.run_main(ros), 1)
        self.assertEqual(ros.spin_calls, 0)
        self.compile.assert_not_called()
        self.export.assert_not_called()
        self.write.assert_not_called()
        ros.nodes[0].logs.error.assert_called_once_with('Both geojson_path and xml_path are required')
        self.assert_cleaned(ros)

    def test_invalid_input_exits_one_and_does_not_export_or_register(self):
        ros = FakeROS()
        self.compile.side_effect = ValueError('invalid flow')
        self.assertEqual(self.run_main(ros), 1)
        self.export.assert_not_called()
        self.write.assert_not_called()
        self.assert_cleaned(ros)

    def test_export_failure_exits_one_and_does_not_register(self):
        ros = FakeROS()
        self.export.side_effect = OSError('output is not writable')
        self.assertEqual(self.run_main(ros), 1)
        self.write.assert_not_called()
        self.assert_cleaned(ros)

    def test_database_failure_exits_one_after_export_and_logs_error_once(self):
        ros = FakeROS()
        self.write.side_effect = RuntimeError('database connection failed')
        self.assertEqual(self.run_main(ros), 1)
        self.assertEqual(ros.spin_calls, 0)
        self.export.assert_called_once()
        ros.nodes[0].logs.error.assert_called_once_with('database connection failed')
        self.assert_cleaned(ros)

    def test_dry_run_exits_zero_without_database_access(self):
        ros = FakeROS(dry_run=True)
        self.assertEqual(self.run_main(ros), 0)
        self.export.assert_called_once()
        self.write.assert_not_called()
        self.assertEqual(ros.spin_calls, 0)
        self.assertIn('Compiled 2 parameters and 1 tasks', ros.nodes[0].logs.info.call_args.args[0])
        self.assert_cleaned(ros)

    def test_keep_alive_retains_service_and_can_import_again(self):
        ros = FakeROS(keep_alive=True)
        responses = []

        def request_import(node):
            self.assertEqual(len(node.services), 1)
            self.assertEqual(node.services[0].name, '~/import')
            responses.append(node.services[0].callback(FakeTrigger.Request(), FakeTrigger.Response()))

        ros.on_spin = request_import
        self.assertEqual(self.run_main(ros), 0)
        self.assertEqual(ros.spin_calls, 1)
        self.assertEqual(self.write.call_count, 2)
        self.assertTrue(responses[0].success)
        self.assert_cleaned(ros)

    def test_keep_alive_preserves_failed_startup_status_until_interrupt(self):
        ros = FakeROS(keep_alive=True)
        self.write.side_effect = RuntimeError('duplicate task models')
        self.assertEqual(self.run_main(ros), 1)
        self.assertEqual(ros.spin_calls, 1)
        ros.nodes[0].logs.error.assert_called_once_with('duplicate task models')
        self.assert_cleaned(ros)

    def test_import_on_start_false_keeps_service_even_with_default_keep_alive(self):
        ros = FakeROS(import_on_start=False)
        self.assertEqual(self.run_main(ros), 0)
        self.assertEqual(ros.spin_calls, 1)
        self.assertEqual(ros.nodes[0].services[0].name, '~/import')
        self.compile.assert_not_called()
        self.write.assert_not_called()
        self.assert_cleaned(ros)

    def test_service_only_mode_imports_when_requested(self):
        ros = FakeROS(import_on_start=False)
        responses = []
        ros.on_spin = lambda node: responses.append(
            node.services[0].callback(FakeTrigger.Request(), FakeTrigger.Response()))
        self.assertEqual(self.run_main(ros), 0)
        self.write.assert_called_once()
        self.assertTrue(responses[0].success)
        self.assert_cleaned(ros)

    def test_service_setup_failure_cleans_up_partially_created_node(self):
        ros = FakeROS()
        ros.setup_error = RuntimeError('service creation failed')
        with patch('sys.stderr') as errors:
            self.assertEqual(self.run_main(ros), 1)
        self.assertTrue(errors.write.called)
        self.assertEqual(ros.spin_calls, 0)
        self.compile.assert_not_called()
        self.assert_cleaned(ros)

    def test_spin_failure_exits_one_and_cleans_up(self):
        ros = FakeROS(import_on_start=False)
        ros.spin_error = RuntimeError('executor failed')
        self.assertEqual(self.run_main(ros), 1)
        ros.nodes[0].logs.error.assert_called_once_with('executor failed')
        self.assert_cleaned(ros)

    def test_external_shutdown_destroys_node_without_shutdown_twice(self):
        ros = FakeROS(import_on_start=False)
        ros.on_spin = lambda node: ros.shutdown()
        ros.spin_error = ExternalShutdownException()
        self.assertEqual(self.run_main(ros), 0)
        self.assert_cleaned(ros)

    def test_ros_init_failure_returns_one_without_constructing_node(self):
        ros = FakeROS()
        ros.init_error = RuntimeError('context initialization failed')
        with patch('sys.stderr'):
            self.assertEqual(self.run_main(ros), 1)
        self.assertFalse(ros.active)
        self.assertEqual(ros.nodes, [])
        self.assertEqual(ros.shutdown_calls, 0)
        self.compile.assert_not_called()


if __name__ == '__main__':
    unittest.main()
