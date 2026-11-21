#!/usr/bin/env python3
"""Preview, apply, or roll back local catalogue classification overrides.

The default is a read-only diff. --apply writes data/library/classification-overrides.json
and does not rebuild the catalogue, edit SkillRecords, or export the public site.
"""
import argparse
import json
from pathlib import Path
import sys

from classification_overrides import (
    DATABASE, NOTE, OVERRIDES, apply_plan, apply_rollback, catalogue_stamp, catalogue_topics,
    connect_readonly, load_overrides, load_proposal, plan_import, plan_rollback, public_changes,
    report_counts, save_overrides, snapshot_notice,
)


def emit(payload, code):
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write('\n')
    return code


def run(argv=None):
    parser = argparse.ArgumentParser(description='预览或写入本机目录分类覆盖层。默认只读，不重建目录，不公开导出。')
    parser.add_argument('--proposals', type=Path, help='分类提案 JSON。不要和 --rollback 一起用')
    parser.add_argument('--database', type=Path, default=DATABASE)
    parser.add_argument('--overrides', type=Path, default=OVERRIDES)
    parser.add_argument('--apply', action='store_true', help='写入覆盖层。省略时只打印差异')
    parser.add_argument('--rollback', action='store_true', help='移除一个导入批次里未经改动的覆盖')
    parser.add_argument('--batch', default='', help='回滚用的 batch_id，或至少 8 位且唯一的前缀')
    args = parser.parse_args(argv)
    if args.rollback == bool(args.proposals):
        return emit({'error': '请只选择 --proposals 或 --rollback 其中之一'}, 2)
    try:
        store = load_overrides(args.overrides)
    except ValueError as exc:
        return emit({'error': str(exc)}, 2)
    if args.rollback:
        if not args.batch:
            return emit({'error': '回滚需要 --batch'}, 2)
        try:
            rollback = plan_rollback(store, args.batch)
        except ValueError as exc:
            return emit({'error': str(exc), 'note': NOTE}, 1)
        wrote = False
        if args.apply:
            wrote = save_overrides(args.overrides, apply_rollback(store, rollback))
        return emit({
            'mode': 'rollback-apply' if args.apply else 'rollback-dry-run',
            'wrote': bool(wrote), 'overrides': str(args.overrides), 'note': NOTE,
            'next': 'python3 library/build.py' if args.apply else None, **rollback,
        }, 0)
    try:
        proposal = load_proposal(args.proposals)
        connection = connect_readonly(args.database)
    except (OSError, ValueError) as exc:
        return emit({'error': str(exc)}, 2)
    try:
        plan = plan_import(proposal, connection, store, catalogue_topics(args.database))
        stamp = catalogue_stamp(connection)
    finally:
        connection.close()
    counts = report_counts(plan)
    wrote = False
    if args.apply and counts['apply']:
        wrote = save_overrides(args.overrides, apply_plan(store, plan))
    return emit({
        'mode': 'apply' if args.apply else 'dry-run',
        'wrote': bool(wrote),
        'batch_id': proposal['batch_id'],
        'proposal_catalog_as_of': proposal.get('catalog_as_of'),
        'catalogue_as_of': stamp,
        'snapshot_notice': snapshot_notice(proposal.get('catalog_as_of'), stamp),
        'counts': counts,
        'changes': public_changes(plan),
        'pending_rebuild': plan['pending_rebuild'],
        'conflicts': plan['conflicts'],
        'retained': plan['retained'],
        'overrides': str(args.overrides),
        'note': NOTE,
        'next': 'python3 library/build.py' if wrote or counts['pending_rebuild'] else None,
    }, 1 if counts['conflicts'] else 0)


if __name__ == '__main__':
    sys.exit(run())
