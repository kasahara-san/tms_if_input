"""Command-line conversion and optional MongoDB import (ROS not required)."""

import argparse
import json
import sys

from .compiler import compile_scenario, export_compilation


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--geojson', required=True, help='Input GeoJSON path')
    parser.add_argument('--xml', required=True, help='Input construction XML path')
    parser.add_argument('--output-dir', default='/tmp/tms_if_input', help='Artifact directory')
    parser.add_argument('--dry-run', action='store_true', help='Export without writing MongoDB')
    parser.add_argument('--mongo-uri', default='mongodb://localhost:27017')
    parser.add_argument('--mongo-db', default='rostmsdb')
    parser.add_argument('--import-key', default='default', help='Provenance label for new records')
    parser.add_argument('--mongo-timeout-ms', type=int, default=5000)
    args = parser.parse_args(argv)
    try:
        compilation = compile_scenario(args.geojson, args.xml)
        destination = export_compilation(compilation, args.output_dir)
        report = {'output_dir': str(destination.resolve()),
                  'parameter_count': len(compilation.parameters),
                  'task_count': len(compilation.tasks), 'dry_run': args.dry_run}
        if not args.dry_run:
            from .database import write_database
            report.update(write_database(
                compilation, mongo_uri=args.mongo_uri, mongo_db=args.mongo_db,
                import_key=args.import_key, timeout_ms=args.mongo_timeout_ms))
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f'tms_if_input: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
