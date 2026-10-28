"""Container-local conditional experiments over the existing trusted HTTP port."""
import asyncio
import base64
import hashlib
import json
import time
from dataclasses import asdict

from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
from agent_core.dual_llm_modeling import parse_d3_plan_proposal
from agent_core.actions import MCPCall
from agent_core.operation_accounting import D4OperationStart
from agent_core.stage_storage import StageStateError
from agent_core.turn_modeling import digest
from agent_adapters.web.http_adapter import D7HttpRequest, HttpCapabilityError, D7HttpPolicy
from Sandbox.http_experiment import HttpExperimentProcess, MAX_SCRIPT_BYTES
from Sandbox.task_run_sandbox import TaskRunSandboxError
from qinyuan_core import get_logger

TOOL = 'http.run_experiment'
SCRIPT_ERRORS = frozenset({'SyntaxError','NameError','TypeError','ValueError','KeyError','IndexError',
    'AttributeError','ZeroDivisionError','AssertionError','ImportError','ModuleNotFoundError','PermissionError',
    'RuntimeError','TimeoutError','MemoryError','RecursionError','OSError'})
SPEC = dict(qualified_name=TOOL, tags=['http','conditional-experiment','authorized-target-channel'],
    description='[authorized-target-channel] Run Python branches/loops in a networkless container. Use request(method,path_query,headers={},body="") for target HTTP through the trusted forwarder; never use sockets, subprocesses or localhost as a substitute for the remote target. request returns status, body bytes, text and evidence_ref. HTTP uses only the authorized origin, persistent cookies and per-request evidence. Print a concise unreviewed summary.',
    input_schema={'type':'object','properties':{'script':{'type':'string','minLength':1,'maxLength':MAX_SCRIPT_BYTES},
        'timeout_seconds':{'type':'integer','minimum':5,'maximum':300}},
        'required':['script','timeout_seconds'],'additionalProperties':False})


def failure_diagnostics(evidence, store, task_id):
    """Read-only failure projection bound to the original operation and saved receipt."""
    from agent_adapters.output_evidence import SAFE_MODEL_ERROR_CODES
    rows=[]
    for operation in store.list_d4_operations(task_id):
        if '/experiment/' not in operation.start.resource_ref:continue
        identity=operation.start.operation_id
        failure=evidence.read('experiment-failure',identity)
        if failure is None:continue
        claim=evidence.read('mcp-call',identity)
        result=evidence.read('mcp-result',identity)
        if (claim is None or claim['task_run_id'] != task_id
                or failure['model_request_id'] != claim['model_request_id']
                or failure['request_sha256'] != digest(claim) or operation.start.context_sha256 != digest(claim)
                or failure['phase'] not in {'http_execute','receipt_save','receipt_apply'}
                or failure['code'] not in SAFE_MODEL_ERROR_CODES
                or failure['result_sha256'] is not None and (result is None or failure['result_sha256'] != digest(result))):
            raise ExecutionRecoveryRequired('d22_experiment_failure_diagnostic_invalid')
        parent=store.load_d3_operation(claim['model_request_id'])
        rows.append(dict(execution_id=identity,model_request_id=claim['model_request_id'],
            phase=failure['phase'],code=failure['code'],result_saved=failure['result_sha256'] is not None,
            active=operation.completion is None or parent is None or parent.application_ref is None,
            category='unknown' if failure['phase']=='http_execute' else 'integrity' if failure['phase']=='receipt_apply' else 'persistence',
            exception_types=failure['exception_types'],error_codes=failure['error_codes']))
    return rows


class _StrategyStop(Exception):
    pass


