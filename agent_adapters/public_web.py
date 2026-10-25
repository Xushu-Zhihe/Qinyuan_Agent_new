"""Provider-neutral public-reference tools; first backend is DeepSeek Search/Fetch."""

import asyncio
import http.client
import ipaddress
import json
import os
import socket
import threading
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.handoff_contracts import D8Capacity
from agent_core.turn_modeling import digest

MAX_SECONDS = 30
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PREVIEW_BYTES = D8Capacity().read_return
IDENTITY = 'public_reference_not_target_observation'
SPECS = [
    dict(qualified_name='web.search', tags=['public-reference', 'search'],
         description='Resolve a public technical knowledge gap using original references when it can change the next decision; results do not prove current target behavior.',
         input_schema=dict(type='object', properties={
             'query': dict(type='string', minLength=1, maxLength=128),
             'count': dict(type='integer', minimum=1, maximum=50)}, required=['query'], additionalProperties=False)),
    dict(qualified_name='web.fetch', tags=['public-reference', 'fetch'],
         description='Read original public documentation behind a relevant reference to check its exact claim; no private/current-target URLs or target experiments.',
         input_schema=dict(type='object', properties={
             'url': dict(type='string', minLength=1, maxLength=8192)},
             required=['url'], additionalProperties=False))]
NAMES = frozenset(s['qualified_name'] for s in SPECS)


def preview(payload, identity, claim):
    raw = payload.get('stdout', '').encode('utf-8')
    if len(raw) <= MAX_PREVIEW_BYTES:
        return payload
    return {**payload, 'stdout': raw[:MAX_PREVIEW_BYTES].decode('utf-8', errors='ignore'),
            'truncated': True, 'coverage': 'preview_only_full_source_preserved',
            'details_ref': f"progress:{identity}:{digest(dict(claim=claim, result=payload))}"}


def document_text(payload):
    """Readable fields of the validated provider documents; never unescape source text."""
    if payload.get('status') in {'failed', 'rejected'}:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    documents = json.loads(payload['stdout'])['documents']
    return '\n\n'.join('\n'.join(f"{key}: {value}" for key, value in document.items())
                        for document in documents)


class RetrievalError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _url(value):
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').rstrip('.').encode('idna').decode('ascii').lower()
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == 'https' else 80
        if (not isinstance(value, str) or not value or len(value) > 8192
                or any(ord(c) <= 32 or ord(c) == 127 for c in value) or '\\' in value
                or parsed.scheme not in {'http', 'https'} or not host or '%' in host
                or parsed.username is not None or parsed.password is not None
                or not 1 <= port <= 65535):
            raise ValueError()
    except (ValueError, UnicodeError, AttributeError):
        raise RetrievalError('retrieval.url_invalid') from None
    return host, port


def public_url(value, target=None, *, resolve=socket.getaddrinfo):
    """Check the initial URL locally; provider-side redirects/DNS are not controlled."""
    host, port = _url(value)
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        raise RetrievalError('retrieval.url_not_public')
    addresses = {ipaddress.ip_address(row[4][0]) for row in resolve(host, port, type=socket.SOCK_STREAM)}
    if not addresses or any(not ip.is_global or ip.is_multicast for ip in addresses):
        raise RetrievalError('retrieval.url_not_public')
    if target:
        other, other_port = _url(target)
        if host == other:
            raise RetrievalError('retrieval.target_url_forbidden')
        targets = {ipaddress.ip_address(row[4][0]) for row in resolve(other, other_port, type=socket.SOCK_STREAM)}
        if addresses & targets:
            raise RetrievalError('retrieval.target_url_forbidden')


def _failure(tool, code, category, *, http_status=None):
    return dict(ok=False, status='rejected' if category == 'policy' else 'failed', tool=tool,
                identity=IDENTITY, code=code, category=category, http_status=http_status,
                stdout='', stderr='', exit_code=None, truncated=False)


def _response(tool, args, status, raw, key):
    if not 200 <= status < 300:
        category = ({401: 'authentication', 402: 'billing', 403: 'policy',
                     408: 'timeout', 413: 'capacity', 429: 'rate_limit'}.get(status)
                    or ('provider' if status >= 500 else 'request'))
        return _failure(tool, f'retrieval.http_{status}', category, http_status=status)
    if len(raw) > MAX_RESPONSE_BYTES:
        return _failure(tool, 'retrieval.response_too_large', 'capacity', http_status=status)
    if key.encode() in raw:
        return _failure(tool, 'retrieval.sensitive_response', 'policy', http_status=status)
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        if tool == 'web.search':
            docs = value['results']
            if not isinstance(docs, list) or any(not isinstance(d, dict) or
                    not isinstance(d.get('url'), str) or not isinstance(d.get('title'), str)
                    or any(not isinstance(d.get(k, ''), str) for k in ('snippet', 'summary', 'content')) for d in docs):
                raise ValueError()
            documents = [{k: d.get(k, '') for k in ('url', 'title', 'snippet', 'summary', 'content', 'date')} for d in docs]
        else:
            if not isinstance(value['title'], str) or not isinstance(value['content'], str):
                raise ValueError()
            documents = [dict(url=args['url'], title=value['title'], content=value['content'])]
        stdout = json.dumps(dict(documents=documents), ensure_ascii=False, allow_nan=False)
        if key in stdout:
            return _failure(tool, 'retrieval.sensitive_response', 'policy', http_status=status)
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        return _failure(tool, 'retrieval.response_invalid', 'format', http_status=status)
    return dict(ok=True, status='completed', tool=tool, identity=IDENTITY,
                http_status=status, stdout=stdout, stderr='', exit_code=0, truncated=False)


