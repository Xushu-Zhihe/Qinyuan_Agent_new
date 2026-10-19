"""HTTP capability plugged into D8's existing tree, sessions and review gates."""

import asyncio
import hashlib
import json
import threading
import time
from dataclasses import asdict, replace
from uuid import UUID

from agent_adapters.web.http_adapter import D7HttpAdapter,D7HttpRequest,D7HttpResponseEvidenceService,D7HttpPolicy,D7HttpError
from agent_adapters.web.sandbox_transport import D7SandboxHttpTransport
from agent_adapters.output_evidence import safe_failure_details, SAFE_EXCEPTION_TYPES, SAFE_HTTP_ERROR_CODES
from agent_core.actions import MCPCall, CompleteIntentAction
from agent_core.control_store import UpsertPlan,UpsertExecution,UpsertFact,PlanValidationRecordDraft,AppendPlanValidationRecord
from agent_core.domain import PlanStep,PlanStatus,ExecutionStatus,ExecutionEvidence,PlanValidationStatus
from agent_core.dual_llm_contracts import D3CommitMetadata,D3RecoveryRequired
from agent_core.http_contracts import HttpTurnBinding,HttpPlanBinding,HttpPlanContext,HttpObservation,HttpRunConfig
from agent_core.operation_accounting import D4OperationStart,D4OperationCompletion
from agent_core.turn_modeling import digest


def wire_id(task,name):
    return str(UUID(bytes=hashlib.sha256((task+'/'+name).encode()).digest()[:16],version=4))


