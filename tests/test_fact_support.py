"""Same-Intent Fact reuse over real HTTP scheduling/storage, no network."""
import asyncio
from dataclasses import replace
import pytest
from test_http import http_case
from agent_core.conclusion_contracts import D22FactReviewRequest


def two_observations(case, *, second="reuse"):
    inner_original, outer_original = case.models[1].invoke, case.models[0].invoke
    counts={"complete":0,"review":0,"plan":0}
    def inner(envelope,*args):
        value=inner_original(envelope,*args)
        stage=envelope.output_contract['stage']
        if stage=='inner_plan':
            counts['plan']+=1
            value['path_query']='/?parameter='+str(counts['plan'])
        if stage=='inner_complete':
            counts['complete']+=1
            if counts['complete']==1:value['review_requested']=True
        return value
    def outer(envelope,*args):
        value=outer_original(envelope,*args)
        if envelope.output_contract['stage']=='review' and envelope.request.review_scope=='action':
            value['decision']='replan_current_intent'
        if envelope.output_contract['stage']=='fact_review':
            counts['review']+=1
            assert isinstance(envelope.request,D22FactReviewRequest)
            if counts['review']==2:
                assert len(envelope.request.existing_facts)==1
                if second=='reuse':
                    return dict(decision='reuse',reason='Same scoped conclusion, additional source only.',
                                reuse_fact_id=envelope.request.existing_facts[0]['fact_id'])
                if second=='accept':
                    return dict(decision='accept',reason='Distinct request-scoped claim.',
                                refined_content='Parameter 2 independently received this response; no claim about server effects.')
                return dict(decision=second,reason='No conclusion should be added.')
        return value
    case.models[1].invoke,case.models[0].invoke=inner,outer
    return counts


@pytest.mark.parametrize('http_case',[22],indirect=True)
def test_same_conclusion_reuses_fact_and_keeps_both_observations(http_case):
    case=http_case
    two_observations(case)
    result=asyncio.run(case.app.run(case.token))
    assert result.stop_reason=='d9_awaiting_external_feedback',result
    assert len(case.sends)==2 and len(case.app.state.executions)==2
    fact,=case.app.state.facts
    records=case.app.store.list_d8_conclusions(case.task)
    assert len(records)==2 and all(r.fact_id==fact.fact_id for r in records)
    first,second=case.app.state.plans
    assert first.status.value=='superseded' and second.status.value=='completed'
    assert fact.source_ref==case.app.web.observation(
        case.app.store.load_d3_plan_result(first.plan_id).proof_id,historical=True).execution_id
    assert case.app.store.load_d3_plan_result(second.plan_id).supporting_fact_id==fact.fact_id
    assert case.app.state.intents[0].status.value=='succeeded'
    # Recover the committed relationship without another request or Fact.
    before=len(case.sends)
    case.app.conclusions.reconcile()
    assert len(case.sends)==before and len(case.app.state.facts)==1


@pytest.mark.parametrize('http_case',[22],indirect=True)
def test_equal_bodies_can_support_distinct_scoped_facts(http_case):
    case=http_case
    two_observations(case,second='accept')
    assert asyncio.run(case.app.run(case.token)).stop_reason=='d9_awaiting_external_feedback'
    assert len(case.app.state.facts)==2 and len(case.sends)==2


@pytest.mark.parametrize('http_case',[22],indirect=True)
def test_no_new_information_returns_to_inner_without_fact_or_recovery(http_case):
    case=http_case
    outer_original,inner_original=case.models[0].invoke,case.models[1].invoke
    calls=[]
    def outer(envelope,*args):
        if envelope.output_contract['stage']=='fact_review':
            return dict(decision='no_new_information',reason='Nothing worth adding.')
        return outer_original(envelope,*args)
    def inner(envelope,*args):
        if envelope.output_contract['stage']=='inner_plan':
            calls.append(envelope)
        return inner_original(envelope,*args)
    case.models[0].invoke,case.models[1].invoke=outer,inner
    propose=case.app.inner.propose
    async def stop_after_known_proposal(request,*args,**kwargs):
        value=await propose(request,*args,**kwargs)
        if request.stage=='inner_plan' and len(calls)==3:case.token.is_cancelled=True
        return value
    case.app.inner.propose=stop_after_known_proposal
    result=asyncio.run(case.app.run(case.token))
    assert not result.recovery_required,result
    assert len(calls)==3 and len(case.sends)==2
    assert case.app.state.facts==()
    records=case.app.store.list_d8_conclusions(case.task)
    assert len(records)==2
    assert all(r.outcome=='no_new_information' and r.fact_id is None for r in records)
    assert all(case.app.store.load_d3_plan_result(r.plan_id).outcome=='no_new_information' for r in records)


