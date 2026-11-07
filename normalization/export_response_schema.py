"""Derive the model-response contract from SkillRecord v0.1.1.

This is a projection for local JSON validation, not a second independently edited
schema and not a claim of compatibility with any provider's structured-output API.
No network requests or source-Skill execution are performed.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from check_record import strict_json_loads


def response_schema(record_schema: dict[str, Any]) -> dict[str, Any]:
    """Keep content/gaps and their local $defs references in one document.

    Intended for the current ATLAS schema layout, not arbitrary schema bundling.
    If the source schema later uses other reference layouts, recheck this adapter.
    """
    Draft202012Validator.check_schema(record_schema)
    properties = record_schema['properties']
    result = {
        '$schema': record_schema['$schema'],
        'title': 'ATLAS semantic response (derived; do not edit separately)',
        'type': 'object',
        'properties': {
            'content': deepcopy(properties['content']),
            'gaps': deepcopy(properties['extraction']['properties']['gaps']),
        },
        'required': ['content', 'gaps'],
        'additionalProperties': False,
        '$defs': deepcopy(record_schema.get('$defs', {})),
    }
    Draft202012Validator.check_schema(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schema', type=Path,
                        default=Path(__file__).with_name('skill-record.v0.1.1.schema.json'))
    args = parser.parse_args()
    source = strict_json_loads(args.schema.read_text(encoding='utf-8'))
    print(json.dumps(response_schema(source), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
