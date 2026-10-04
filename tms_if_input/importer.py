"""ROS entry point for importing one replaceable pair of construction files."""

from pathlib import Path
import sys

from .compiler import compile_scenario, export_compilation
from .database import write_database


def main(args=None) -> int:
    # Keep conversion, tests and dry-run CLI usable without a sourced ROS setup.
    import rclpy
    from rclpy.executors import ExternalShutdownException
    from rclpy.node import Node
    from std_srvs.srv import Trigger

    class ScenarioImporter(Node):
        def __init__(self):
            super().__init__('scenario_importer')
            try:
                for name, default in {
                    'geojson_path': '', 'xml_path': '', 'output_dir': '/tmp/tms_if_input',
                    'mongo_uri': 'mongodb://localhost:27017', 'mongo_db': 'rostmsdb',
                    'mongo_timeout_ms': 30000,
                    'dry_run': False, 'import_on_start': True, 'keep_alive': False,
                }.items():
                    self.declare_parameter(name, default)
                self._service = self.create_service(Trigger, '~/import', self._handle)
            except Exception:
                self.destroy_node()
                raise

        def _handle(self, request, response):
            values = {name: self.get_parameter(name).value for name in (
                'geojson_path', 'xml_path', 'output_dir', 'mongo_uri', 'mongo_db',
                'mongo_timeout_ms', 'dry_run')}
            try:
                if not values['geojson_path'] or not values['xml_path']:
                    raise ValueError('Both geojson_path and xml_path are required')
                compilation = compile_scenario(values['geojson_path'], values['xml_path'])
                destination = export_compilation(compilation, values['output_dir'])
                if not values['dry_run']:
                    result = write_database(
                        compilation, mongo_uri=values['mongo_uri'],
                        mongo_db=values['mongo_db'],
                        timeout_ms=values['mongo_timeout_ms'])
                    summary = (
                        f"Database {values['mongo_db']}: Inserted "
                        f"{result['inserted_parameters']} parameters and "
                        f"{result['inserted_tasks']} tasks; kept "
                        f"{result['skipped_parameters']} matching existing parameters and "
                        f"{result['skipped_tasks']} matching existing tasks")
                else:
                    summary = (
                        f'Compiled {len(compilation.parameters)} parameters and '
                        f'{len(compilation.tasks)} tasks')
                response.success = True
                response.message = f'{summary}; artifacts: {Path(destination).resolve()}'
                self.get_logger().info(response.message)
            except Exception as exc:
                response.success = False
                response.message = str(exc)
                self.get_logger().error(response.message)
            return response

    node = None
    exit_code = 0
    try:
        rclpy.init(args=args)
        node = ScenarioImporter()
        if node.get_parameter('import_on_start').value:
            response = node._handle(Trigger.Request(), Trigger.Response())
            exit_code = 0 if response.success else 1
            if not node.get_parameter('keep_alive').value:
                return exit_code
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except Exception as exc:
        exit_code = 1
        if node is not None:
            node.get_logger().error(str(exc))
        else:
            print(str(exc), file=sys.stderr)
    finally:
        try:
            if node is not None:
                node.destroy_node()
        finally:
            if rclpy.ok():
                rclpy.shutdown()
    return exit_code
