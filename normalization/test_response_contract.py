"""Focused response-contract tests; no API calls or source-Skill execution."""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from check_record import check_record, strict_json_loads
from export_response_schema import response_schema

HERE = Path(__file__).resolve().parent
SCHEMA = strict_json_loads((HERE / 'skill-record.v0.1.1.schema.json').read_text(encoding='utf-8'))
ROWS = [strict_json_loads(line) for name in ('examples.v0.1.1.jsonl', 'pilot-records.jsonl')
        for line in (HERE / name).read_text(encoding='utf-8').splitlines() if line.strip()]
ROWS.append(strict_json_loads((HERE / 'tool-backed-pilot.json').read_text(encoding='utf-8')))
ROWS.append(strict_json_loads((HERE / 'api-design-pilot.json').read_text(encoding='utf-8')))


def payload(record):
    return {'content': deepcopy(record['content']), 'gaps': deepcopy(record['extraction']['gaps'])}


class ResponseContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.derived = response_schema(SCHEMA)
        cls.validator = Draft202012Validator(cls.derived)

    def test_all_supplied_payloads_validate(self):
        for record in ROWS:
            with self.subTest(name=record['content']['name']):
                self.validator.validate(payload(record))
                self.assertEqual(check_record(record, SCHEMA)['errors'], [])

    def test_derivation_does_not_mutate_record_schema(self):
        before = deepcopy(SCHEMA)
        response_schema(SCHEMA)
        self.assertEqual(SCHEMA, before)

    def test_extra_source_or_approval_keys_are_rejected(self):
        for key in ('source', 'extraction', 'review_status'):
            with self.subTest(key=key):
                item = payload(ROWS[0]); item[key] = {}
                self.assertFalse(self.validator.is_valid(item))

    def test_missing_semantic_block_is_rejected(self):
        for key in ('content', 'gaps'):
            item = payload(ROWS[0]); del item[key]
            self.assertFalse(self.validator.is_valid(item))

    def test_nested_task_shape_is_checked_through_ref(self):
        item = payload(ROWS[0]); del item['content']['tasks'][0]['goal']
        self.assertFalse(self.validator.is_valid(item))

    def test_nested_gap_shape_is_checked_through_ref(self):
        item = payload(ROWS[0]); item['gaps'] = [{'reason': 'unread_dependency'}]
        self.assertFalse(self.validator.is_valid(item))

    def test_strict_parsing_is_before_assembly(self):
        with self.assertRaisesRegex(ValueError, 'duplicate_json_key'):
            strict_json_loads('{"content": {}, "content": {}, "gaps": []}')

    def test_explicit_assembly_preserves_source_and_runs_full_checker(self):
        original = ROWS[-1]
        item = payload(original)
        self.validator.validate(item)
        # This is a local contract fixture: the context below comes from an already
        # provided example, NOT a fresh join with the user's collection index.
        assembled = {
            'schema_version': SCHEMA['properties']['schema_version']['const'],
            'source': deepcopy(original['source']),
            'content': item['content'],
            'extraction': {**deepcopy(original['extraction']), 'gaps': item['gaps']},
        }
        self.assertEqual(assembled, original)
        self.assertEqual(check_record(assembled, SCHEMA)['errors'], [])
        self.assertEqual(assembled['extraction']['review_status'], 'unreviewed')
        self.assertEqual(assembled['extraction']['execution_status'], 'not_executed')

    def test_payload_schema_does_not_replace_pointer_consistency_check(self):
        original = ROWS[0]
        item = payload(original)
        item['gaps'] = [{'target': '/content/tasks/9999', 'reason': 'other', 'detail': 'Fixture'}]
        self.validator.validate(item)
        assembled = deepcopy(original)
        assembled['content'] = item['content']; assembled['extraction']['gaps'] = item['gaps']
        self.assertTrue(any('gap_target_missing' in e for e in check_record(assembled, SCHEMA)['errors']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