@pytest.mark.parametrize('http_case',[22],indirect=True)
def test_no_information_can_request_completion_from_existing_fact_without_third_http(http_case):
    case=http_case
    two_observations(case,second='no_new_information')
    original=case.models[1].invoke
    def inner(envelope,*args):
        if envelope.output_contract['stage']=='inner_plan':
            import json
            choices=json.loads(envelope.request.history_context).get('completion_facts',[])
            if choices:
                return dict(kind='request_completion',fact_id=choices[0]['fact_id'])
        return original(envelope,*args)
    case.models[1].invoke=inner
    result=asyncio.run(case.app.run(case.token))
    assert result.stop_reason=='d9_awaiting_external_feedback',result
    assert len(case.sends)==2 and len(case.app.state.facts)==1
    assert case.app.state.intents[0].status.value=='succeeded'


@pytest.mark.parametrize('http_case',[22],indirect=True)
def test_saved_reuse_decision_recovers_without_repeating_request_or_review(http_case,monkeypatch):
    from test_chain_multi_turn import ProcessLoss
    case=http_case
    counts=two_observations(case)
    complete=case.app.control.complete_plan
    def lose(binding,result,*args,**kwargs):
        if any(f.fact_id==result.supporting_fact_id for f in case.app.state.facts):
            raise ProcessLoss('after review persisted before atomic support commit')
        return complete(binding,result,*args,**kwargs)
    monkeypatch.setattr(case.app.control,'complete_plan',lose)
    with pytest.raises(ProcessLoss):asyncio.run(case.app.run(case.token))
    assert len(case.sends)==2 and counts['review']==2
    case.app.store.close()
    case.app=case.create()
    result=asyncio.run(case.app.resume(case.task,case.token))
    assert result.stop_reason=='d9_awaiting_external_feedback',result
    assert len(case.sends)==2 and counts['review']==2
    assert len(case.app.state.facts)==1 and len(case.app.store.list_d8_conclusions(case.task))==2


@pytest.mark.parametrize('http_case',[22],indirect=True)
def test_withdrawn_conflicted_and_foreign_scope_fact_are_not_reusable(http_case,monkeypatch):
    from uuid import uuid4
    from agent_core.dual_llm_contracts import D3RecoveryRequired
    case=http_case
    two_observations(case)
    assert asyncio.run(case.app.run(case.token)).stop_reason=='d9_awaiting_external_feedback'
    fact,=case.app.state.facts
    last=case.app.state.plans[-1]
    result=case.app.store.load_d3_plan_result(last.plan_id)
    source=case.app.conclusions.observation(result.proof_id,historical=True)
    assert case.app.conclusions.reusable_facts(replace(source,intent_id=str(uuid4())))==()
    history=case.app.revisions._history()
    for status in ('withdrawn','conflicted','superseded'):
        monkeypatch.setattr(case.app.revisions,'_history',lambda status=status: tuple(
            replace(i,current_status=status,conflict=(status=='conflicted')) if i.record_id==fact.fact_id else i for i in history))
        assert case.app.conclusions.reusable_facts(source)==()
        with pytest.raises(D3RecoveryRequired,match='not_effective'):
            case.app.validator._support(case.app.state,case.app.store.load_d3_plan_binding(last.plan_id),result.proof_id,fact.fact_id)


@pytest.mark.parametrize('http_case',[22],indirect=True)
def test_schema27_migrates_once_preserving_records_and_readonly_history(http_case):
    import sqlite3
    from agent_core.sqlite_control_store import SqliteControlStore,MIGRATION_23,CURRENT_SCHEMA_VERSION
    case=http_case
    assert asyncio.run(case.app.run(case.token)).stop_reason=='d9_awaiting_external_feedback'
    path=case.directory/'control.sqlite'
    case.app.store.close()
    # Rebuild precisely the pre-change table; no later tables are left behind.
    with sqlite3.connect(path) as db:
        saved=db.execute('SELECT * FROM d8_conclusions ORDER BY event_sequence').fetchall()
        db.executescript('ALTER TABLE d8_conclusions RENAME TO temporary_conclusions; DROP INDEX idx_d8_conclusions_run; DROP INDEX idx_d8_conclusions_fact;'+MIGRATION_23)
        db.executemany('INSERT INTO d8_conclusions VALUES (?,?,?,?,?,?,?,?)',saved)
        db.execute('DROP TABLE temporary_conclusions')
        # Rebuild the other table changed after schema27, including its original source FK.
        db.execute('DROP TRIGGER facts_source_insert')
        facts=db.execute('SELECT * FROM facts').fetchall()
        db.execute('DROP TABLE facts')
        db.execute("CREATE TABLE facts(fact_id TEXT PRIMARY KEY,turn_id TEXT NOT NULL REFERENCES turns(turn_id),content TEXT NOT NULL,source_ref TEXT NOT NULL REFERENCES executions(execution_id),status TEXT NOT NULL,created_event_sequence INTEGER NOT NULL,created_event_ordinal INTEGER NOT NULL,verification_ref TEXT)")
        db.executemany('INSERT INTO facts VALUES (?,?,?,?,?,?,?,?)',facts)
        db.execute('CREATE INDEX idx_facts_context ON facts(turn_id,status,created_event_sequence DESC,created_event_ordinal DESC)')
        db.execute('DELETE FROM schema_versions WHERE version>27')
    with SqliteControlStore.open_readonly(path) as old:
        assert old._schema_version==27 and len(old.list_d8_conclusions(case.task))==1
    with SqliteControlStore.open(path) as migrated:
        assert migrated._schema_version==CURRENT_SCHEMA_VERSION
        assert [tuple(r) for r in migrated._connection.execute('SELECT * FROM d8_conclusions ORDER BY event_sequence')]==saved
        assert migrated._connection.execute('PRAGMA foreign_key_check').fetchone() is None
        assert [tuple(r) for r in migrated._connection.execute('SELECT * FROM facts')]==facts
        assert next(r for r in migrated._connection.execute('PRAGMA table_info(d8_conclusions)') if r[1]=='plan_id')[3]==0
        assert migrated._connection.execute('SELECT COUNT(*) FROM schema_versions WHERE version=29').fetchone()[0]==1
        assert migrated._connection.execute('SELECT COUNT(*) FROM schema_versions WHERE version=28').fetchone()[0]==1
    with SqliteControlStore.open(path) as again:
        assert len(again.list_d8_conclusions(case.task))==1