class HttpExperiments:
    def __init__(self, mcp):
        self.mcp, self.app = mcp, mcp.app
        self.runner_factory = HttpExperimentProcess  # Local test/assembly seam.
        self.live = None
        self.log = get_logger('exec')

    @property
    def enabled(self):
        return self.app.web is not None and self.app.config.intent_execution_policy == 'continuous_v1'

    @property
    def evidence(self):
        return self.mcp.evidence

    @property
    def stage(self):
        return self.app.store.load_stage_run_config(self.app.state.task_run.task_run_id) is not None

    def _limits(self, owner, *, tool_resource=None):
        app = self.app
        if self.stage:
            turn = next(t for t in app.store.list_stage_turns(app.task_id) if any(r.road_id == owner for r in t.roads))
            return app.validator.action_limits(app.task_id, turn.turn_id, owner, tool_resource=tool_resource)[1]
        turn_id = next(i.turn_id for i in app.state.intents if i.intent_id == owner)
        _, maximum = app.validator.action_limits(app.state.task_run.task_run_id, turn_id, owner)
        app.validator._allowed(app.state.task_run.task_run_id, turn_id, owner, 'probe', maximum)
        return maximum

    async def _strategy(self, owner=None, *, start=False):
        if self.stage:
            if owner is not None:
                await self.app._supervise(owner, self.app._token)
            return False
        self.app.supervision.poll()
        if start and owner is not None and self.app.sessions.due(owner):
            self.app.supervision.start(owner)
        return owner is not None and self.app.supervision.stop(owner)

    def _apply_parent(self, parent):
        if self.stage:
            self.app.store.mark_d4_operation_applied(parent, parent)
        else:
            self.app.store.mark_d3_operation_applied(parent, parent)

    def validate(self, call):
        args = call.arguments
        if (not self.enabled or set(args) != {'script','timeout_seconds'}
                or not isinstance(args['script'],str) or not args['script'].strip() or '\x00' in args['script']
                or len(args['script'].encode('utf-8')) > MAX_SCRIPT_BYTES
                or type(args['timeout_seconds']) is not int or not 5 <= args['timeout_seconds'] <= 300):
            raise ValueError('d22_experiment_arguments_invalid')

    def result(self, identity):
        value = self.evidence.read('experiment-result', identity)
        if value is None:
            return None
        session = self.evidence.read('experiment-session', identity)
        rows = self.records(identity)
        if (session is None or value['session_sha256'] != digest(session)
                or session['task_run_id'] != self.app.state.task_run.task_run_id
                or value['requests'] != [r.start.operation_id for r in rows]
                or any(r.completion is None for r in rows)):
            raise ExecutionRecoveryRequired('d22_experiment_session_changed')
        for ref in value['requests']:
            self.mcp.feedback(ref)
        return {**value, 'identity':'unreviewed_experiment_summary_with_independent_request_evidence'}

    def validate_receipt(self, operation, claim, result):
        """Recheck the protected originals, not script-reported responses."""
        web = self.app.web
        web.attach()
        source = claim['experiment_request']
        session = self.evidence.read('experiment-session',claim['model_request_id'])
        if (session is None or session['task_run_id'] != claim['task_run_id']
                or session['intent_id'] != claim['intent_id'] or session['call'] != claim['call']
                or session['authorization_sha256'] != web.policy()['authorization_sha256']):
            raise ExecutionRecoveryRequired('d22_experiment_source_changed')
        if self.stage:
            from agent_core.stage_contracts import PlanningSession
            parent = self.app.store.load_d4_operation(claim['model_request_id'])
            native = self.evidence.read_model_result(parent)
            if (parent.start.category != 'action' or parent.start.role != 'inner_action'
                    or native.session != PlanningSession('road_execution',claim['intent_id'])):
                raise ExecutionRecoveryRequired('d22_experiment_source_changed')
            proposal = native.result
        else:
            native = self.app.model_evidence.read(claim['model_request_id'])
            parent = self.app.store.load_d3_operation(claim['model_request_id'])
            if (native is None or parent is None or native.outcome != 'accepted' or native.stage != 'inner_plan'
                or native.task_run_id != claim['task_run_id'] or parent.start.task_run_id != claim['task_run_id']
                or parent.start.category != 'inner_model' or parent.start.role != 'inner_action'
                or native.role_config_ref != self.app.config.inner_role.role_config_ref
                or parent.start.context_sha256 != native.context_sha256
                or parent.start.base_checkpoint_sequence != native.base_checkpoint_sequence
                or parent.completion is None or parent.completion.result_kind != 'completed'
                or parent.completion.evidence_ref != claim['model_request_id']
                or parent.completion.evidence_sha256 != digest(asdict(native))):
                raise ExecutionRecoveryRequired('d22_experiment_source_changed')
            try:
                proposal = parse_d3_plan_proposal(native.raw)
            except (TypeError, ValueError) as error:
                raise ExecutionRecoveryRequired('d22_experiment_source_changed') from error
        from agent_core.stage_contracts import tool_calls
        if not any(asdict(call) == session['call'] for call in tool_calls(proposal)):
            raise ExecutionRecoveryRequired('d22_experiment_source_changed')
        raw = web.protected.read(source['evidence_id'])
        if hashlib.sha256(raw).hexdigest() != source['sha256']:
            raise ExecutionRecoveryRequired('d22_experiment_source_changed')
        fields = json.loads(raw)
        fields['headers'] = tuple(tuple(h) for h in fields['headers'])
        request = D7HttpRequest(operation.start.operation_id, **fields)
        method, _, _, _ = D7HttpPolicy.for_origin(web.policy()['endpoint']).validate(request)
        if method not in web.policy()['allowed_methods']:
            raise ExecutionRecoveryRequired('d22_experiment_source_changed')
        response = result['payload']['http_response']
        body = web.protected.read(response['body_evidence_id'])
        if (hashlib.sha256(body).hexdigest() != response['body_sha256'] or len(body) != response['body_bytes']
                or result['payload']['status'] != 'completed'
                or operation.start.resource_ref != 'mcp/'+claim['intent_id']+'/experiment/'+claim['model_request_id']+'/'+str(source['ordinal'])):
            raise ExecutionRecoveryRequired('d22_experiment_source_changed')
        web.cookie_snapshot(response)

    def record_failure(self, operation_id, claim, result, phase, error):
        from agent_adapters.output_evidence import safe_failure_details, SAFE_MODEL_ERROR_CODES
        code = str(error) if isinstance(error,ExecutionRecoveryRequired) and str(error) in SAFE_MODEL_ERROR_CODES else "d22_experiment_receipt_settlement_failed"
        if phase == "http_execute":code = "d22_experiment_request_outcome_unknown"
        if self.evidence.read("experiment-failure",operation_id) is None:
            self.evidence.save("experiment-failure",operation_id,dict(
                model_request_id=claim["model_request_id"], request_sha256=digest(claim),
                result_sha256=digest(result) if result is not None else None,
                phase=phase,code=code,**safe_failure_details(error,include_chain=True)))
        return code

    def projection(self, operation, claim, result):
        self.validate_receipt(operation,claim,result)
        return dict(call=dict(tool_name=TOOL,arguments=result['payload']['http_request']),result=result['payload'])

    def records(self, parent):
        return [r for r in self.mcp.records() if '/experiment/'+parent+'/' in r.start.resource_ref]

    def reconcile(self):
        # A lost script may have already changed the target. Never replay it.
        operations = (self.app.store.list_d4_operations(self.app.state.task_run.task_run_id) if self.stage else
                      self.app.store.list_d3_operations(self.app.state.task_run.task_run_id))
        for op in operations:
            identity = op.start.operation_id
            session = self.evidence.read('experiment-session',identity)
            if session is None or identity == self.live:
                continue
            rows = self.records(identity)
            if any(r.completion is None for r in rows):
                raise ExecutionRecoveryRequired('d22_experiment_request_outcome_unknown')
            result = self.result(identity)
            if result is None:
                process = self.evidence.read('experiment-process',identity)
                if process is None or process['sandbox_id'] != self.app.state.task_run.sandbox_id:
                    raise ExecutionRecoveryRequired('d22_experiment_process_reconciliation_required')
                # Read-only reconciliation: never restart/kill/replay a lost program.
                try:
                    status = self.app.http_sandbox._runtime.client.api.exec_inspect(process['exec_id'])
                except Exception as error:
                    raise ExecutionRecoveryRequired('d22_experiment_process_reconciliation_required') from error
                if status.get('ContainerID') != process['sandbox_id'] or status.get('Running') is not False:
                    raise ExecutionRecoveryRequired('d22_experiment_process_reconciliation_required')
                stopped = self.evidence.read('experiment-stop',identity)
                if stopped is not None:
                    if (stopped['session_sha256'] != digest(session)
                            or stopped['requests'] != [r.start.operation_id for r in rows]):
                        raise ExecutionRecoveryRequired('d22_experiment_session_changed')
                    # Reuse the retained intent to stop and observations. The
                    # fresh inspection above, not the failed close, proves exit.
                    restored = {**stopped, 'stop_diagnostic':{**stopped.get('stop_diagnostic',{}),
                        'process_stopped':True, 'phase':'reconciled_exit'}}
                else:
                    restored = dict(session_sha256=digest(session),
                        requests=[r.start.operation_id for r in rows],status='interrupted',
                        code='d22_experiment_process_lost',output='')
                self.evidence.save('experiment-result',identity,restored)
            self._apply_parent(identity)

    async def _event(self, runner, token, expires, intent_id=None):
        pending = asyncio.create_task(asyncio.to_thread(runner.receive))
        try:
            while not pending.done():
                await asyncio.wait({pending},timeout=0.025)
                if await self._strategy(intent_id):
                    raise _StrategyStop()
                if token.is_cancelled:
                    raise InterruptedError('d22_experiment_cancelled')
                if self.app.clock() >= self.app.state.task_run.deadline_at or time.monotonic() >= expires:
                    raise TimeoutError('d22_experiment_timeout')
            return pending.result()
        except BaseException:
            pending.cancel()
            raise

    async def request(self, parent, intent_id, call, ordinal, raw, token, expires):
        app, web = self.app, self.app.web
        resource = 'mcp/'+intent_id+'/experiment/'+parent+'/'+str(ordinal)
        if self.stage:
            app._check_dispatch(tool_resource=resource)
        else:
            app._check_dispatch()
        if set(raw) != {'kind','method','path_query','headers','body'} or not isinstance(raw['headers'],dict):
            raise ValueError('d22_experiment_request_invalid')
        maximum = self._limits(intent_id, tool_resource=resource)
        from agent_adapters.http_capability import wire_id
        identity = wire_id(app.state.task_run.task_run_id,'experiment/request/'+parent+'/'+str(ordinal))
        proposed = D7HttpRequest(identity,raw['method'],raw['path_query'],tuple(raw['headers'].items()),raw['body'])
        method,path,headers,body = web.http.policy.validate(proposed)
        if method not in web.policy()['allowed_methods']:
            raise ValueError('d22_experiment_method_not_authorized')
        saved = dict(method=method,path_query=path,headers=[list(h) for h in headers],body=body.decode('utf-8'))
        encoded = json.dumps(saved,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')
        metadata = web.protected.save(wire_id(app.state.task_run.task_run_id,'experiment/request-evidence/'+identity),encoded)
        claim = dict(task_run_id=app.state.task_run.task_run_id,intent_id=intent_id,model_request_id=parent,
            call=asdict(call),experiment_request=dict(ordinal=ordinal,evidence_id=metadata.evidence_id,
                sha256=hashlib.sha256(encoded).hexdigest()))
        prior = app.store.load_d4_operation(identity)
        if prior is not None:
            raise ExecutionRecoveryRequired('d22_experiment_request_replay_forbidden')
        previous = web.restore_cookies()
        self.evidence.save('mcp-call',identity,claim)
        timeout = min(maximum,app.config.http_timeout_seconds,app.state.task_run.deadline_at-app.clock(),expires-time.monotonic())
        if timeout <= 0:
            raise TimeoutError('d22_experiment_timeout')
        app.store.begin_d4_operation(D4OperationStart(identity,claim['task_run_id'],'tool_dispatch',app.clock(),
            app.state.latest_checkpoint.sequence,digest(claim),resource,
            app.config.d4_budget.budget_policy_ref))
        self.log.info('experiment.request_started',run=claim['task_run_id'],intent=intent_id,request=parent,exec=identity)
        web.http.timeout_seconds = timeout
        pending = asyncio.create_task(asyncio.to_thread(web.http.execute,proposed))
        phase, result = 'http_execute', None
        try:
            while not pending.done():
                await asyncio.wait({pending},timeout=0.025)
                await self._strategy(intent_id)
                if token.is_cancelled or app.clock() >= app.state.task_run.deadline_at or time.monotonic() >= expires:
                    raise ExecutionRecoveryRequired('d22_experiment_request_outcome_unknown')
            response = pending.result()
            phase = 'receipt_save'
            data = dict(execution_id=identity,method=method,status=response.status,
                body_evidence_id=response.body_evidence_id,body_sha256=response.body_sha256,body_bytes=response.body_bytes)
            data.update(web.save_cookies(identity,previous,response))
            payload = dict(status='completed',tool=TOOL,exit_code=0,stdout=app.redactor.redact(response.body_text),stderr='',
                http_request=app.redactor.redact_value(saved),http_response=data,body_text_truncated=response.body_text_truncated)
            result = dict(request_sha256=digest(claim),payload=payload,finished_at=app.clock())
            self.evidence.save('mcp-result',identity,result)
            phase = 'receipt_apply'
            self.mcp._apply(app.store.load_d4_operation(identity),claim,result)
        except BaseException as error:
            code = self.record_failure(identity,claim,result if phase=='receipt_apply' else None,phase,error)
            self.log.error('experiment.request_unknown' if phase=='http_execute' else 'experiment.receipt_failed',
                run=claim['task_run_id'],request=parent,exec=identity,phase=phase,
                category='unknown' if phase=='http_execute' else 'integrity' if phase=='receipt_apply' else 'persistence',
                code=code,error_type=type(error).__name__)
            if phase != 'http_execute' and isinstance(error,ExecutionRecoveryRequired):raise
            failure = ExecutionRecoveryRequired(code)
            failure.operation_context = dict(operation_id=identity, resource_ref=resource, road_id=intent_id)
            raise failure from error
        self.log.info('experiment.request_finished',run=claim['task_run_id'],request=parent,exec=identity,
            status=response.status,body_bytes=response.body_bytes)
        body = web.protected.read(response.body_evidence_id)
        return dict(status=response.status,body_b64=base64.b64encode(body).decode('ascii'),evidence_ref=identity,
            content_type=response.content_type,redirect_path_query=response.redirect_path_query,
            cross_origin_redirect_blocked=response.cross_origin_redirect_blocked)

    async def run(self, parent, intent_id, call, token):
        self.validate(call)
        app = self.app
        if self.result(parent) is not None:
            self._apply_parent(parent)
            return
        if self.evidence.read('experiment-session',parent) is not None:
            raise ExecutionRecoveryRequired('d22_experiment_replay_forbidden')
        app._check_dispatch()
        app.web.attach()
        maximum = self._limits(intent_id)
        timeout = min(call.arguments['timeout_seconds'],maximum,app.state.task_run.deadline_at-app.clock())
        if timeout <= 0:
            raise TimeoutError('d22_experiment_timeout')
        session = dict(task_run_id=app.state.task_run.task_run_id,intent_id=intent_id,call=asdict(call),
            authorization_sha256=app.web.policy()['authorization_sha256'])
        self.evidence.save('experiment-session',parent,session)
        self.live = parent
        runner = None
        finished = dict(status='failed',code='d22_experiment_environment_error',output='')
        primary_error = None
        expires = time.monotonic()+timeout
        phase = 'process_start'
        try:
            runner = await asyncio.to_thread(self.runner_factory,app.http_sandbox,session['task_run_id'],
                app.state.task_run.sandbox_id,call.arguments['script'],timeout)
            phase = 'bridge_ready'
            ready = await self._event(runner,token,expires)
            if ready.get('kind') == 'unavailable':
                finished['code'] = 'd22_experiment_isolation_unavailable'
            elif ready.get('kind') != 'ready':
                raise ValueError('d22_experiment_bridge_invalid')
            else:
                self.evidence.save('experiment-process',parent,dict(sandbox_id=app.state.task_run.sandbox_id,
                    exec_id=runner.exec_id))
                while True:
                    phase = 'bridge_event'
                    event = await self._event(runner,token,expires,intent_id)
                    await self._strategy(intent_id)
                    if event.get('kind') == 'finished':
                        if (event.get('status') not in {'completed','failed'} or not isinstance(event.get('output'),str)
                                or len(event['output'].encode('utf-8'))>MAX_SCRIPT_BYTES):
                            raise ValueError('d22_experiment_bridge_invalid')
                        finished = dict(status=event['status'],code=None if event['status']=='completed' else 'd22_experiment_script_error',
                            output=app.redactor.redact(event['output']))
                        if event['status']=='failed':
                            finished['error_type']=event.get('error_type') if event.get('error_type') in SCRIPT_ERRORS else 'OtherError'
                            line=event.get('error_line')
                            if type(line) is int and 1<=line<=call.arguments['script'].count('\n')+1:
                                finished['error_line']=line
                            finished['failure'] = dict(category='script', phase='script_execution',
                                code=finished['code'], error_type=finished['error_type'])
                        break
                    if await self._strategy(intent_id, start=True):
                        finished = dict(status='stopped',code='d22_experiment_strategy_stop',output='')
                        break
                    if event.get('kind') != 'request':
                        raise ValueError('d22_experiment_bridge_invalid')
                    try:
                        phase = 'target_request'
                        result = await self.request(parent,intent_id,call,len(self.records(parent))+1,event,token,expires)
                    except (ValueError,HttpCapabilityError) as error:
                        from agent_adapters.output_evidence import SAFE_HTTP_ERROR_CODES
                        code=getattr(error,'code',error.args[0] if len(error.args)==1 else None)
                        if code not in SAFE_HTTP_ERROR_CODES | {'d22_experiment_method_not_authorized','d22_experiment_request_invalid'}:
                            code='d22_experiment_request_rejected'
                        self.log.warning('experiment.request_rejected',run=session['task_run_id'],request=parent,
                            category='policy',code=code)
                        finished = dict(status='rejected',code=code,output='',
                            failure=dict(category='policy',phase='target_request',code=code))
                        break
                    await self._strategy(intent_id, start=True)
                    phase = 'bridge_reply'
                    await asyncio.to_thread(runner.send,result)
        except ExecutionRecoveryRequired as error:
            primary_error = error
            raise
        except StageStateError as error:
            primary_error = error
            self.log.warning('experiment.local_failure',run=session['task_run_id'],request=parent,
                category=error.category.value, phase=phase, code=error.code, error_type=type(error).__name__)
            raise
        except _StrategyStop:
            finished = dict(status='stopped',code='d22_experiment_strategy_stop',output='')
        except TaskRunSandboxError as error:
            if error.outcome == 'outcome_unknown':
                primary_error = ExecutionRecoveryRequired('d22_experiment_process_outcome_unknown')
                raise primary_error from error
            finished['code'] = 'd22_experiment_environment_error'
            finished['failure'] = dict(category='environment',phase=phase,code=finished['code'],
                error_type=type(error).__name__)
        except (TimeoutError,InterruptedError) as error:
            finished = dict(status='stopped',code='d22_experiment_timeout' if isinstance(error,TimeoutError) else 'd22_experiment_cancelled',output='')
        except Exception as error:
            code = 'd22_experiment_environment_error' if runner is None else 'd22_experiment_local_error'
            category = 'environment' if runner is None else 'bridge' if phase.startswith('bridge_') else 'technical_failure'
            finished['failure'] = dict(category=category,phase=phase,code=code,
                error_type=type(error).__name__ if type(error).__name__ in SCRIPT_ERRORS else 'OtherError')
            self.log.warning('experiment.local_failure',run=session['task_run_id'],request=parent,
                **finished['failure'])
            finished['code'] = code
        finally:
            try:
                if runner is not None:
                    try:
                        await asyncio.to_thread(runner.close)
                    except Exception as error:
                        from agent_adapters.output_evidence import safe_failure_details
                        failure = safe_failure_details(error,include_chain=True)
                        diagnostic = dict(getattr(runner,'stop_diagnostic',dict(phase='close',process_stopped=False)))
                        if isinstance(error,TaskRunSandboxError):
                            diagnostic.update(sandbox_phase=error.phase,sandbox_outcome=error.outcome)
                        retained = dict(session_sha256=digest(session),
                            requests=[r.start.operation_id for r in self.records(parent)],
                            **finished,stop_diagnostic=diagnostic,stop_failure=failure)
                        self.evidence.save('experiment-stop',parent,retained)
                        self.log.error('experiment.stop_unknown',run=session['task_run_id'],intent=intent_id,
                            request=parent,category='unknown',code='d22_experiment_process_outcome_unknown',
                            stop_diagnostic=diagnostic,**failure)
                        if primary_error is None:
                            raise ExecutionRecoveryRequired('d22_experiment_process_outcome_unknown') from error
                    else:
                        diagnostic = getattr(runner,'stop_diagnostic',None)
                        if diagnostic is not None:
                            finished['stop_diagnostic'] = diagnostic
            finally:
                self.live = None
        result = dict(session_sha256=digest(session),requests=[r.start.operation_id for r in self.records(parent)],**finished)
        self.evidence.save('experiment-result',parent,result)
        self._apply_parent(parent)
        self.log.info('experiment.finished',run=session['task_run_id'],intent=intent_id,request=parent,
            status=result['status'],code=result['code'],request_count=len(result['requests']))
