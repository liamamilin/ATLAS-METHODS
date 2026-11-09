"""Use the user's existing Atlas model connection for an explicit Luna fallback."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


LUNA = 'openai/gpt-6-luna'


class AtlasLunaClient:
    def __init__(self, timeout):
        self.timeout = timeout
        self.pack = Path(os.environ.get('ATLAS_PACK', '')).expanduser().resolve()
        self.config = Path(os.environ.get('ATLAS_MODEL_CONFIG', '')).expanduser().resolve()
        if not (self.pack / 'atlas_model.py').is_file() or not self.config.is_file():
            raise ValueError('--luna-fallback requires ATLAS_PACK and ATLAS_MODEL_CONFIG')

    def call(self, prompt, attempt):
        session = 'atlas-normalization-' + uuid.uuid4().hex
        reply = {'model': LUNA, 'producer': 'atlas_model/' + LUNA, 'session_id': session,
                 'usage': {'available': False, 'source': 'atlas_model.complete'}}
        try:
            process = subprocess.run(
                [sys.executable, '-B', str(Path(__file__).resolve()), str(self.timeout), session],
                cwd=self.pack, input=prompt, capture_output=True, encoding='utf-8',
                timeout=self.timeout + 15, check=True,
                env={**os.environ, 'PWD': str(self.pack), 'ATLAS_PACK': str(self.pack),
                     'ATLAS_MODEL_CONFIG': str(self.config)},
            )
            result = json.loads(process.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return {**reply, 'transport_error': 'Atlas Luna request could not complete'}
        if 'error' in result:
            error = result['error']
            reply.update(transport_error='Atlas Luna request failed: ' + error['code'],
                         upstream_status=error['upstream_status'], input_error=error['input_error'],
                         content_filtered=error['content_filtered'])
            if error['code'] in ('quota_exhausted', 'rate_limited') and not reply['content_filtered']:
                reply['limit'] = {'kind': 'quota' if error['code'] == 'quota_exhausted' else 'rate',
                                  'code': error['code']}
            return reply
        model = result.get('model', '')
        if result.get('provider') != 'openai' or not (model == 'gpt-6-luna' or model.startswith('gpt-6-luna-')):
            return {**reply, 'transport_error': 'Atlas Luna returned an unexpected provider or model'}
        text = result['text']
        (attempt / 'response.txt').write_text(text, encoding='utf-8')
        model = 'openai/' + model
        usage = result.get('usage', {})
        tokens = {'input': usage.get('prompt_tokens'), 'output': usage.get('completion_tokens')}
        if any(value is not None for value in tokens.values()):
            reply['usage'].update(available=True, total_tokens=usage.get('total_tokens'),
                                  turns=[{'model': model, 'tokens': tokens}])
        return {**reply, 'text': text, 'model': model, 'producer': 'atlas_model/' + model}


def complete_from_pack(timeout, session):
    sys.path.insert(0, str(Path.cwd()))
    import atlas_model

    try:
        return atlas_model.complete([{'role': 'user', 'content': sys.stdin.read()}],
                                    provider='openai', model='gpt-6-luna', thinking=False,
                                    timeout=timeout, session_id=session)
    except atlas_model.ModelConfigError as exc:
        message = str(exc).lower()
        return {'error': {'code': exc.code, 'upstream_status': exc.upstream_status,
                          'input_error': any(term in message for term in (
                              'context_length_exceeded', 'context length', 'context window')),
                          'content_filtered': any(term in message for term in (
                              'content_filter', 'content_policy_violation'))}}


if __name__ == '__main__':
    print(json.dumps(complete_from_pack(int(sys.argv[1]), sys.argv[2])))
