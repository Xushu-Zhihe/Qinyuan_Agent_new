"""Real stage/evidence composition with controlled binary IO and provider input."""
import asyncio
import base64
import io
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from test_stage_application import case
from test_candidate_evidence import _TestOnlyProtection
from agent_adapters.image_observations import ImageObservations, attach_images
from agent_adapters.web import http_adapter
from MCPs.Mcp_client.client import MCPExecutionResult
from Sandbox.image_readback import validate_image_path


def png(size=(400, 150)):
    output = io.BytesIO()
    Image.new('RGB', size, (17, 103, 219)).save(output, format='PNG')
    return output.getvalue()


@pytest.mark.parametrize('path', ['/etc/passwd', '/workspace/work/../secret', '/workspace/work/a\\b',
    '/workspace/input//a', 'https://example.test/a.png'])
def test_image_path_rejects_non_workspace_and_ambiguous_paths(path):
    with pytest.raises(ValueError, match='image.path_rejected'):
        validate_image_path(path)


def test_image_pixels_reach_inner_and_outer_without_text_or_ledger_base64(case, monkeypatch):
    from agent_adapters.deepseek_roles import build_payload, role_config, EstimatedCapacity, model_capacity_policy, TRANSPORT
    from agent_core.output_contracts import D8OutputRequest, output_schema
    monkeypatch.setattr(http_adapter, 'WindowsEvidenceProtection', _TestOnlyProtection)
    original = png()
    tools = case.tools
    tools.specs = [{'qualified_name': 'image.read_image', 'description': 'Read actual pixels', 'tags': ['image'],
        'input_schema': {'type': 'object', 'properties': {'path': {'type': 'string'}},
                         'required': ['path'], 'additionalProperties': False}}]
    tools.list_registered_tools = lambda: [SimpleNamespace(qualified_name='image.read_image')]
    tools.bind_agent = lambda *a, **k: tools
    async def read(name, args):
        tools.calls.append((name, args))
        return MCPExecutionResult(case.app.task_id, 'controlled-sandbox', name, 'image_read', (),
            'completed', 0, '', '', False, image_bytes=original)
    tools.call_tool = read
    seen = []
    requests = []
    def inspect(request, raw):
        if request.stage == 'stage_action' and raw.get('kind') == 'mcp_call':
            return dict(kind='mcp_call', tool_name='image.read_image', arguments=dict(path='/workspace/work/symbol.png'))
        images = (request.execution_context or {}).get('stage_runtime', {}).get('images')
        if images:
            from agent_core.stage_planning import INNER_STAGES
            role = role_config('inner_action' if request.stage in INNER_STAGES else 'outer_planning')
            request = replace(request, role_config_ref=role.role_config_ref)
            stage, schema = output_schema(request)
            envelope = D8OutputRequest(request, {'version': 1, 'stage': stage, 'schema': schema}, 'Read evidence.', None)
            if request.stage == 'stage_action':
                requests.append((envelope, role))
            loader = ImageObservations(case.app.directory, case.app.task_id, case.app.evidence).load
            payload = build_payload(envelope, role, image_loader=loader)
            parts = payload['messages'][-1]['content']
            assert parts[0]['type'] == 'text' and parts[1]['type'] == 'image_url'
            assert base64.b64decode(parts[1]['image_url']['url'].split(',', 1)[1]) == original
            assert payload['model'] == 'deepseek-flash' and payload['reasoning_effort'] == 'high'
            assert 'base64,' not in json.dumps(request.execution_context)
            capacity = EstimatedCapacity(role, role)
            capacity.image_loader = loader
            assert capacity.fits(envelope, model_capacity_policy(role.model, TRANSPORT['base_url']))
            seen.append(request.stage)
        return raw
    errors = []
    def checked(request, raw):
        try:
            return inspect(request, raw)
        except Exception:
            import traceback
            errors.append(traceback.format_exc())
            raise
    case.models.inner.transform = case.models.outer.transform = checked
    try:
        report = asyncio.run(case.app.run(case.token))
    except Exception:
        assert not errors, errors
        raise
    assert report.turn.achievement
    assert 'stage_action' in seen and 'stage_review' in seen
    assert len(tools.calls) == 1
    encoded = base64.b64encode(original).decode()
    for path in (case.app.directory / 'stage-evidence').glob('*.json'):
        assert encoded not in path.read_text(encoding='utf-8')
    metadata = next(item for req in case.models.inner.calls
        for item in (req.execution_context or {}).get('stage_runtime', {}).get('images', ()))
    images = ImageObservations(case.app.directory, case.app.task_id, case.app.evidence)
    assert images.load(metadata).endswith(encoded)
    with pytest.raises(ValueError, match='source_binding_changed'):
        images.load({**metadata, 'sha256': '0' * 64})
    from agent_adapters.deepseek_roles import D8DeepSeekModel
    from agent_adapters.deepseek_model import DeepSeekSettings
    from agent_core.modeling import ModelError, ModelErrorKind
    envelope, role = requests[0]
    attempts = []
    class RefusesImages:
        def _invoke_payload(self, payload, timeout, token, *, diagnostic):
            attempts.append(payload)
            diagnostic.update(http_status=400 if len(attempts) == 1 else 200,
                worker_exit_confirmed=True, transport_end='http_error' if len(attempts) == 1 else 'finished')
            if len(attempts) == 1:
                return ModelError(ModelErrorKind.PROVIDER_FAILURE, 'deepseek_http_400')
            return dict(content='', finish_reason='tool_calls', tool_calls=[])
    model = D8DeepSeekModel(role, DeepSeekSettings(role.model, 'offline-test-key'), transport=RefusesImages())
    model.image_loader = images.load
    model.invoke(envelope, 30, SimpleNamespace(is_cancelled=False))
    assert len(attempts) == 2
    fallback_parts = attempts[1]['messages'][-1]['content']
    assert 'NOT inspected' in fallback_parts[0]['text']
    assert all(p['type'] == 'text' for p in fallback_parts)
    lost = []
    class LosesResponse:
        def _invoke_payload(self, payload, timeout, token, *, diagnostic):
            lost.append(payload)
            diagnostic.update(worker_exit_confirmed=False)
            return ModelError(ModelErrorKind.TIMED_OUT, 'd8_transport_outcome_unknown')
    model.transport = LosesResponse()
    result = model.invoke(envelope, 30, SimpleNamespace(is_cancelled=False))
    assert isinstance(result, ModelError) and len(lost) == 1


