"""ROS entry point for importing one replaceable pair of construction files."""

from pathlib import Path

from .compiler import compile_scenario, export_compilation
from .database import write_database


def main(args=None) -> None:
    # Keep conversion, tests and dry-run CLI usable without a sourced ROS setup.
    import rclpy
    from rclpy.node import Node
    from std_srvs.srv import Trigger

    class ScenarioImporter(Node):
        def __init__(self):
            super().__init__('scenario_importer')
            for name, default in {
                'geojson_path': '', 'xml_path': '', 'output_dir': '/tmp/tms_if_input',
                'mongo_uri': 'mongodb://localhost:27017', 'mongo_db': 'rostmsdb',
                'import_key': 'default', 'mongo_timeout_ms': 5000,
                'dry_run': False, 'import_on_start': True,
            }.items():
                self.declare_parameter(name, default)
            self._service = self.create_service(Trigger, '~/import', self._handle)
            self._timer = None
            if self.get_parameter('import_on_start').value:
                self._timer = self.create_timer(0.1, self._start)

        def _start(self):
            self._timer.cancel()
            response = self._handle(Trigger.Request(), Trigger.Response())
            if not response.success:
                self.get_logger().error(response.message)

        def _handle(self, request, response):
            values = {name: self.get_parameter(name).value for name in (
                'geojson_path', 'xml_path', 'output_dir', 'mongo_uri', 'mongo_db',
                'import_key', 'mongo_timeout_ms', 'dry_run')}
            try:
                if not values['geojson_path'] or not values['xml_path']:
                    raise ValueError('Both geojson_path and xml_path are required')
                compilation = compile_scenario(values['geojson_path'], values['xml_path'])
                destination = export_compilation(compilation, values['output_dir'])
                if not values['dry_run']:
                    result = write_database(
                        compilation, mongo_uri=values['mongo_uri'],
                        mongo_db=values['mongo_db'], import_key=values['import_key'],
                        timeout_ms=values['mongo_timeout_ms'])
                    summary = (
                        f"Inserted {result['inserted_parameters']} parameters and "
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

    rclpy.init(args=args)
    node = ScenarioImporter()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
