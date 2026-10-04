"""Import a GeoJSON/XML pair into rostmsdb and export reviewable artifacts."""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition, UnlessCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    samples = os.path.join(get_package_share_directory('tms_if_input'), 'json_samples')
    defaults = {
        'geojson_path': '261001-kyoto.geojson',
        'xml_path': '261001-kyoto.xml',
        'output_dir': '/tmp/tms_if_input',
        'mongo_uri': 'mongodb://localhost:27017',
        'mongo_db': 'rostmsdb',
        'mongo_timeout_ms': '5000',
        'dry_run': 'false',
        'import_on_start': 'true',
        'keep_alive': 'false',
        'prefix': 'tms_if_input',
        'use_namespace': 'true',
        'use_sim_time': 'false',
    }
    descriptions = {
        'geojson_path': 'GeoJSON filename in json_samples (or an absolute path)',
        'xml_path': 'XML filename in json_samples (or an absolute path)',
        'keep_alive': 'Keep the import service running after the initial import',
    }
    arguments = [DeclareLaunchArgument(name, default_value=value,
                                       description=descriptions.get(name, ''))
                 for name, value in defaults.items()]
    parameters = {}
    for name in defaults:
        if name in {'prefix', 'use_namespace'}:
            continue
        value_type = bool if name in {'dry_run', 'import_on_start', 'keep_alive', 'use_sim_time'} else (
            int if name == 'mongo_timeout_ms' else str)
        value = LaunchConfiguration(name)
        if name in {'geojson_path', 'xml_path'}:
            value = PathJoinSubstitution([samples, value])
        parameters[name] = ParameterValue(value, value_type=value_type)
    return LaunchDescription(arguments + [
        Node(package='tms_if_input', executable='scenario_importer',
             namespace=LaunchConfiguration('prefix'), parameters=[parameters],
             condition=IfCondition(LaunchConfiguration('use_namespace')), output='screen'),
        Node(package='tms_if_input', executable='scenario_importer',
             parameters=[parameters],
             condition=UnlessCondition(LaunchConfiguration('use_namespace')), output='screen'),
    ])