def test_large_image_normalizes_and_invalid_image_is_rejected(case, monkeypatch):
    from uuid import uuid4
    monkeypatch.setattr(http_adapter, 'WindowsEvidenceProtection', _TestOnlyProtection)
    images = ImageObservations(case.app.directory, case.app.task_id, case.app.evidence)
    data = png((3000, 1800))
    result = images.save(str(uuid4()), data)
    assert result['resized'] and result['width'] <= 2048 and result['byte_size'] <= 1024 * 1024
    assert len(images.store.read(result['evidence_id'])) == result['byte_size']
    with pytest.raises(ValueError, match='image.decode_failed'):
        images.save(str(uuid4()), b'not an image')
    with pytest.raises(ValueError, match='image.source_size_rejected'):
        images.save(str(uuid4()), b'x' * (8 * 1024 * 1024 + 1))


def test_no_pixels_fallback_is_explicit_and_text_only_is_unchanged():
    payload = {'model': 'text-model', 'messages': [{'role': 'user', 'content': 'Original context'}]}
    original = json.dumps(payload)
    assert json.dumps(attach_images(payload, (), None)) == original
    result = attach_images(payload, [{'evidence_id': 'metadata-only'}], None)
    assert result['model'] == 'text-model'
    assert 'NOT inspected' in result['messages'][0]['content'][0]['text']
    assert all(p['type'] == 'text' for p in result['messages'][0]['content'])


def test_binary_readback_bypasses_command_output_limit(monkeypatch):
    import struct
    import threading
    from contextlib import nullcontext
    from Sandbox.image_readback import read_image, READ_IMAGE
    import docker.utils.socket
    monkeypatch.setattr(docker.utils.socket, 'read', lambda socket, n=4096: socket.recv(n))
    output = io.BytesIO()
    Image.effect_noise((512, 512), 100).save(output, format='PNG')
    data = output.getvalue()
    assert len(data) > 64 * 1024
    wire = io.BytesIO(struct.pack('>BxxxI', 1, len(data)) + data)
    class Socket:
        def recv(self, size): return wire.read(size)
        def close(self): pass
    class API:
        def exec_create(self, sandbox_id, command, **kwargs):
            assert sandbox_id == 'owned' and command == ['python3', '-I', '-c', READ_IMAGE, '/workspace/work/a.png']
            assert kwargs['user'] == '10001:10001'
            return {'Id': 'read-exec'}
        def exec_start(self, *a, **k): return Socket()
        def exec_inspect(self, *a): return {'Running': False, 'ExitCode': 0}
    binding = SimpleNamespace(task_run_id='task', execution_lock=threading.Lock())
    sandbox = SimpleNamespace(_binding=lambda s: binding, _verify_active=lambda *a: None,
        _admit=lambda b: nullcontext(), _runtime=SimpleNamespace(client=SimpleNamespace(api=API())))
    assert read_image(sandbox, 'owned', '/workspace/work/a.png') == data


@pytest.mark.parametrize('changed', [False, True])
def test_mcp_image_plan_is_exact_and_bytes_stay_out_of_text_payload(changed):
    from MCPs.Mcp_client.client import MCPManager, MCPCommandPlanError
    manager = object.__new__(MCPManager)
    path, reads, audits = '/workspace/work/a.png', [], []
    class Session:
        async def call_tool(self, name, *, arguments):
            return SimpleNamespace(structuredContent={'image_read': {'path': '/workspace/work/other.png' if changed else path}})
    manager.tool_index = {'image.read_image': SimpleNamespace(server_name='image', tool_name='read_image')}
    manager.runtimes = {'image': SimpleNamespace(session=Session())}
    manager.mcp_call_timeout_seconds = 5
    def read(agent, source):
        reads.append((agent, source))
        return png()
    manager.sandbox_service = SimpleNamespace(read_image=read,
        get_session=lambda agent: SimpleNamespace(sandbox_id='owned'))
    async def audit(value): audits.append(value)
    manager._write_audit = audit
    async def run():
        return await manager._execute_bound_tool(agent_id='task', qualified_name='image.read_image', arguments={'path': path})
    if changed:
        with pytest.raises(MCPCommandPlanError, match='image.plan_changed'):
            asyncio.run(run())
        assert not reads
    else:
        result = asyncio.run(run())
        assert result.image_bytes == png() and result.stdout == ''
        assert 'image_bytes' not in result.to_tool_payload()
        assert [entry['event'] for entry in audits] == ['started', 'finished']