def _worker(channel, settings, payload, timeout):
    """Fixed official API only, in the existing cancellable transport child process."""
    client = None
    tool, args = payload['tool'], payload['arguments']
    try:
        if tool == 'web.fetch':
            public_url(args['url'], payload['target'])
        client = http.client.HTTPSConnection('api.deepseek.com', timeout=timeout)
        client.connect()
        channel.send({'connected': True})
        client.request('POST', '/v1/tools/' + ('search' if tool == 'web.search' else 'fetch'),
                       body=json.dumps(args, ensure_ascii=False, allow_nan=False).encode('utf-8'),
                       headers={'Authorization': 'Bearer ' + settings.api_key, 'Content-Type': 'application/json'})
        response = client.getresponse()
        if 300 <= response.status < 400:
            result = _failure(tool, 'retrieval.api_redirect_rejected', 'policy', http_status=response.status)
        else:
            result = _response(tool, args, response.status, response.read(MAX_RESPONSE_BYTES + 1), settings.api_key)
        channel.send({'payload': result})
    except RetrievalError as error:
        channel.send({'payload': _failure(tool, error.code, 'policy')})
    except (TimeoutError, socket.timeout):
        channel.send({'payload': _failure(tool, 'retrieval.timeout', 'timeout')})
    except (OSError, http.client.HTTPException):
        channel.send({'payload': _failure(tool, 'retrieval.transport_failed', 'network')})
    finally:
        if client is not None:
            client.close()
        channel.close()


@dataclass(repr=False)
class DeepSeekRetrieval:
    api_key: str = field(repr=False)
    binding = dict(provider='deepseek', endpoint='https://api.deepseek.com/v1/tools')

    def invoke(self, call, timeout, token, target):
        from agent_adapters.deepseek_model import DeepSeekModelAdapter, DeepSeekSettings
        # Reuse process cancellation/exit confirmation; no model name is sent to Search/Fetch.
        transport = DeepSeekModelAdapter(DeepSeekSettings('retrieval', self.api_key,
                                         timeout_seconds=MAX_SECONDS), worker=_worker)
        detail = {}
        result = transport._invoke_payload(dict(tool=call.tool_name, arguments=dict(call.arguments), target=target),
                                           timeout, token, diagnostic=detail)
        if not detail.get('worker_exit_confirmed'):
            raise D3RecoveryRequired('retrieval.worker_stop_unconfirmed')
        if isinstance(result, ModelError):
            if detail.get('transport_end') == 'worker_lost':
                raise D3RecoveryRequired('retrieval.outcome_unknown')
            category = ('cancelled' if result.kind == ModelErrorKind.CANCELLED else
                        'timeout' if result.kind == ModelErrorKind.TIMED_OUT else 'network')
            result = _failure(call.tool_name, 'retrieval.' + category, category)
        return {**result, 'provider': self.binding['provider']}


class PublicWeb:
    """Generic tool policy/async adapter; a replacement backend keeps these tool schemas."""
    def __init__(self, backend):
        self.backend = backend
        self.binding = backend.binding

    def validate(self, call):
        from agent_core.output_contracts import decode_response
        spec = next((s for s in SPECS if s['qualified_name'] == call.tool_name), None)
        if spec is None:
            raise ValueError('retrieval.tool_invalid')
        _, issues, terminal, _ = decode_response(dict(call.arguments), spec['input_schema'])
        text = call.arguments.get('query', call.arguments.get('url', ''))
        if issues or terminal or not text.strip() or '\x00' in text:
            raise ValueError('retrieval.arguments_invalid')
        key = getattr(self.backend, 'api_key', None)
        if key and key in json.dumps(dict(call.arguments), ensure_ascii=False):
            raise ValueError('retrieval.sensitive_request')
        if call.tool_name == 'web.fetch':
            _url(text)

    async def call(self, call, timeout, token, target=None):
        self.validate(call)
        stopped = threading.Event()
        class InvocationToken:
            @property
            def is_cancelled(self):
                return stopped.is_set() or token.is_cancelled
            @property
            def is_timed_out(self):
                return getattr(token, 'is_timed_out', False)
        pending = asyncio.create_task(asyncio.to_thread(self.backend.invoke, call,
                                    min(timeout, MAX_SECONDS), InvocationToken(), target))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            from agent_core.dual_llm_modeling import MODEL_CANCEL_GRACE_SECONDS
            stopped.set()
            try:
                return await asyncio.wait_for(asyncio.shield(pending), MODEL_CANCEL_GRACE_SECONDS)
            except asyncio.TimeoutError:
                raise D3RecoveryRequired('retrieval.worker_stop_unconfirmed') from None


def from_env_file(path, model_settings=None):
    """Credentials remain separate from model selection and all durable bindings."""
    from dotenv import dotenv_values
    values = dotenv_values(path, interpolate=False)
    key = os.environ.get('QINYUAN_SEARCH_API_KEY') or values.get('QINYUAN_SEARCH_API_KEY')
    if not key and model_settings is not None:
        parsed = urlsplit(model_settings.base_url)
        if (parsed.scheme == 'https' and parsed.hostname == 'api.deepseek.com'
                and parsed.netloc in {'api.deepseek.com', 'api.deepseek.com:443'}
                and parsed.path.rstrip('/') in {'', '/v1'} and not parsed.query and not parsed.fragment
                and parsed.username is None and parsed.password is None):
            key = model_settings.api_key
    if not key:
        return None
    if not isinstance(key, str) or not key.strip() or not key.isascii() or any(ord(c) < 32 for c in key):
        raise ValueError('retrieval.credentials_invalid')
    return PublicWeb(DeepSeekRetrieval(key))
