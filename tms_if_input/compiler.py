"""Compile replaceable GeoJSON/XML pairs into MongoDB documents."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import tempfile

from .behavior_tree import build_task_documents
from .model import load_inputs
from .parameters import build_parameter_documents


@dataclass(frozen=True)
class Compilation:
    parameters: list[dict]
    tasks: list[dict]


def _identity(document: dict) -> tuple:
    model = document.get('model_name')
    return document['record_name'], json.dumps(model, ensure_ascii=False, sort_keys=True)


def validate_documents(compilation: Compilation) -> None:
    """Reject conflicting records before creating any artifacts or DB writes."""
    seen = set()
    for document in compilation.parameters:
        key = _identity(document)
        if key in seen:
            raise ValueError(f'Conflicting parameter record: {key}')
        seen.add(key)
    models = [task['model_name'] for task in compilation.tasks]
    if len(models) != len(set(models)):
        raise ValueError('Compiled task documents contain duplicate machine models')
    # Also verifies that no NaN/Infinity can reach MongoDB through input JSON.
    json.dumps({'parameters': compilation.parameters, 'tasks': compilation.tasks},
               allow_nan=False)


def compile_scenario(geojson_path, xml_path) -> Compilation:
    plan, graph = load_inputs(geojson_path, xml_path)
    result = Compilation(build_parameter_documents(plan, graph), build_task_documents(plan))
    validate_documents(result)
    return result


def _atomic_write(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.' + path.name, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
            stream.flush()
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def export_compilation(compilation: Compilation, output_dir) -> Path:
    """Write reviewable documents and one behavior-tree XML per machine."""
    validate_documents(compilation)
    for task in compilation.tasks:
        name = task['description']
        if Path(name).name != name or name in {'.', '..'}:
            raise ValueError(f'Unsafe output filename: {name!r}')
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    previous = []
    previous_path = destination / 'tasks.json'
    if previous_path.is_file():
        try:
            previous = json.loads(previous_path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            pass
    if not isinstance(previous, list):
        previous = []
    _atomic_write(destination / 'parameters.json',
                  json.dumps(compilation.parameters, ensure_ascii=False, indent=2,
                             allow_nan=False) + '\n')
    _atomic_write(destination / 'tasks.json',
                  json.dumps(compilation.tasks, ensure_ascii=False, indent=2,
                             allow_nan=False) + '\n')
    for task in compilation.tasks:
        _atomic_write(destination / task['description'], task['task_sequence'] + '\n')
    active_names = {task['description'] for task in compilation.tasks}
    for old in previous:
        if not isinstance(old, dict):
            continue
        name = old.get('description')
        sequence = old.get('task_sequence')
        if (not isinstance(name, str) or Path(name).name != name or
                not isinstance(sequence, str) or
                not name.endswith('_task.xml') or name in active_names):
            continue
        path = destination / name
        # Only delete stale XMLs whose contents still match our prior export.
        # User edits and other files in the output directory are preserved.
        try:
            if path.is_file() and path.read_text(encoding='utf-8') == sequence + '\n':
                path.unlink()
        except (OSError, UnicodeError):
            pass
    return destination
