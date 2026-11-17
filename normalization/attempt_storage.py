"""Space-efficient, lossless local evidence for normalization attempts."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path, data):
    """Durable publication before removing any redundant evidence elsewhere."""
    path = Path(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
        _sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def save_request(attempt, prompt):
    """Keep immutable request.txt paths, sharing only byte-identical requests.

    The pool belongs to this batch, not a global source-identity database.
    Never open an existing request for writing: retries get a new attempt path.
    """
    data = prompt.encode('utf-8')
    pool = attempt.parents[2] / '.requests'
    pool.mkdir(exist_ok=True)
    blob = pool / (hashlib.sha256(data).hexdigest() + '.txt')
    if blob.exists():
        if blob.is_symlink() or blob.read_bytes() != data:
            raise ValueError('Request pool integrity mismatch')
        os.link(blob, attempt / 'request.txt')
        _sync_directory(attempt)
        return
    with tempfile.NamedTemporaryFile(dir=pool, delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), 0o444)
            os.fsync(stream.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        try:
            os.link(temp, blob)
            _sync_directory(pool)
        except FileExistsError:
            pass
        if blob.is_symlink() or blob.read_bytes() != data:
            raise ValueError('Request pool integrity mismatch')
        os.link(blob, attempt / 'request.txt')
        _sync_directory(attempt)
    finally:
        temp.unlink(missing_ok=True)


def archive_session(attempt, raw):
    path = attempt / 'session.json.gz'
    atomic_bytes(path, gzip.compress(raw, compresslevel=6, mtime=0))
    if gzip.decompress(path.read_bytes()) != raw:
        raise ValueError('Session archive verification failed')
    return {'path': path.name, 'sha256': hashlib.sha256(raw).hexdigest(),
            'bytes': len(raw), 'compressed_bytes': path.stat().st_size}


def release_session(attempt, session, sid, work, executable, env, archive):
    """Release only this completed tool-free conversion's redundant DB copy.

    Every refusal or CLI failure keeps the DB copy. Existing historical sessions
    are never enumerated or removed. The full export remains importable locally.
    """
    info = session.get('info', {})
    times = info.get('time', {})
    receipt = {'archive': archive, 'session_id': sid, 'database_copy': 'retained'}
    path = attempt / 'session-storage.json'
    safe = (info.get('id') == sid and info.get('title') == 'ATLAS SkillRecord conversion'
            and not info.get('parentID') and not info.get('parent_id')
            and info.get('location', {}).get('directory') == str(work)
            and isinstance(times.get('idle'), (int, float))
            and isinstance(times.get('busy'), (int, float))
            and times['idle'] >= times['busy'])
    if not safe:
        receipt['reason'] = 'Export does not prove this private conversion is idle'
    # Persist identity and verified archive hash before the destructive CLI call.
    atomic_bytes(path, json.dumps(receipt, indent=2).encode())
    if safe:
        try:
            deleted = subprocess.run([executable, 'session', 'delete', '--standalone', sid],
                                     cwd=work, env=env, stdin=subprocess.DEVNULL,
                                     capture_output=True, timeout=60)
            if deleted.returncode == 0:
                receipt['database_copy'] = 'deleted'
            else:
                receipt['reason'] = f'Session delete exited {deleted.returncode}'
        except (OSError, subprocess.SubprocessError) as exc:
            # A CLI timeout does not prove whether the server completed deletion.
            receipt['database_copy'] = 'unknown'
            receipt['reason'] = type(exc).__name__
        try:
            atomic_bytes(path, json.dumps(receipt, indent=2).encode())
        except OSError as exc:
            receipt['receipt_write_error'] = type(exc).__name__
    return receipt
