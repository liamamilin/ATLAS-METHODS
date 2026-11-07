"""Small offline consistency checker for ATLAS SkillRecord.

Usage: python check_record.py --schema skill-record.v0.1.1.schema.json examples.v0.1.1.jsonl
Requires Python 3.10+ and jsonschema. Does not download, install or execute a Skill.

Structural/consistency results are not semantic-quality verdicts. Pass source_texts
from a trusted fixed-commit reader when using the function to check line bounds.
This module does not independently verify Git provenance, actual model delivery,
or semantic fidelity. Schema remains 0.1.1; checker patch: converged.1.
"""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterator, Mapping
from jsonschema import Draft202012Validator, FormatChecker

CHECKER_VERSION = "converged.1"


def source_line_count(text: str) -> int:
    """Original file lines use LF boundaries; a final LF adds no phantom line."""
    return text.count("\n") + int(bool(text) and not text.endswith("\n"))


def is_repo_path(path: str) -> bool:
    """Evidence/read paths are canonical repo-relative paths, not commands/URLs."""
    return (bool(path.strip()) and not path.startswith("/")
            and "\\" not in path and not re.match(r"^[A-Za-z]:/", path)
            and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", path)
            and all(part not in {"", ".", ".."} for part in path.split("/"))
            and not any(ord(c) < 32 or ord(c) == 127 for c in path))


