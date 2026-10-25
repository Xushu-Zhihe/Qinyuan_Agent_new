"""SCNET admission uses confirmed limits without changing the execution flow."""
from dataclasses import asdict, replace
import json
from types import SimpleNamespace
import pytest
from agent_adapters import deepseek_roles as roles
from agent_adapters.simulation import real_config, parse_request
from agent_core.output_contracts import D8OutputRequest
from test_models import real_request


@pytest.mark.parametrize("version", [6, 21, 22])
def test_verified_capacity_is_independent_of_contract_version(monkeypatch, tmp_path, version):
    monkeypatch.setattr(roles, "MODEL", "DeepSeek-V4.1-Flash")
    monkeypatch.setitem(roles.TRANSPORT, "base_url", "https://api.scnet.cn/api/llm/v1")
    assert roles.default_capacity_policy() == roles.SCNET_V41_CAPACITY
    assert real_config(contract_version=version)["handoff_config"]["inner_capacity"] == asdict(roles.SCNET_V41_CAPACITY)
    raw = real_request(tmp_path)
    raw["execution_profile"] = f"d8_v{version}"
    raw["d8_config"] = real_config(contract_version=version)
    raw["dual_llm_config"]["outer_role"] = asdict(roles.role_config("outer_planning", version=version-5))
    raw["dual_llm_config"]["inner_role"] = asdict(roles.role_config("inner_action", version=version-5))
    # Docker schema is validated locally; no Docker process is started.
    raw = json.loads(json.dumps(raw))
    request, config = parse_request(raw)
    assert config.handoff_config.inner_capacity == roles.SCNET_V41_CAPACITY
    for name in ("inner_capacity", "outer_capacity"):
        raw["d8_config"]["handoff_config"][name] = asdict(replace(roles.SCNET_V41_CAPACITY,
            total_units=262144, output_reserve_units=16384))
        with pytest.raises(ValueError, match="d8_real_capacity_policy_mismatch"):
            parse_request(raw)
        raw["d8_config"]["handoff_config"][name] = asdict(roles.SCNET_V41_CAPACITY)


@pytest.mark.parametrize("overshoot", [-1, 0, 1])
@pytest.mark.parametrize("explicit", [False, True])
def test_full_request_boundary_accounts_for_output_and_margin(monkeypatch, overshoot, explicit):
    role = roles.role_config("inner_action")
    role = replace(role, model="DeepSeek-V4.1-Flash")
    monkeypatch.setitem(roles.TRANSPORT, "base_url", "https://api.scnet.cn/api/llm/v1")
    policy = roles.SCNET_V41_CAPACITY
    payload = dict(model=role.model, messages=[dict(role="system", content="rule"),
        dict(role="user", content="{}")])
    if explicit:payload["max_tokens"] = 32768
    limit = policy.total_units - policy.output_reserve_units - 4096
    payload["padding"] = "x" * (limit - len(roles.payload_bytes({**payload, "padding": ""})) + overshoot)
    monkeypatch.setattr(roles, "build_payload", lambda *a, **k: payload)
    envelope = D8OutputRequest(SimpleNamespace(model_request_id="id"), {"stage": "inner_plan"}, "", None)
    capacity = roles.EstimatedCapacity(role, role)
    assert capacity.fits(envelope, policy) is (overshoot <= 0)
    assert capacity.last_measurement["input_tokens"] is None
    assert capacity.last_measurement["estimated_input_tokens"] == len(roles.payload_bytes(payload)) + 4096
    assert capacity.last_measurement["output_reserve_tokens"] == roles.SCNET_V41_MAX_OUTPUT
    assert capacity.last_measurement["requested_output_tokens"] == (32768 if explicit else None)
    assert ("max_tokens" in payload) is explicit


def test_previously_paused_request_size_can_pass_without_dropping_context(monkeypatch):
    role = replace(roles.role_config("inner_action"), model="DeepSeek-V4.1-Flash")
    monkeypatch.setitem(roles.TRANSPORT, "base_url", "https://api.scnet.cn/api/llm/v1")
    payload = dict(model=role.model, messages=[dict(role="system", content="rules"), dict(role="user", content="{}")], max_tokens=32768)
    payload["padding"] = "x" * (246904 - len(roles.payload_bytes({**payload, "padding": ""})))
    monkeypatch.setattr(roles, "build_payload", lambda *a, **k: payload)
    env = D8OutputRequest(SimpleNamespace(), {"stage": "inner_plan"}, "", None)
    capacity = roles.EstimatedCapacity(role, role)
    assert capacity.fits(env, roles.SCNET_V41_CAPACITY)
    assert len(roles.payload_bytes(payload)) == 246904
    payload["max_tokens"] = 384001
    with pytest.raises(ValueError, match="capacity_binding"):
        capacity.fits(env, roles.SCNET_V41_CAPACITY)


