import os
from setuptools import find_packages, setup
from glob import glob

package_name = 'tms_if_input'

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'launch'), glob('launch/*.launch.py')),
        (os.path.join('share', package_name, 'json_samples'),
         glob('json_samples/*.geojson') + glob('json_samples/*.xml')),
    ],
    install_requires=['setuptools', 'pymongo'],
    zip_safe=True,
    maintainer='common',
    maintainer_email='kasahara.yuichiro.res@gmail.com',
    description='Compile construction GeoJSON/XML scenarios for ROS2-TMS and MongoDB',
    license='TODO: License declaration',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'scenario_importer = tms_if_input.importer:main',
            'compile_scenario = tms_if_input.cli:main',
        ],
    },
)