def walk(value: Any, path: str = '') -> Iterator[tuple[str, Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            token = str(key).replace('~', '~0').replace('/', '~1')
            yield from walk(child, path + '/' + token)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from walk(child, path + '/' + str(i))


def pointer_exists(record: dict, pointer: str) -> bool:
    if pointer == '':
        return True
    if not pointer.startswith('/') or re.search(r'~(?![01])', pointer):
        return False
    current: Any = record
    for encoded in pointer[1:].split('/'):
        key = encoded.replace('~1', '/').replace('~0', '~')
        if isinstance(current, dict) and key in current:
            current = current[key]
        elif isinstance(current, list) and re.fullmatch(r'0|[1-9][0-9]*', key) and int(key) < len(current):
            current = current[int(key)]
        else:
            return False
    return True


def interval_covered(start: int, end: int, ranges: list[dict]) -> bool:
    cursor = start
    for item in sorted(ranges, key=lambda x: (x['line_start'], x['line_end'])):
        a, b = item['line_start'], item['line_end']
        if b < cursor:
            continue
        if a > cursor:
            return False
        cursor = max(cursor, b + 1)
        if cursor > end:
            return True
    return False


def check_record(record: dict, schema: dict, source_texts: Mapping[str, str] | None = None) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for error in validator.iter_errors(record):
        path = '/' + '/'.join(map(str, error.absolute_path))
        errors.append(f'schema {path}: {error.message}')
    if errors:
        return {'errors': errors, 'warnings': warnings, 'checks_scope': 'schema; malformed record not inspected further'}

    # Whitespace-only metadata/content is not a supplied value. Preserve all other
    # whitespace verbatim, including in commands and code samples.
    for field_path, value in walk(record):
        if isinstance(value, str) and not value.strip():
            errors.append(f'blank_string: {field_path}; use null/[] only where permitted')

    content = record['content']
    extraction = record['extraction']
    reads = extraction['files_read']
    read_paths = [r['path'] for r in reads]
    read_map = {r['path']: r for r in reads}
    entry = record['source']['skill_path']
    if len(read_paths) != len(set(read_paths)):
        errors.append('duplicate_read_file: consolidate ranges for the same file')
    for path in set(read_paths) | {entry}:
        if not is_repo_path(path):
            errors.append(f'invalid_repository_path: {path!r}')
    if entry not in read_map:
        errors.append('entry_not_read: skill_path absent from actual read record')
    scope = 'entry_only' if set(read_paths) == {entry} else 'entry_and_references'
    if extraction['scope'] != scope:
        errors.append('scope_mismatch: scope disagrees with files_read')
    task_ids = [t['id'] for t in content['tasks']]
    if len(task_ids) != len(set(task_ids)):
        errors.append('duplicate_task_id')
    if not any(content[k] for k in ('tasks', 'rules', 'resources', 'notes')):
        warnings.append('empty_content: retain as a draft/problem record, not a completed substantive conversion')

    texts = source_texts if source_texts is not None else {}
    if not isinstance(texts, Mapping):
        errors.append('invalid_source_texts: expected a mapping of repository paths to complete source texts')
        texts = {}
    invalid_texts = [p for p in read_paths if p in texts and not isinstance(texts[p], str)]
    for p in invalid_texts:
        errors.append(f'invalid_source_text: {p}; expected a string containing the full fixed-commit file')
    texts = {p: t for p, t in texts.items() if isinstance(t, str)}
    missing_texts = sorted(set(read_paths) - texts.keys())
    if missing_texts:
        warnings.append('source_bytes_not_checked: no source text supplied for: ' + ', '.join(missing_texts)
                        + '; file-length checks skipped for these paths')
    for file in reads:
        path = file['path']
        line_count = source_line_count(texts[path]) if path in texts else None
        for interval in file.get('ranges', []):
            if interval['line_end'] < interval['line_start']:
                errors.append(f'read_range_reversed: {path}')
            elif line_count is not None and interval['line_end'] > line_count:
                errors.append(f'read_range_out_of_bounds: {path}')
    for node_path, node in walk(content, '/content'):
        if not isinstance(node, dict) or 'evidence' not in node:
            continue
        for evidence in node['evidence']:
            path = evidence['path']
            if not is_repo_path(path):
                errors.append(f'invalid_evidence_path: {node_path} -> {path!r}')
            file = read_map.get(path)
            if file is None:
                errors.append(f'evidence_unread: {node_path} -> {path}')
                continue
            if 'line_start' in evidence:
                start, end = evidence['line_start'], evidence['line_end']
                if end < start:
                    errors.append(f'evidence_range_reversed: {node_path} -> {path}')
                    continue
                if path in texts and end > source_line_count(texts[path]):
                    errors.append(f'evidence_out_of_bounds: {node_path} -> {path}')
                if file['coverage'] == 'partial' and not interval_covered(start, end, file.get('ranges', [])):
                    errors.append(f'evidence_outside_read_ranges: {node_path} -> {path}')
            else:
                warnings.append(f'section_locator_unresolved: {node_path} -> {path}; use line ranges or resolve the section against the snapshot')
    for gap in extraction['gaps']:
        if not pointer_exists(record, gap['target']):
            errors.append(f'gap_target_missing: {gap["target"]}')
    return {'errors': list(dict.fromkeys(errors)), 'warnings': list(dict.fromkeys(warnings)),
            'checks_scope': ('structure and declared-read metadata only; line bounds checked only for supplied full-file texts; '
                             'Git/index provenance, actual delivery, semantic fidelity and skill execution not checked')}


def strict_json_loads(text: str) -> Any:
    """Reject repeated object keys instead of silently discarding source content."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate_json_key: {key}')
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f'non_json_constant: {value}')

    value = json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    for _, node in walk(value):
        strings = [node] if isinstance(node, str) else (list(node) if isinstance(node, dict) else [])
        for string in strings:
            try:
                string.encode('utf-8')
            except UnicodeEncodeError as exc:
                raise ValueError('invalid_unicode: unpaired surrogate in JSON text') from exc
    return value


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('records', type=Path, help='One SkillRecord per JSONL line')
    p.add_argument('--schema', type=Path, default=Path(__file__).with_name('skill-record.v0.1.1.schema.json'))
    args = p.parse_args()
    schema = strict_json_loads(args.schema.read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    failed = 0
    checked = 0
    # Decode each physical JSONL line separately so one corrupt UTF-8 record
    # cannot prevent later independent records from being inspected.
    with args.records.open('rb') as f:
        for number, raw_line in enumerate(f, 1):
            if not raw_line.strip():
                continue
            checked += 1
            try:
                line = raw_line.decode('utf-8')
                record = strict_json_loads(line)
                result = check_record(record, schema)
            except (ValueError, RecursionError) as exc:
                result = {'errors': [f'{type(exc).__name__}: {exc}'], 'warnings': []}
            failed += bool(result['errors'])
            # Escaping diagnostics keeps malformed Unicode from breaking output.
            print(json.dumps({'line': number, **result}, ensure_ascii=True))
    if checked == 0:
        print(json.dumps({'line': None, 'errors': ['empty_input: no JSONL records were checked'], 'warnings': []}))
        return 1
    return int(bool(failed))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        raise SystemExit(f'ERROR: {exc}') from exc