from test_http import http_case


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
@pytest.mark.parametrize('trigger',['utf8','full_envelope','uncovered','unavailable_summary','still_too_large'])
def test_continuous_session_compacts_before_freezing_full_request(http_case,monkeypatch,trigger):
    import asyncio
    from uuid import uuid4
    from test_exploration_loop import install
    from agent_core.handoff_contracts import handoff_json
    from agent_core.dual_llm_contracts import D3RecoveryRequired
    c=http_case
    install(c,steps=5,stop='inconclusive')
    assert not asyncio.run(c.app._run_once(c.token)).recovery_required
    actor=c.app.state.intents[0]
    rows=c.app.sessions.records(actor.intent_id)
    assert len(rows)>4
    assert asyncio.run(c.app.handoff.summarize(actor.turn_id,c.token,decision_prefix=rows[:-4])).origin == 'model_organized'
    previous=c.app.sessions.evidence.read('request',rows[-1].model_request_id)
    original=json.dumps(previous,sort_keys=True)
    base=next(e.request for e in c.exploration_envelopes if e.output_contract['stage']=='inner_plan')
    history=json.loads(base.history_context)
    if trigger in {'full_envelope','still_too_large'}:
        history['retained_material']='x'*(550000 if trigger=='full_envelope' else 850000)
    draft=replace(base,model_request_id=str(uuid4()),base_checkpoint_sequence=c.app.state.latest_checkpoint.sequence,
                  history_context=handoff_json(history))
    payload=c.app.sessions.payload
    padding='界'*25000 if trigger in {'utf8','uncovered','unavailable_summary'} else 'x'*45000
    def large(record):
        data=payload(record)
        return {**data,'observation':{**data['observation'],'detail':padding}} if record in rows[:-4] else data
    monkeypatch.setattr(c.app.sessions,'payload',large)
    if trigger=='utf8':
        raw=handoff_json([large(r) for r in rows])
        limit=c.app.handoff.capacity().capacity.visible_history
        assert len(raw)<limit<len(raw.encode('utf-8'))
    if trigger=='full_envelope':
        assert len(handoff_json([large(r) for r in rows]).encode('utf-8'))<c.app.handoff.capacity().capacity.visible_history
        # Place the full envelope across the actual profile boundary while its
        # compressed form fits; do not bake a second legacy model window into the test.
        history.pop('retained_material')
        plain = replace(draft, history_context=handoff_json(history))
        expanded = c.app.sessions._wrap(plain, preview=True)
        c.app.handoff._fits_complete_request(expanded,c.app.config.handoff_config.inner_capacity)
        full_size = c.app.handoff.capacity_port.last_measurement['payload_utf8_bytes']
        packet = json.loads(expanded.session_context)
        packet['messages'] = packet['messages'][-12:]
        c.app.handoff._fits_complete_request(replace(expanded,session_context=handoff_json(packet)),c.app.config.handoff_config.inner_capacity)
        compact_size = c.app.handoff.capacity_port.last_measurement['payload_utf8_bytes']
        policy = c.app.config.handoff_config.inner_capacity
        history['retained_material'] = 'x' * (policy.total_units-policy.output_reserve_units-4096-full_size+(full_size-compact_size)//2)
        draft = replace(draft,history_context=handoff_json(history))
    if trigger=='uncovered':
        latest=c.app.handoff._latest_summary
        record,summary=latest()
        summary={**summary,'all_items':[i for i in summary['all_items'] if not i['record_id'].startswith('decision/')]}
        monkeypatch.setattr(c.app.handoff,'_latest_summary',lambda:(record,summary))
    read_request=c.app.sessions.evidence.read
    if trigger in {'unavailable_summary','uncovered'}:
        if trigger=='unavailable_summary':
            record,summary=c.app.handoff._latest_summary()
            monkeypatch.setattr(c.app.handoff,'_latest_summary',lambda:(replace(record,origin='trusted_code_fallback'),summary))
        def previously_compacted(kind,key):
            data=read_request(kind,key)
            if kind=='request' and key==rows[-1].model_request_id:
                packet=json.loads(data['request']['session_context'])
                packet['segment_start']=rows[-4].ordinal
                data={**data,'request':{**data['request'],'session_context':handoff_json(packet)}}
            return data
        monkeypatch.setattr(c.app.sessions.evidence,'read',previously_compacted)
    if trigger=='still_too_large':
        with pytest.raises(D3RecoveryRequired,match='capacity_paused'):
            c.app.sessions.wrap(draft)
        assert c.app.sessions.evidence.read('request',draft.model_request_id) is None
        assert c.app.output_boundary.evidence.read('input-measurement',draft.model_request_id) is None
        # Pre-admission diagnostics cannot freeze a failed measurement under the
        # ID later used for the admitted input; retain the failure in a pause receipt.
        assert c.app.handoff.pause_report['input_measurement']['model_request_id']==draft.model_request_id
        assert c.app.handoff.pause_report['unit']=='estimated_token_units'
        recovered=c.app.sessions.wrap(replace(draft,history_context=base.history_context))
        assert c.app.output_boundary.evidence.read('input-measurement',recovered.model_request_id)['fits']
    else:
        result=c.app.sessions.wrap(draft)
        packet=json.loads(result.session_context)
        if trigger in {'uncovered','unavailable_summary'}:
            assert len(packet['messages'])==len(rows)*3
            assert 'compaction' not in packet  # A later summary cannot invent coverage.
        else:
            assert packet['segment_start']==rows[-4].ordinal
            assert len(packet['messages'])==12
            assert packet['compaction']['raw_decisions_retained']
            assert len(packet['compaction']['omitted_decision_refs'])==len(rows)-4
            assert 'retained_material' not in history or json.loads(result.history_context)['retained_material']==history['retained_material']
            measured=c.app.output_boundary.evidence.read('input-measurement',result.model_request_id)
            assert measured['fits'] and measured['measurement_kind']=='provider_envelope'
    assert json.dumps(read_request('request',rows[-1].model_request_id),sort_keys=True)==original
    assert not c.app.state.facts


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
@pytest.mark.parametrize('wake',['deadline','checkpoint','cancel','operation'])
def test_unchanged_capacity_wait_does_not_rebuild_request(http_case,monkeypatch,wake):
    import asyncio
    from agent_core.operation_accounting import D4OperationStart,D4OperationCompletion
    from agent_core.turn_modeling import digest
    from uuid import uuid4
    c=http_case
    c.app.handoff.capacity()
    blocked=[]
    real_once=c.app._run_once
    async def capacity_block(token):
        if token.is_cancelled:return await real_once(token)
        blocked.append(c.app.state.latest_checkpoint.sequence)
        return c.app.result('d8_output_complete_request_capacity_paused',recovery_required=True)
    monkeypatch.setattr(c.app,'_run_once',capacity_block)
    waits=[]
    reloads=[]
    reload=c.app.controller.reload_state
    def revalidate():
        reloads.append(1)
        return reload()
    monkeypatch.setattr(c.app.controller,'reload_state',revalidate)
    async def wait(token):
        waits.append(1)
        if len(waits)<3:
            assert len(blocked)==1 and not reloads
            return
        if len(waits)==3 and wake=='checkpoint':
            c.app.handoff.increase_capacity({'visible_history':c.app.handoff.capacity().capacity.visible_history+1024})
            return
        if len(waits)==3 and wake=='operation':
            identity=str(uuid4())
            c.app.store.begin_d4_operation(D4OperationStart(identity,c.task,'tool_dispatch',c.clock(),
                c.app.state.latest_checkpoint.sequence,digest({'controlled':True}),'controlled-observation',c.app.config.d4_budget.budget_policy_ref))
            c.app.store.complete_d4_operation(D4OperationCompletion(identity,c.clock(),'completed','controlled-receipt',digest({'done':True})))
            return
        if wake=='cancel':token.is_cancelled=True
        else:c.clock.now=c.app.state.task_run.deadline_at
    monkeypatch.setattr(c.app,'_wait_continuation',wait)
    result=asyncio.run(c.app.run(c.token))
    assert len(blocked)==(2 if wake in {'checkpoint','operation'} else 1)
    assert result.stop_reason==('task_run_cancelled' if wake=='cancel' else 'task_run_deadline_exceeded')
    if wake=='deadline':assert not reloads
    if wake in {'checkpoint','operation'}:assert reloads
    assert c.app.state.task_run.status.value==('cancelled' if wake=='cancel' else 'deadline_exceeded')
    assert not c.sends and not c.models[0].calls and not c.models[1].calls
    assert not c.app.state.facts and not c.app.state.intents


def test_unconfirmed_alias_does_not_select_scnet_capacity(monkeypatch):
    monkeypatch.setattr(roles,'MODEL','deepseek-flash')
    monkeypatch.setitem(roles.TRANSPORT,'base_url','https://api.scnet.cn/api/llm/v1')
    with pytest.raises(ValueError, match="d8_model_capacity_configuration_unsupported"):
        roles.default_capacity_policy()


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
def test_capacity_wait_does_not_latch_over_a_just_finished_supervisor(http_case,monkeypatch):
    import asyncio
    import threading
    c=http_case
    c.app.handoff.capacity()
    calls=[]
    async def blocked(token):
        calls.append(1)
        if len(calls)==1:
            finished=threading.Event()
            finished.set()  # Completes during assembly, before the wait key is captured.
            c.app.supervision.pending=(None,finished,[],None)
        else:
            assert len(calls)==2 and c.app.supervision.pending[1].is_set()
            c.app.supervision.pending=None  # Model reconciliation, before attempting capacity again.
        return c.app.result('d8_output_complete_request_capacity_paused',recovery_required=True)
    monkeypatch.setattr(c.app,'_run_once',blocked)
    waits=[]
    async def wait(token):
        waits.append(1)
        if len(waits)==3:c.clock.now=c.app.state.task_run.deadline_at
    monkeypatch.setattr(c.app,'_wait_continuation',wait)
    result=asyncio.run(c.app.run(c.token))
    assert len(calls)==2 and result.stop_reason=='task_run_deadline_exceeded'
    assert not c.sends and not c.app.store.list_d4_operations(c.task)


@pytest.mark.parametrize("model", sorted(roles.OFFICIAL_V4_MODELS))
def test_verified_official_models_map_to_current_window(monkeypatch, model):
    monkeypatch.setattr(roles, "MODEL", model)
    monkeypatch.setitem(roles.TRANSPORT, "base_url", "https://api.deepseek.com")
    assert roles.default_capacity_policy() == roles.OFFICIAL_V4_CAPACITY
    assert roles.OFFICIAL_V4_CAPACITY.total_units == 1_000_000
    assert roles.OFFICIAL_V4_CAPACITY.output_reserve_units == 384_000


@pytest.mark.parametrize("endpoint,model", [("https://other.test", "deepseek-flash"),
    ("https://api.deepseek.com", "unverified-model"), ("https://api.scnet.cn/api/llm/v1", "deepseek-flash")])
def test_unverified_pair_never_inherits_official_window(endpoint, model):
    with pytest.raises(ValueError, match="d8_model_capacity_configuration_unsupported"):
        roles.model_capacity_policy(model, endpoint)


def test_official_full_request_matches_runs112_size_without_sending_output_cap(monkeypatch):
    monkeypatch.setitem(roles.TRANSPORT, "base_url", "https://api.deepseek.com")
    role = replace(roles.role_config("inner_action"), model="deepseek-flash")
    payload = dict(model=role.model, messages=[dict(role="system", content="rule"), dict(role="user", content="{}")],
                   thinking={"type":"enabled"}, reasoning_effort="high")
    payload["padding"] = "x" * (244604-len(roles.payload_bytes({**payload,"padding":""})))
    monkeypatch.setattr(roles, "build_payload", lambda *a, **k: payload)
    capacity = roles.EstimatedCapacity(role, role)
    request = D8OutputRequest(SimpleNamespace(model_request_id="offline"), {"stage":"inner_plan"}, "", None)
    assert capacity.fits(request, roles.OFFICIAL_V4_CAPACITY)
    assert capacity.last_measurement["input_tokens"] is None
    assert capacity.last_measurement["estimated_input_tokens"] == 248700
    assert capacity.last_measurement["output_reserve_tokens"] == 384000
    assert "max_tokens" not in payload and payload["reasoning_effort"] == "high"
    with pytest.raises(ValueError, match="capacity_binding"):
        capacity.fits(request, roles.SCNET_V41_CAPACITY)