class HttpCapability:
    def __init__(self,app):
        self.app,self.store=app,app.store
        self.evidence=app.output_boundary.evidence
        self.proofs=self
        self.http=None
        self.protected=None
        self.transport_factory=None  # An assembly-only test seam, never a public address.
        self.read_only=False
        self.file_proofs=app.proofs if hasattr(app,'proofs') else None
        self.file_commands=app.commands if hasattr(app,'commands') else None
        self._context_thread=threading.get_ident()
        self._context_cache={}

    @property
    def state(self):
        controller=getattr(self.app,'controller',None)
        if controller is not None and controller._persistent_state is None:
            refs=self.store.list_task_run_refs()
            if len(refs)!=1:raise D3RecoveryRequired('d20_http_state_owner_unavailable')
            return self.store.load_control_state_rows(refs[0])
        return self.app.state

    @property
    def task(self):return self.state.task_run

    def policy(self):
        auth=self.app.config.web_authorization
        value=dict(endpoint=auth.allowed_origin,allowed_methods=list(auth.allowed_methods),
                   authorization_sha256=digest(asdict(auth)))
        if self.app.config.contract_version in {21,22}:
            value['attachment_indices']=list(self.app.config.authorization.attachment_indices)
        return value

    def turn_binding(self,turn_id,request_id):
        auth=self.app.config.web_authorization
        if self.app.config.contract_version in {21,22} and self.app.config.authorization.attachment_indices:
            from agent_core.hybrid_contracts import HybridTurnBinding
            from agent_core.dual_llm_contracts import D3TurnBinding
            from agent_core.planning_assertion import freeze_planning_policy
            frozen=freeze_planning_policy(self.app)
            bindings=[]
            for item in frozen['imports']:
                receipt=self.app.imports.read_receipt(item['import_id'])
                bindings.append(D3TurnBinding(self.app._id('hybrid-file/'+turn_id+'/'+str(receipt.attachment_index)),
                    self.task.task_run_id,turn_id,receipt.attachment_index,receipt.import_id,
                    receipt.byte_size,receipt.sha256,request_id))
            return HybridTurnBinding(self.app._id('hybrid-turn/'+turn_id),self.task.task_run_id,
                turn_id,auth.allowed_origin,auth.authorization_id,request_id,attachments=tuple(bindings))
        return HttpTurnBinding(self.app._id('http-turn/'+turn_id),self.task.task_run_id,
            turn_id,auth.allowed_origin,auth.authorization_id,request_id)

    def attach(self):
        if self.protected is not None:return
        self.protected=D7HttpResponseEvidenceService(self.app.directory,self.task.task_run_id,create=not self.read_only)
        if self.read_only:return
        if self.transport_factory is not None:
            transport=self.transport_factory(self)
        else:
            if self.task.target_network is None:
                raise D3RecoveryRequired('d20_http_network_binding_missing')
            configured=HttpRunConfig(self.task.task_run_id,
                wire_id(self.task.task_run_id,'request'),self.app.config.web_authorization.allowed_origin,
                self.app.config.web_authorization,self.app.config.outer_role,self.app.config.inner_role,
                self.app.config.d4_budget,'sha256:'+digest(asdict(self.app.config.handoff_config)))
            transport=D7SandboxHttpTransport(self.app.http_sandbox,sandbox_id=self.task.sandbox_id,
                run_config=configured,network_binding=self.task.target_network,
                target_network=self.app.controller._target_network)
        self.http=D7HttpAdapter(self.task.task_run_id,self.policy()['endpoint'],self.protected.store,
                               transport=transport)

    def normalize_plan(self,request,raw):
        self.attach()
        if (not isinstance(raw,dict) or set(raw)!={'kind','method','path_query','headers','body'}
                or raw['kind']!='http_request' or not isinstance(raw['headers'],dict)):
            raise ValueError('d20_http_action_fields_invalid')
        operation=wire_id(self.task.task_run_id,'request/'+request.model_request_id)
        proposed=D7HttpRequest(operation,raw['method'],raw['path_query'],tuple(raw['headers'].items()),raw['body'])
        try:
            method,path,headers,body=self.http.policy.validate(proposed)
        except D7HttpError as exc:
            raise ValueError('d20_http_request_not_authorized') from exc
        if method not in self.app.config.web_authorization.allowed_methods:
            raise ValueError('d20_http_method_not_authorized')
        value=dict(method=method,path_query=path,headers=list(headers),body=body.decode('utf-8'))
        encoded=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')
        metadata=self.protected.save(operation,encoded)
        safe=dict(kind='http_request_saved',method=method,request_evidence_id=metadata.evidence_id,
                  request_sha256=hashlib.sha256(encoded).hexdigest())
        self.evidence.save('http-plan-binding',request.model_request_id,
            dict(task_run_id=self.task.task_run_id,request_sha256=digest(asdict(request)),
                 result=safe,finished_at=self.app.clock()))
        return safe

    def read_request(self,binding):
        self.attach()
        raw=self.protected.read(binding.request_evidence_id)
        if hashlib.sha256(raw).hexdigest()!=binding.request_sha256:
            raise D3RecoveryRequired('d20_http_request_evidence_changed')
        value=json.loads(raw)
        request=D7HttpRequest(binding.execution_id,value['method'],value['path_query'],
                              tuple(tuple(h) for h in value['headers']),value['body'])
        method,_,_,_=D7HttpPolicy.for_origin(self.policy()['endpoint']).validate(request)
        if method not in self.app.config.web_authorization.allowed_methods:
            raise D3RecoveryRequired('d20_http_method_not_authorized')
        return request

    def result(self,execution_id):
        return self.evidence.read('http-result',execution_id)

    def cookie_snapshot(self,value):
        self.attach()
        try:
            raw=self.protected.read(value['cookie_evidence_id'])
            snapshot=json.loads(raw)
            if (hashlib.sha256(raw).hexdigest()!=value['cookie_sha256']
                    or snapshot['task_run_id']!=self.task.task_run_id
                    or snapshot['origin']!=self.policy()['endpoint']
                    or snapshot['authorization_sha256']!=self.policy()['authorization_sha256']
                    or snapshot['execution_id']!=value['execution_id']
                    or snapshot['previous_execution_id']!=value['cookie_previous_execution_id']):
                raise ValueError('cookie binding changed')
            return snapshot
        except (KeyError, ValueError, TypeError, D7HttpError) as exc:
            raise D3RecoveryRequired('d20_http_cookie_version_unavailable') from exc

    def cookie_history(self):
        """Recheck the complete durable chain, including read-only projections."""
        values=[]
        for plan in self.state.plans:
            binding=self.store.load_d3_plan_binding(plan.plan_id)
            if not isinstance(binding,HttpPlanBinding):continue
            value=self.result(binding.execution_id)
            if value is None:continue
            op=self.store.load_d4_operation(binding.execution_id)
            if op is None:raise D3RecoveryRequired('d20_http_cookie_result_without_start')
            values.append((op.start.base_checkpoint_sequence,value))
        previous=None
        last=None
        for _,value in sorted(values,key=lambda pair:pair[0]):
            snapshot=self.cookie_snapshot(value)
            if snapshot['previous_execution_id']!=previous:
                raise D3RecoveryRequired('d20_http_cookie_history_conflict')
            previous=value['execution_id']
            last=snapshot
        return previous,last

    def restore_cookies(self):
        previous,last=self.cookie_history()
        if last is not None:self.http.restore_cookie_session(last['session'])
        return previous

    def save_cookies(self,execution_id,previous,response):
        snapshot=dict(task_run_id=self.task.task_run_id,origin=self.policy()['endpoint'],
            authorization_sha256=self.policy()['authorization_sha256'],execution_id=execution_id,
            previous_execution_id=previous,session=self.http.export_cookie_session(),
            response=dict(content_type=response.content_type,redirect_path_query=response.redirect_path_query,
                cross_origin_redirect_blocked=response.cross_origin_redirect_blocked))
        raw=json.dumps(snapshot,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')
        metadata=self.protected.save(wire_id(self.task.task_run_id,'cookie/'+execution_id),raw)
        return dict(cookie_evidence_id=metadata.evidence_id,cookie_sha256=hashlib.sha256(raw).hexdigest(),
                    cookie_previous_execution_id=previous)

    def command_exists(self,reference):
        if not reference.startswith('http-request:'):
            return self.file_commands.command_exists(reference) if self.file_commands is not None else False
        return any('http-request:'+b.request_sha256==reference
            for p in self.state.plans if isinstance((b:=self.store.load_d3_plan_binding(p.plan_id)),HttpPlanBinding))

    def output_exists(self,reference):
        if not reference.startswith('http-response:'):
            return self.file_commands.output_exists(reference) if self.file_commands is not None else False
        value=self.result(reference.split(':',1)[1])
        return value is not None

    def lookup(self,execution_id):
        value=self.result(execution_id)
        if value is None:return self.file_commands.lookup(execution_id) if self.file_commands is not None else None
        return ExecutionEvidence(value['task_run_id'],self.policy()['endpoint'],value['finished_at'])

    def read(self,proof_id):
        if self.result(proof_id) is not None:return self.observation(proof_id,historical=True)
        return self.file_proofs.proofs.read(proof_id) if self.file_proofs is not None else None

    def issue(self,*args):return self.file_proofs.issue(*args)

    def read_verified(self,execution_id,proof_id,*,allow_inactive_plan=False):
        if self.result(proof_id) is None and self.file_proofs is not None:
            return self.file_proofs.read_verified(execution_id,proof_id,allow_inactive_plan=allow_inactive_plan)
        if execution_id!=proof_id:raise D3RecoveryRequired('d20_http_proof_owner_changed')
        source=self.observation(proof_id,historical=allow_inactive_plan)
        return source,ExecutionEvidence(source.task_run_id,source.endpoint,source.observed_at)

    def observation(self,execution_id,*,historical=False):
        self.attach()
        execution=next((e for e in self.state.executions if e.execution_id==execution_id),None)
        if execution is None or execution.status is not ExecutionStatus.SUCCEEDED:
            raise D3RecoveryRequired('d20_http_execution_not_succeeded')
        binding=self.store.load_d3_plan_binding(execution.plan_id)
        value=self.result(execution_id)
        if not isinstance(binding,HttpPlanBinding) or value is None:
            raise D3RecoveryRequired('d20_http_result_missing')
        request=self.read_request(binding)
        self.cookie_snapshot(value)
        op=self.store.load_d4_operation(execution_id)
        body=self.protected.read(value['body_evidence_id'])
        if (value['task_run_id']!=self.task.task_run_id or value['plan_id']!=binding.plan_id
                or value['execution_id']!=binding.execution_id or value['request_sha256']!=binding.request_sha256
                or value['body_sha256']!=hashlib.sha256(body).hexdigest() or value['body_bytes']!=len(body)
                or value['method']!=request.method or execution.output_ref!='http-response:'+execution_id
                or op is None or op.completion is None or op.completion.result_kind!='completed'
                or op.completion.evidence_sha256!=digest(value)
                or not self.task.started_at<=value['finished_at']<=self.task.deadline_at):
            raise D3RecoveryRequired('d20_http_source_binding_changed')
        plan=next(p for p in self.state.plans if p.plan_id==binding.plan_id)
        if not historical and plan.status not in {PlanStatus.RUNNING,PlanStatus.COMPLETED}:
            raise D3RecoveryRequired('d20_http_plan_inactive')
        return HttpObservation(self.task.task_run_id,binding.turn_id,binding.intent_id,binding.plan_id,
            binding.request_evidence_id,execution_id,execution_id,execution_id,value['body_sha256'],
            value['body_bytes'],value['finished_at'],digest({'binding':asdict(binding),'result':value}),
            method=value['method'],endpoint=self.policy()['endpoint'],response_status=value['status'],
            request_sha256=binding.request_sha256,response_evidence_id=value['body_evidence_id'],
            rule_binding_sha256=binding.rule_binding_sha256)

    def execution_projection(self,execution,binding):
        content=dict(kind='execution',status=execution.status.value,step_index=execution.step_index)
        version={'execution':asdict(execution)}
        if execution.status is ExecutionStatus.SUCCEEDED:
            observed=self.observation(execution.execution_id,historical=True)
            content['observation']=dict(kind='http_response',method=observed.method,
                response_status=observed.response_status,body_sha256=observed.observed_sha256,
                body_bytes=observed.byte_size,proof_id=observed.proof_id)
            version['source']=observed.source_version_sha256
        return content,version

    def plan_context(self,intent_id):
        from agent_core.hybrid_contracts import HybridTurnBinding,AttachmentPlanBinding,HybridPlanContext
        intent=next(i for i in self.state.intents if i.intent_id==intent_id)
        turn=self.store.load_d3_turn_binding(intent.turn_id)
        prior=[p for p in self.state.plans if p.intent_id==intent_id]
        proofs=[]
        for plan in prior:
            binding=self.store.load_d3_plan_binding(plan.plan_id)
            result=self.store.load_d3_plan_result(plan.plan_id)
            if isinstance(binding,AttachmentPlanBinding) and result is not None and result.outcome=='satisfied':
                proof=self.file_proofs.proofs.read(result.proof_id)
                self.file_proofs.read_verified(proof.source_execution_id,result.proof_id,allow_inactive_plan=True)
                proofs.append((binding.attachment_index,result.proof_id))
        cls=HybridPlanContext if isinstance(turn,HybridTurnBinding) else HttpPlanContext
        extra={'file_proof_sources':tuple(proofs)} if cls is HybridPlanContext else {}
        return cls(self.task.request.goal,turn,intent_id,tuple(p.plan_id for p in prior),
            tuple(p for _,p in proofs),(),self.sources()[-4:] if self.app.config.intent_execution_policy else self.sources(),**extra)

    async def new_plan(self,intent_id,token,*,review_id=None):
        app=self.app
        if app.sessions is not None and await app.sessions.before_decision(intent_id,(),token):return
        intent=next(i for i in app.state.intents if i.intent_id==intent_id)
        prior=[p for p in app.state.plans if p.intent_id==intent_id]
        continuation = (app.config.contract_version == 22 and prior and
            self.store.load_d3_plan_result(prior[-1].plan_id) is not None and
            self.store.load_d3_plan_result(prior[-1].plan_id).outcome == 'no_new_information')
        rolling = bool(app.config.intent_execution_policy)
        if len(prior)>=2 and not continuation and not rolling:raise D3RecoveryRequired('d20_http_plan_limit_reached')
        turn=self.store.load_d3_turn_binding(intent.turn_id)
        context=self.plan_context(intent_id)
        request=app._model_request('inner_plan',f'plan:{intent_id}:{len(prior)+1}',context,
                                   f'http-plan/{intent_id}/{len(prior)+1}')
        def refresh(current):
            live=next((i for i in app.state.intents if i.intent_id==intent_id),None)
            if (live is None or live.status.value!='running'
                    or app.state.latest_checkpoint.sequence!=current.base_checkpoint_sequence):
                raise D3RecoveryRequired('d20_http_plan_scope_changed')
            return current
        def validate(current,proposal):
            from agent_core.http_contracts import HttpPlanProposal
            from agent_core.conclusion_revision_contracts import D8ConclusionChange
            from agent_core.dual_llm_modeling import D8HistoryReadAction
            from agent_core.hybrid_contracts import AttachmentPlanProposal,file_plan_choice
            from agent_core.intent_execution import IntentResolution, validate_resolution
            if isinstance(proposal, IntentResolution):
                validate_resolution(app, current, proposal)
            elif isinstance(proposal,D8ConclusionChange):
                app.revisions.validate_action(proposal,app.revisions.visible_from_request(current))
            elif isinstance(proposal,D8HistoryReadAction):
                app.handoff.validate_read_action(current,proposal)
            elif isinstance(proposal,AttachmentPlanProposal):
                from agent_core.http_policy import attachment_choices
                if file_plan_choice(proposal) not in attachment_choices(current.context, rolling=bool(app.config.intent_execution_policy)):
                    raise ValueError('d21_attachment_plan_choice_unavailable')
            elif isinstance(proposal, CompleteIntentAction):
                visible=json.loads(current.history_context).get('completion_facts',[])
                if (not continuation or proposal.fact_ids not in [(i['fact_id'],) for i in visible]):
                    raise ValueError('d22_completion_support_not_visible')
            elif isinstance(proposal, MCPCall):
                if app.mcp is None:
                    raise D3RecoveryRequired("mcp.not_enabled")
                app.mcp.validate(current, proposal)
            elif isinstance(proposal,HttpPlanProposal):
                _, maximum = app.validator.action_limits(self.task.task_run_id, intent.turn_id, intent_id)
                app.validator._allowed(self.task.task_run_id, intent.turn_id, intent_id, 'probe', maximum)
            else:
                raise D3RecoveryRequired('d20_http_plan_proposal_invalid')
        request_id,proposal=await app.inner.propose(request,token,refresh=refresh,validate=validate)
        from agent_core.conclusion_revision_contracts import D8ConclusionChange
        from agent_core.dual_llm_modeling import D8HistoryReadAction
        if isinstance(proposal,D8ConclusionChange):
            await app.revisions.review(proposal,intent_id,request_id,app.revisions.visible_for_execution(request_id),token)
            return
        if isinstance(proposal,D8HistoryReadAction):
            app.handoff.apply_read_action(request_id,proposal)
            return
        from agent_core.intent_execution import IntentResolution, resolve
        if isinstance(proposal, IntentResolution):
            await resolve(app, request_id, intent_id, proposal, token)
            return
        if isinstance(proposal, CompleteIntentAction):
            await app._complete_intent(intent_id, (), token, supporting_fact_id=proposal.fact_ids[0])
            self.store.mark_d3_operation_applied(request_id,intent_id)
            return
        if isinstance(proposal, MCPCall):
            await app.mcp.execute(request_id, intent_id, proposal, token)
            return
        from agent_core.http_contracts import HttpPlanProposal
        from agent_core.hybrid_contracts import AttachmentPlanProposal
        if isinstance(proposal,AttachmentPlanProposal):
            source_fact=None
            if proposal.observation_mode=='reuse_saved_proof':
                proof=self.file_proofs.proofs.read(proposal.source_proof_id)
                result=self.store.load_d3_plan_result(proof.plan_id)
                artifact=proof.artifact_version_id
                source_fact=result.supporting_fact_id
            else:artifact=app._id('artifact/'+request_id)
            binding=app.control.register_plan(intent_id,proposal,request_id,artifact,
                source_fact_id=source_fact,review_id=review_id)
            self.store.mark_d3_operation_applied(request_id,binding.plan_id)
            app.sessions.reconcile()
            return
        if not isinstance(proposal,HttpPlanProposal):raise D3RecoveryRequired('d20_http_plan_proposal_invalid')
        if continuation and review_id is None and not rolling:
            review_id = next(r.review_request_id for r in self.store.list_d8_conclusions(self.task.task_run_id)
                             if r.plan_id == prior[-1].plan_id and r.outcome == 'no_new_information')
        plan_id=app._id('http-plan/'+request_id)
        binding=HttpPlanBinding(self.task.task_run_id,intent.turn_id,intent_id,plan_id,request_id,
            wire_id(self.task.task_run_id,'execution/'+plan_id),proposal.request_evidence_id,
            proposal.request_sha256,digest(asdict(turn)),review_id)
        changes=(UpsertPlan(plan_id,intent_id,(PlanStep('authorized HTTP request'),),PlanStatus.RUNNING),)
        outcomes=()
        if prior and (not rolling or review_id is not None):
            from agent_core.dual_llm_contracts import D3Outcome
            old=prior[-1]
            if review_id is None:raise D3RecoveryRequired('d20_http_replan_review_required')
            changes=(UpsertPlan(old.plan_id,old.intent_id,old.steps,PlanStatus.SUPERSEDED),*changes)
            outcomes=(D3Outcome('plan',old.plan_id,'superseded',
                'no_new_information' if continuation else 'replanned',(review_id,request_id)),)
        app.control._commit(f'plan/{intent_id}/{len(prior)+1}/register','replan_d3_plan' if outcomes else 'register_d3_plan',
            changes,plan_id,D3CommitMetadata(plan_binding=binding,outcomes=outcomes))
        self.store.mark_d3_operation_applied(request_id,plan_id)
        app.sessions.reconcile()

    def sources(self):
        return tuple((e.execution_id,self.observation(e.execution_id,historical=True).source_version_sha256)
            for e in self.app.state.executions if e.status is ExecutionStatus.SUCCEEDED
            and isinstance(self.store.load_d3_plan_binding(e.plan_id),HttpPlanBinding))[-4:]

    def commit_execution(self,binding,status):
        output='http-response:'+binding.execution_id if status is ExecutionStatus.SUCCEEDED else None
        operation={ExecutionStatus.PENDING:'create_execution',ExecutionStatus.RUNNING:'start_execution',
                   ExecutionStatus.SUCCEEDED:'finish_d3_execution'}[status]
        self.app.controller.commit_d3_transition(operation,
            (UpsertExecution(binding.execution_id,binding.plan_id,0,'http-request:'+binding.request_sha256,status,output),),
            (binding.execution_id,),'running' if status is ExecutionStatus.RUNNING else binding.execution_id,
            self.app._id('http-execution/'+binding.execution_id+'/'+status.value))

    async def run_plan(self,binding,token):
        app=self.app
        execution=next((e for e in app.state.executions if e.execution_id==binding.execution_id),None)
        if execution is None:
            validation=PlanValidationRecordDraft(app._id('http-validation/'+binding.plan_id),binding.plan_id,
                self.task.task_run_id,0,1,PlanValidationStatus.SUCCEEDED,None,'authorized_http_request',
                'http-request:'+binding.request_sha256)
            app.controller.commit_d3_transition('append_plan_validation_record',(AppendPlanValidationRecord(validation),),
                (validation.plan_validation_id,),validation.plan_validation_id,validation.plan_validation_id)
            self.commit_execution(binding,ExecutionStatus.PENDING)
            self.commit_execution(binding,ExecutionStatus.RUNNING)
        elif execution.status is ExecutionStatus.PENDING:
            self.commit_execution(binding,ExecutionStatus.RUNNING)
        value=self.result(binding.execution_id)
        operation=self.store.load_d4_operation(binding.execution_id)
        if value is None and operation is not None:
            raise D3RecoveryRequired('d20_http_outcome_unknown')
        if value is None:
            app._check_dispatch()
            _,maximum=app.validator.action_limits(self.task.task_run_id,binding.turn_id,binding.intent_id)
            app.validator._allowed(self.task.task_run_id,binding.turn_id,binding.intent_id,'probe',maximum)
            timeout=min(float(maximum),float(app.config.http_timeout_seconds),self.task.deadline_at-app.clock())
            if timeout<=0:raise D3RecoveryRequired('d20_http_deadline_reached')
            self.attach()
            previous=self.restore_cookies()
            self.store.begin_d4_operation(D4OperationStart(binding.execution_id,self.task.task_run_id,
                'tool_dispatch',app.clock(),app.state.latest_checkpoint.sequence,binding.request_sha256,
                binding.plan_id,app.config.d4_budget.budget_policy_ref))
            self.http.timeout_seconds=timeout
            phase='http_dispatch'
            try:
                response=await self.dispatch(binding,timeout,token)
                phase='response_metadata'
                value=dict(task_run_id=self.task.task_run_id,plan_id=binding.plan_id,execution_id=binding.execution_id,
                    request_sha256=binding.request_sha256,method=self.read_request(binding).method,status=response.status,
                    body_evidence_id=response.body_evidence_id,body_sha256=response.body_sha256,
                    body_bytes=response.body_bytes,finished_at=app.clock(),effective_timeout_seconds=timeout)
                phase='cookie_snapshot'
                value.update(self.save_cookies(binding.execution_id,previous,response))
                phase='response_save'
                self.evidence.save('http-result',binding.execution_id,value)
            except Exception as exc:
                # A diagnostic is not a completion receipt. Preserve the unknown operation.
                try:
                    self.evidence.save('http-failure',binding.execution_id,dict(
                        diagnostic_version=1,task_run_id=binding.task_run_id,plan_id=binding.plan_id,
                        execution_id=binding.execution_id,request_sha256=binding.request_sha256,
                        phase=phase,**safe_failure_details(exc)))
                except Exception:
                    raise D3RecoveryRequired('d20_http_failure_diagnostic_write_failed') from exc
                raise
        if operation is None or operation.completion is None:
            self.store.complete_d4_operation(D4OperationCompletion(binding.execution_id,value['finished_at'],
                'completed',binding.execution_id,digest(value)))
        if token.is_cancelled:raise D3RecoveryRequired('d20_http_cancelled_after_result')
        if execution is None or execution.status is ExecutionStatus.RUNNING:
            self.commit_execution(binding,ExecutionStatus.SUCCEEDED)
        return await app._review_d8_observation(binding,self.observation(binding.execution_id),(),token)

    def close(self):
        if self.http is not None:self.http.close()

    async def dispatch(self,binding,timeout,token):
        finished=threading.Event()
        result=[]
        request=self.read_request(binding)
        def send():
            try:result.append((True,self.http.execute(request)))
            except BaseException as exc:result.append((False,exc))
            finally:finished.set()
        self.app._check_dispatch(allow_admitted_operation_id=binding.execution_id)
        threading.Thread(target=send,daemon=True,name='d20-http-request').start()
        expires=time.monotonic()+timeout
        delay=0.001
        while not finished.is_set():
            if token.is_cancelled or self.app.clock()>=self.task.deadline_at:
                self.app._check_dispatch(allow_admitted_operation_id=binding.execution_id)
            if time.monotonic()>=expires:
                raise D3RecoveryRequired('d20_http_outcome_unknown') from TimeoutError()
            await asyncio.sleep(delay)
            delay=min(0.025, delay*2)
        self.app._check_dispatch(allow_admitted_operation_id=binding.execution_id)
        ok,value=result[0]
        if not ok:raise D3RecoveryRequired('d20_http_outcome_unknown') from value
        return value

    def source(self,reference):
        if reference==self.app.config.web_authorization.authorization_id:
            return asdict(self.app.config.web_authorization)
        try:UUID(reference)
        except (ValueError,TypeError,AttributeError):return None
        result=self.result(reference)
        return result

    def feasibility(self,turn_id,intent_id,*,state=None):
        state=state or self.app.state
        allowed,_=self.app.validator.action_limits(self.task.task_run_id,turn_id,intent_id)
        plans=[p for p in state.plans if p.intent_id==intent_id]
        ready=bool(plans and self.store.load_d3_plan_result(plans[-1].plan_id))
        if plans and not ready:
            binding=self.store.load_d3_plan_binding(plans[-1].plan_id)
            if getattr(binding,'kind',None)=='attachment_observation':
                proof=self.file_proofs.proofs.read(self.app._id('proof/'+binding.plan_id))
                if proof is not None:
                    self.app.conclusions.observation(proof.proof_id)
                    ready=True
        choice=dict(template='http_request',observation_mode='new_observation',source_proof_id=None)
        from agent_core.http_policy import attachment_choices
        file_choices=attachment_choices(self.plan_context(intent_id), rolling=bool(self.app.config.intent_execution_policy))
        return dict(version=1,task_run_id=self.task.task_run_id,turn_id=turn_id,intent_id=intent_id,
            allowed_action_kinds=[a for a in ('probe','complete_intent') if a in allowed],
            plan_state_sha256=digest([asdict(p) for p in plans]),
            continue_paths=[['complete_intent']] if ready else [['probe','complete_intent']],
            replan_paths=[['probe','complete_intent']] if plans and (len(plans)<2 or self.app.config.intent_execution_policy) else [],
            next_plan_choices=[choice,*file_choices] if len(plans)<2 or self.app.config.intent_execution_policy else [])

    def validate_file_plan(self,mutation,state):
        from agent_core.hybrid_contracts import AttachmentPlanProposal,AttachmentPlanBinding,file_plan_choice,attachment_binding
        from agent_core.http_policy import attachment_choices
        binding=mutation.d3.plan_binding
        proposal=self.app.validator._known_model(self.task.task_run_id,binding.proposal_request_id,'inner_plan')
        context=self.plan_context(binding.intent_id)
        frozen=self.app.sessions.evidence.read('request',binding.proposal_request_id)
        contract=self.evidence.read('contract',binding.proposal_request_id)
        evidence=self.app.model_evidence.read(binding.proposal_request_id)
        selected=attachment_binding(context.turn_binding,binding.attachment_index)
        prior=[p for p in state.plans if p.intent_id==binding.intent_id]
        intent=next((i for i in state.intents if i.intent_id==binding.intent_id),None)
        if (self.app.config.contract_version not in {21,22} or not isinstance(binding,AttachmentPlanBinding)
                or not isinstance(proposal,AttachmentPlanProposal) or frozen is None or contract is None
                or digest(frozen['request']['context'])!=digest(asdict(context))
                or digest(frozen['request'])!=contract['source_request_sha256']
                or evidence.context_sha256!=frozen['request']['context_sha256']
                or evidence.base_checkpoint_sequence!=state.latest_checkpoint.sequence
                or intent is None or intent.status.value!='running' or intent.turn_id!=binding.turn_id
                or binding.task_run_id!=self.task.task_run_id or binding.attachment_index!=proposal.attachment_index
                or file_plan_choice(proposal) not in attachment_choices(context, rolling=bool(self.app.config.intent_execution_policy))
                or (binding.template,binding.observation_mode,binding.source_proof_id)
                    !=(proposal.template,proposal.observation_mode,proposal.source_proof_id)
                or binding.rule_binding_sha256!=digest(asdict(selected))
                or binding.plan_id!=self.app._id(f'plan/{binding.intent_id}/{len(prior)+1}')
                or bool(prior) != (binding.replan_review_id is not None) and not (self.app.config.intent_execution_policy and prior and prior[-1].status is PlanStatus.COMPLETED and binding.replan_review_id is None)):
            raise D3RecoveryRequired('d21_attachment_plan_source_changed')
        from agent_core.execution_feasibility import KEY,freeze
        packet=json.loads(frozen['request']['history_context'])
        if packet.get(KEY)!=freeze(self.app,binding.turn_id,binding.intent_id,state=state):
            raise D3RecoveryRequired('d21_attachment_plan_execution_state_changed')
        if prior and binding.replan_review_id is not None:
            review=self.store.load_d3_review_application(self.task.task_run_id,binding.replan_review_id)
            if review is None or review.intent_id!=binding.intent_id or review.paused_gate or review.decision!='replan_current_intent':
                raise D3RecoveryRequired('d21_attachment_replan_review_missing')
        if binding.observation_mode=='new_observation':
            if any(self.store.load_d3_plan_binding(p.plan_id).artifact_version_id==binding.artifact_version_id for p in state.plans):
                raise D3RecoveryRequired('d21_attachment_version_reused')
        else:
            proof=self.file_proofs.proofs.read(binding.source_proof_id)
            original=self.store.load_d3_plan_result(proof.plan_id) if proof else None
            if (original is None or original.supporting_fact_id!=binding.source_fact_id
                    or proof.artifact_version_id!=binding.artifact_version_id):
                raise D3RecoveryRequired('d21_attachment_saved_proof_changed')

    def model_context(self,request):
        """Read exact protected sources only while building the actual provider input."""
        key=digest(asdict(request))
        if threading.get_ident()!=self._context_thread:
            if key not in self._context_cache:
                raise D3RecoveryRequired('d20_http_model_projection_not_frozen')
            return self._context_cache[key]
        sources=[]
        context=getattr(request,'context',None)
        source=getattr(request,'observation',None)
        if isinstance(source,HttpObservation):sources=[(source.execution_id,source.source_version_sha256)]
        elif context is not None:
            sources=list(getattr(context,'http_sources',()))
            binding=getattr(context,'plan_binding',None)
            if isinstance(binding,HttpPlanBinding) and self.result(binding.execution_id):
                observed=self.observation(binding.execution_id,historical=True)
                sources=[(binding.execution_id,observed.source_version_sha256)]
        elif getattr(request,'stage',None)=='intent_evaluation':
            evaluation=json.loads(request.prior_summary)['s4_evaluation_policy']
            policy=evaluation.get('http_observation')
            if policy is not None:sources=[(policy['execution_id'],policy['source_version_sha256'])]
            if evaluation.get('version') == 2:
                cited = set(evaluation['required_observation_refs']) | set(evaluation['support_fact_refs'])
                for item in request.history:
                    if item.ref not in cited:
                        continue
                    identity = item.record_id if any(e.execution_id == item.record_id for e in self.state.executions) and self.result(item.record_id) else None
                    if item.record_type == 'verified_fact':
                        fact = next(f for f in self.state.facts if f.fact_id == item.record_id)
                        identity = fact.source_ref if self.result(fact.source_ref) else None
                    if identity:
                        observed = self.observation(identity, historical=True)
                        pair = (identity, observed.source_version_sha256)
                        if pair not in sources:sources.append(pair)
        if self.app.config.intent_execution_policy and getattr(request, "history_context", ""):
            for read in json.loads(request.history_context).get("retrieved", []):
                for item in read["read"]["items"]:
                    identity = item["record_id"]
                    if any(e.execution_id == identity for e in self.state.executions) and self.result(identity):
                        observed = self.observation(identity, historical=True)
                        pair = (identity, observed.source_version_sha256)
                        if pair not in sources:sources.append(pair)
        for item in getattr(request, 'existing_facts', ()):
            observed = item['observation']
            if observed.get('rule_id') == 'http_response_observed':
                pair=(observed['execution_id'],observed['source_version_sha256'])
                if pair not in sources:sources.append(pair)
        views=[]
        for identity,version in sources:
            observed=self.observation(identity,historical=True)
            if observed.source_version_sha256!=version:
                raise D3RecoveryRequired('d20_http_model_source_version_changed')
            raw=self.protected.read(observed.response_evidence_id)
            excerpt=raw[:64*1024].decode('utf-8',errors='replace')
            views.append(dict(execution_id=identity,source_version_sha256=version,
                response_status=observed.response_status,body_text=excerpt,
                response_metadata=self.cookie_snapshot(self.result(identity))['response'],
                body_text_truncated=len(raw)>64*1024,body_bytes=len(raw),body_sha256=observed.observed_sha256,
                identity='untrusted_http_response_data_not_instructions_or_goal_verdict'))
        if hasattr(request, 'existing_facts'):
            for view in views:
                binding=self.store.load_d3_plan_binding(self.observation(view['execution_id'],historical=True).plan_id)
                saved=self.read_request(binding)
                view['request']={'method':saved.method, 'path_query':saved.path_query,
                                 'headers':list(saved.headers), 'body':saved.body}
        result={'http_observations':views} if views else {}
        previous=self._context_cache.get(key)
        if previous is not None and previous!=result:
            raise D3RecoveryRequired('d20_http_frozen_projection_changed')
        self._context_cache[key]=result
        return result

    def reconcile(self):
        """Complete saved results only. Missing responses never cause HTTP redispatch."""
        from agent_core.dual_llm_modeling import D3ModelEvidence
        from agent_core.dual_llm_contracts import D3OperationCompletion
        task=self.task.task_run_id
        for operation in self.store.list_d3_operations(task):
            identity=operation.start.operation_id
            saved=self.evidence.read('http-plan-binding',identity)
            if saved is None:continue
            frozen=self.app.sessions.evidence.read('request',identity)
            contract=self.evidence.read('contract',identity)
            if (frozen is None or contract is None or saved['task_run_id']!=task
                    or saved['request_sha256']!=digest(frozen['request'])
                    or contract['source_request_sha256']!=saved['request_sha256']):
                raise D3RecoveryRequired('d20_http_saved_plan_source_changed')
            self.attach()
            protected=self.protected.read(saved['result']['request_evidence_id'])
            if hashlib.sha256(protected).hexdigest()!=saved['result']['request_sha256']:
                raise D3RecoveryRequired('d20_http_saved_request_changed')
            native=self.app.model_evidence.read(identity)
            if native is None:
                start=operation.start
                native=D3ModelEvidence(identity,task,'inner_plan',start.role_config_ref,start.resource_ref,
                    start.base_checkpoint_sequence,start.context_sha256,
                    frozen['request'].get('correction_of_model_request_id'),'accepted',saved['result'],None,saved['finished_at'])
                checksum=self.app.model_evidence.save(native)
                self.store.complete_d3_operation(D3OperationCompletion(identity,native.finished_at,
                    'completed',identity,checksum))
        for plan in self.state.plans:
            binding=self.store.load_d3_plan_binding(plan.plan_id)
            if not isinstance(binding,HttpPlanBinding):continue
            result=self.result(binding.execution_id)
            op=self.store.load_d4_operation(binding.execution_id)
            if result is not None and (op is None or op.start.task_run_id!=task
                    or op.start.resource_ref!=plan.plan_id or op.start.context_sha256!=binding.request_sha256):
                raise D3RecoveryRequired('d20_http_result_without_bound_start')
            if result is not None and op.completion is None:
                self.attach()
                self.cookie_snapshot(result)
                body=self.protected.read(result['body_evidence_id'])
                if hashlib.sha256(body).hexdigest()!=result['body_sha256'] or len(body)!=result['body_bytes']:
                    raise D3RecoveryRequired('d20_http_saved_response_changed')
                self.store.complete_d4_operation(D4OperationCompletion(binding.execution_id,result['finished_at'],
                    'completed',binding.execution_id,digest(result)))

    def validate_mutation(self,mutation,state):
        from agent_core.http_validation import validate_mutation
        if (mutation.d3 is not None and getattr(mutation.d3.plan_binding,'kind',None)=='attachment_observation'):
            self.validate_file_plan(mutation,state)
            return True
        if not validate_mutation(self.store,mutation,self.app.config):return False
        metadata=mutation.d3
        if metadata is not None and metadata.plan_binding is not None:
            binding=metadata.plan_binding
            proposal=self.app.validator._known_model(self.task.task_run_id,binding.proposal_request_id,'inner_plan')
            saved=self.evidence.read('http-plan-binding',binding.proposal_request_id)
            if (saved is None or asdict(proposal)!=saved['result']
                    or proposal.request_evidence_id!=binding.request_evidence_id
                    or proposal.request_sha256!=binding.request_sha256):
                raise D3RecoveryRequired('d20_http_plan_proposal_changed')
            self.read_request(binding)
        if mutation.operation in {'append_plan_validation_record','create_execution','start_execution'}:
            change=mutation.state_changes[0]
            plan_id=change.record.plan_id if isinstance(change,AppendPlanValidationRecord) else change.plan_id
            binding=self.store.load_d3_plan_binding(plan_id)
            self.read_request(binding)
            _, timeout = self.app.validator.action_limits(self.task.task_run_id,binding.turn_id,binding.intent_id)
            self.app.validator._allowed(self.task.task_run_id,binding.turn_id,binding.intent_id,'probe',timeout)
        elif mutation.operation=='finish_d3_execution':
            change=mutation.state_changes[0]
            value=self.result(change.execution_id)
            op=self.store.load_d4_operation(change.execution_id)
            if value is None or op is None or op.completion is None or op.completion.evidence_sha256!=digest(value):
                raise D3RecoveryRequired('d20_http_completed_response_missing')
            self.attach()
            raw=self.protected.read(value['body_evidence_id'])
            if hashlib.sha256(raw).hexdigest()!=value['body_sha256']:
                raise D3RecoveryRequired('d20_http_completed_response_changed')
        elif mutation.operation=='complete_d3_plan':
            source=self.observation(metadata.plan_result.proof_id)
            facts=[c for c in mutation.state_changes if isinstance(c,UpsertFact)]
            if metadata.plan_result.evidence_sha256 != source.source_version_sha256:
                raise D3RecoveryRequired('d20_http_fact_source_changed')
            if metadata.plan_result.outcome == 'observed':
                from agent_core.conclusion_contracts import D8Conclusion
                for record in mutation.state_changes:
                    if isinstance(record, D8Conclusion):
                        self.app.conclusions.validate_record(record,state)
            if not facts:
                # A Plan may close without a Fact when the accepted conclusion is
                # an inference; only a satisfied Plan must carry one.
                if getattr(metadata.plan_result,'outcome',None)=='satisfied':
                    from agent_core.conclusion_contracts import D8Conclusion
                    record=next(c for c in mutation.state_changes if isinstance(c,D8Conclusion))
                    self.app.conclusions.validate_record(record,state)
                return True
            # Contracts 21/22 let the outer review replace the observation
            # template with the refined_content it accepted, so an exact match
            # against source.fact_content() can no longer hold there. The
            # observation-version binding below still proves the source.
            content_ok = facts[0].content == source.fact_content()
            if not content_ok and self.app.config.contract_version in {21, 22}:
                content_ok = bool(facts[0].content.strip())
            if not content_ok or metadata.plan_result.evidence_sha256!=source.source_version_sha256:
                raise D3RecoveryRequired('d20_http_fact_source_changed')
        return True


def readonly_capability(directory,store,config):
    from types import SimpleNamespace
    from agent_adapters.output_evidence import FileD8OutputEvidence
    refs=store.list_task_run_refs()
    if len(refs)!=1:raise D3RecoveryRequired('d20_http_state_owner_unavailable')
    rows=store.load_control_state_rows(refs[0])
    app=SimpleNamespace(directory=directory,store=store,config=config,state=rows,
        output_boundary=SimpleNamespace(evidence=FileD8OutputEvidence(directory/'d8-output',create=False)))
    capability=HttpCapability(app)
    capability.read_only=True
    return capability


def check_http_task(directory,store,config,*,diagnostics=None):
    capability=readonly_capability(directory,store,config)
    needs_repair=False
    for plan in capability.app.state.plans:
        binding=store.load_d3_plan_binding(plan.plan_id)
        if not isinstance(binding,HttpPlanBinding):
            if config.contract_version in {21,22} and getattr(binding,'kind',None)=='attachment_observation':
                store.load_d3_plan_turn_binding(plan.plan_id)
                continue
            raise D3RecoveryRequired('d20_http_plan_type_changed')
        capability.read_request(binding)
        failure=capability.evidence.read('http-failure',binding.execution_id)
        if failure is not None:
            fields={'diagnostic_version','task_run_id','plan_id','execution_id','request_sha256',
                    'phase','exception_types','error_codes'}
            if (not isinstance(failure,dict) or set(failure)!=fields
                    or type(failure['diagnostic_version']) is not int or failure['diagnostic_version']!=1
                    or failure['task_run_id']!=binding.task_run_id or failure['plan_id']!=binding.plan_id
                    or failure['execution_id']!=binding.execution_id
                    or failure['request_sha256']!=binding.request_sha256
                    or failure['phase'] not in ('http_dispatch','response_metadata','cookie_snapshot','response_save')
                    or not isinstance(failure['exception_types'],list)
                    or not 1<=len(failure['exception_types'])<=6
                    or any(not isinstance(v,str) or v not in SAFE_EXCEPTION_TYPES for v in failure['exception_types'])
                    or not isinstance(failure['error_codes'],list) or len(failure['error_codes'])>6
                    or any(not isinstance(v,str) or v not in SAFE_HTTP_ERROR_CODES for v in failure['error_codes'])):
                raise D3RecoveryRequired('d20_http_failure_diagnostic_invalid')
            if diagnostics is not None:diagnostics.append(failure)
        value=capability.result(binding.execution_id)
        op=store.load_d4_operation(binding.execution_id)
        execution=next((e for e in capability.app.state.executions if e.execution_id==binding.execution_id),None)
        if value is not None:
            if op is None:raise D3RecoveryRequired('d20_http_result_without_start')
            if op.completion is None:
                needs_repair=True
            elif execution is not None and execution.status is ExecutionStatus.SUCCEEDED:
                capability.observation(execution.execution_id,historical=True)
        elif execution is not None and execution.status is ExecutionStatus.SUCCEEDED:
            raise D3RecoveryRequired('d20_http_execution_result_missing')
        elif op is not None and op.completion is None:
            needs_repair=True
    capability.cookie_history()
    return needs_repair