from test_output import output_case

@pytest.mark.parametrize('output_case',[22],indirect=True)
def test_fixed_observations_also_reuse_fact_without_rewriting_origin(output_case):
    case=output_case
    inner=case.models['inner'].transform
    reviews=case.models['review'].transform
    count=[]
    def model(envelope,raw):
        if envelope.output_contract['stage']=='inner_complete':
            count.append(True)
            if len(count)==1:raw['review_requested']=True
            else:case.token.is_cancelled=True
        return inner(envelope,raw)
    def review(envelope,raw):
        if envelope.output_contract['stage']=='review' and envelope.request.review_scope=='action':
            raw['decision']='replan_current_intent'
        return reviews(envelope,raw)
    def facts(envelope,raw):
        if getattr(envelope.request,'existing_facts',()):
            return dict(decision='reuse',reason='Same fixed-copy conclusion.',
                        reuse_fact_id=envelope.request.existing_facts[0]['fact_id'])
        return raw
    case.models['fact'].transform=facts
    case.models['inner'].transform=model
    case.models['review'].transform=review
    result=asyncio.run(case.app.run(case.token))
    assert not result.recovery_required,result
    fact,=case.app.state.facts
    records=case.app.store.list_d8_conclusions(case.task)
    assert len(records)==2 and all(r.fact_id==fact.fact_id for r in records)


@pytest.mark.parametrize('http_case',[22],indirect=True)
@pytest.mark.parametrize('repair',[True,False])
def test_ambiguous_duplicate_requires_one_explicit_correction(http_case,repair):
    case=http_case
    counts=two_observations(case)
    outer=case.models[0].invoke
    attempts=[]
    def model(envelope,*args):
        if envelope.output_contract['stage']=='fact_review' and envelope.request.existing_facts:
            attempts.append(envelope.request)
            if envelope.request.correction_of_model_request_id is None or not repair:
                return dict(decision='accept',reason='Ambiguous unchanged final text.')
            assert envelope.request.rejection_code=='d22_ambiguous_duplicate_conclusion'
        return outer(envelope,*args)
    case.models[0].invoke=model
    result=asyncio.run(case.app.run(case.token))
    assert len(attempts)==2 and len(case.sends)==2 and len(case.app.state.facts)==1,result
    if repair:
        assert result.stop_reason=='d9_awaiting_external_feedback',result
    else:
        assert result.stop_reason=='d22_ambiguous_duplicate_conclusion',result
        again=asyncio.run(case.app.resume(case.task,case.token))
        assert again.stop_reason==result.stop_reason and len(attempts)==2 and len(case.sends)==2


@pytest.mark.parametrize('output_case',[22],indirect=True)
def test_fixed_no_information_can_continue_past_old_two_plan_limit(output_case):
    case=output_case
    seen=[]
    original=case.models['inner'].transform
    def model(envelope,raw):
        if envelope.output_contract['stage']=='inner_plan':seen.append(envelope)
        return original(envelope,raw)
    case.models['inner'].transform=model
    case.models['fact'].transform=lambda envelope,raw: dict(decision='no_new_information',reason='No new conclusion.')
    propose=case.app.inner.propose
    async def stop_after_known_proposal(request,*args,**kwargs):
        value=await propose(request,*args,**kwargs)
        if request.stage=='inner_plan' and len(seen)==3:case.token.is_cancelled=True
        return value
    case.app.inner.propose=stop_after_known_proposal
    result=asyncio.run(case.app.run(case.token))
    assert not result.recovery_required,result
    assert len(seen)==3 and case.app.state.facts==()
    records=case.app.store.list_d8_conclusions(case.task)
    assert len(records)==2 and all(r.outcome=='no_new_information' for r in records)
