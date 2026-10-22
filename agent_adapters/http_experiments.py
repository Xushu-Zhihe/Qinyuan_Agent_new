"""Container-local conditional experiments over the existing trusted HTTP port."""
import asyncio
import base64
import hashlib
import json
import time
from dataclasses import asdict

from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.operation_accounting import D4OperationStart
from agent_core.turn_modeling import digest
from agent_adapters.web.http_adapter import D7HttpRequest, D7HttpError, D7HttpPolicy
from Sandbox.http_experiment import HttpExperimentProcess, MAX_SCRIPT_BYTES
from Sandbox.task_run_sandbox import TaskRunSandboxError
from qinyuan_core import get_logger

TOOL = 'http.run_experiment'
SCRIPT_ERRORS = frozenset({'SyntaxError','NameError','TypeError','ValueError','KeyError','IndexError',
    'AttributeError','ZeroDivisionError','AssertionError','ImportError','ModuleNotFoundError','PermissionError',
    'RuntimeError','TimeoutError','MemoryError','RecursionError','OSError'})
SPEC = dict(qualified_name=TOOL, tags=['http','conditional-experiment'],
    description='Run Python branches/loops in this container. request(method,path_query,headers={},body="") returns status, body bytes, text and evidence_ref. HTTP uses the authorized origin, persistent cookies and per-request evidence; direct networking/process creation is disabled. Print a concise unreviewed summary.',
    input_schema={'type':'object','properties':{'script':{'type':'string','minLength':1,'maxLength':MAX_SCRIPT_BYTES},
        'timeout_seconds':{'type':'integer','minimum':5,'maximum':300}},
        'required':['script','timeout_seconds'],'additionalProperties':False})


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
            raise D3RecoveryRequired('d22_experiment_session_changed')
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
            raise D3RecoveryRequired('d22_experiment_source_changed')
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
                or parent.completion.evidence_sha256 != digest(asdict(native))
                or native.raw != {'kind':'mcp_call',**session['call']}):
            raise D3RecoveryRequired('d22_experiment_source_changed')
        raw = web.protected.read(source['evidence_id'])
        if hashlib.sha256(raw).hexdigest() != source['sha256']:
            raise D3RecoveryRequired('d22_experiment_source_changed')
        fields = json.loads(raw)
        fields['headers'] = tuple(tuple(h) for h in fields['headers'])
        request = D7HttpRequest(operation.start.operation_id, **fields)
        method, _, _, _ = D7HttpPolicy.for_origin(web.policy()['endpoint']).validate(request)
        if method not in web.policy()['allowed_methods']:
            raise D3RecoveryRequired('d22_experiment_source_changed')
        response = result['payload']['http_response']
        body = web.protected.read(response['body_evidence_id'])
        if (hashlib.sha256(body).hexdigest() != response['body_sha256'] or len(body) != response['body_bytes']
                or result['payload']['status'] != 'completed'
                or operation.start.resource_ref != 'mcp/'+claim['intent_id']+'/experiment/'+claim['model_request_id']+'/'+str(source['ordinal'])):
            raise D3RecoveryRequired('d22_experiment_source_changed')
        web.cookie_snapshot(response)

    def projection(self, operation, claim, result):
        self.validate_receipt(operation,claim,result)
        return dict(call=dict(tool_name=TOOL,arguments=result['payload']['http_request']),result=result['payload'])

    def records(self, parent):
        return [r for r in self.mcp.records() if '/experiment/'+parent+'/' in r.start.resource_ref]

    def reconcile(self):
        # A lost script may have already changed the target. Never replay it.
        for op in self.app.store.list_d3_operations(self.app.state.task_run.task_run_id):
            identity = op.start.operation_id
            session = self.evidence.read('experiment-session',identity)
            if session is None or identity == self.live:
                continue
            rows = self.records(identity)
            if any(r.completion is None for r in rows):
                raise D3RecoveryRequired('d22_experiment_request_outcome_unknown')
            result = self.result(identity)
            if result is None:
                process = self.evidence.read('experiment-process',identity)
                if process is None or process['sandbox_id'] != self.app.state.task_run.sandbox_id:
                    raise D3RecoveryRequired('d22_experiment_process_reconciliation_required')
                # Read-only reconciliation: never restart/kill/replay a lost program.
                try:
                    status = self.app.http_sandbox._runtime.client.api.exec_inspect(process['exec_id'])
                except Exception as error:
                    raise D3RecoveryRequired('d22_experiment_process_reconciliation_required') from error
                if status.get('ContainerID') != process['sandbox_id'] or status.get('Running') is not False:
                    raise D3RecoveryRequired('d22_experiment_process_reconciliation_required')
                self.evidence.save('experiment-result',identity,dict(session_sha256=digest(session),
                    requests=[r.start.operation_id for r in rows],status='interrupted',
                    code='d22_experiment_process_lost',output=''))
            self.app.store.mark_d3_operation_applied(identity,identity)

    async def _event(self, runner, token, expires, intent_id=None):
        pending = asyncio.create_task(asyncio.to_thread(runner.receive))
        try:
            while not pending.done():
                await asyncio.wait({pending},timeout=0.025)
                self.app.supervision.poll()
                if intent_id is not None and self.app.supervision.stop(intent_id):
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
        app._check_dispatch()
        if set(raw) != {'kind','method','path_query','headers','body'} or not isinstance(raw['headers'],dict):
            raise ValueError('d22_experiment_request_invalid')
        _, maximum = app.validator.action_limits(app.state.task_run.task_run_id,
            next(i.turn_id for i in app.state.intents if i.intent_id==intent_id),intent_id)
        app.validator._allowed(app.state.task_run.task_run_id,
            next(i.turn_id for i in app.state.intents if i.intent_id==intent_id),intent_id,'probe',maximum)
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
            raise D3RecoveryRequired('d22_experiment_request_replay_forbidden')
        previous = web.restore_cookies()
        self.evidence.save('mcp-call',identity,claim)
        timeout = min(maximum,app.config.http_timeout_seconds,app.state.task_run.deadline_at-app.clock(),expires-time.monotonic())
        if timeout <= 0:
            raise TimeoutError('d22_experiment_timeout')
        app.store.begin_d4_operation(D4OperationStart(identity,claim['task_run_id'],'tool_dispatch',app.clock(),
            app.state.latest_checkpoint.sequence,digest(claim),'mcp/'+intent_id+'/experiment/'+parent+'/'+str(ordinal),
            app.config.d4_budget.budget_policy_ref))
        self.log.info('experiment.request_started',run=claim['task_run_id'],intent=intent_id,request=parent,exec=identity)
        web.http.timeout_seconds = timeout
        pending = asyncio.create_task(asyncio.to_thread(web.http.execute,proposed))
        try:
            while not pending.done():
                await asyncio.wait({pending},timeout=0.025)
                app.supervision.poll()
                if token.is_cancelled or app.clock() >= app.state.task_run.deadline_at or time.monotonic() >= expires:
                    raise D3RecoveryRequired('d22_experiment_request_outcome_unknown')
            response = pending.result()
            data = dict(execution_id=identity,method=method,status=response.status,
                body_evidence_id=response.body_evidence_id,body_sha256=response.body_sha256,body_bytes=response.body_bytes)
            data.update(web.save_cookies(identity,previous,response))
            payload = dict(status='completed',tool=TOOL,exit_code=0,stdout=app.redactor.redact(response.body_text),stderr='',
                http_request=app.redactor.redact_value(saved),http_response=data,body_text_truncated=response.body_text_truncated)
            result = dict(request_sha256=digest(claim),payload=payload,finished_at=app.clock())
            self.evidence.save('mcp-result',identity,result)
            self.mcp._apply(app.store.load_d4_operation(identity),claim,result)
        except BaseException as error:
            self.log.error('experiment.request_unknown',run=claim['task_run_id'],request=parent,exec=identity,
                category='unknown',code='d22_experiment_request_outcome_unknown',error_type=type(error).__name__)
            raise D3RecoveryRequired('d22_experiment_request_outcome_unknown') from error
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
            app.store.mark_d3_operation_applied(parent,parent)
            return
        if self.evidence.read('experiment-session',parent) is not None:
            raise D3RecoveryRequired('d22_experiment_replay_forbidden')
        app._check_dispatch()
        app.web.attach()
        intent = next(i for i in app.state.intents if i.intent_id == intent_id)
        _, maximum = app.validator.action_limits(app.state.task_run.task_run_id,intent.turn_id,intent_id)
        app.validator._allowed(app.state.task_run.task_run_id,intent.turn_id,intent_id,'probe',maximum)
        timeout = min(call.arguments['timeout_seconds'],maximum,app.state.task_run.deadline_at-app.clock())
        if timeout <= 0:
            raise TimeoutError('d22_experiment_timeout')
        session = dict(task_run_id=app.state.task_run.task_run_id,intent_id=intent_id,call=asdict(call),
            authorization_sha256=app.web.policy()['authorization_sha256'])
        self.evidence.save('experiment-session',parent,session)
        self.live = parent
        runner = None
        finished = dict(status='failed',code='d22_experiment_environment_error',output='')
        expires = time.monotonic()+timeout
        try:
            runner = await asyncio.to_thread(self.runner_factory,app.http_sandbox,session['task_run_id'],
                app.state.task_run.sandbox_id,call.arguments['script'],timeout)
            ready = await self._event(runner,token,expires)
            if ready.get('kind') == 'unavailable':
                finished['code'] = 'd22_experiment_isolation_unavailable'
            elif ready.get('kind') != 'ready':
                raise ValueError('d22_experiment_bridge_invalid')
            else:
                self.evidence.save('experiment-process',parent,dict(sandbox_id=app.state.task_run.sandbox_id,
                    exec_id=runner.exec_id))
                while True:
                    event = await self._event(runner,token,expires,intent_id)
                    app.supervision.poll()
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
                        break
                    if app.sessions.due(intent_id):
                        app.supervision.start(intent_id)
                    if app.supervision.stop(intent_id):
                        finished = dict(status='stopped',code='d22_experiment_strategy_stop',output='')
                        break
                    if event.get('kind') != 'request':
                        raise ValueError('d22_experiment_bridge_invalid')
                    try:
                        result = await self.request(parent,intent_id,call,len(self.records(parent))+1,event,token,expires)
                    except (ValueError,D7HttpError) as error:
                        from agent_adapters.output_evidence import SAFE_HTTP_ERROR_CODES
                        code=getattr(error,'code',error.args[0] if len(error.args)==1 else None)
                        if code not in SAFE_HTTP_ERROR_CODES | {'d22_experiment_method_not_authorized','d22_experiment_request_invalid'}:
                            code='d22_experiment_request_rejected'
                        self.log.warning('experiment.request_rejected',run=session['task_run_id'],request=parent,
                            category='policy',code=code)
                        finished = dict(status='rejected',code=code,output='')
                        break
                    if app.sessions.due(intent_id):
                        app.supervision.start(intent_id)
                    await asyncio.to_thread(runner.send,result)
        except D3RecoveryRequired:
            raise
        except _StrategyStop:
            finished = dict(status='stopped',code='d22_experiment_strategy_stop',output='')
        except TaskRunSandboxError as error:
            if error.outcome == 'outcome_unknown':
                raise D3RecoveryRequired('d22_experiment_process_outcome_unknown') from error
            finished['code'] = 'd22_experiment_environment_error'
        except (TimeoutError,InterruptedError) as error:
            finished = dict(status='stopped',code='d22_experiment_timeout' if isinstance(error,TimeoutError) else 'd22_experiment_cancelled',output='')
        except Exception as error:
            self.log.warning('experiment.local_failure',run=session['task_run_id'],request=parent,
                category='environment' if runner is None else 'technical_failure',error_type=type(error).__name__)
            finished['code'] = 'd22_experiment_environment_error' if runner is None else 'd22_experiment_local_error'
        finally:
            try:
                if runner is not None:
                    await asyncio.to_thread(runner.close)
            finally:
                self.live = None
        result = dict(session_sha256=digest(session),requests=[r.start.operation_id for r in self.records(parent)],**finished)
        self.evidence.save('experiment-result',parent,result)
        app.store.mark_d3_operation_applied(parent,parent)
        self.log.info('experiment.finished',run=session['task_run_id'],intent=intent_id,request=parent,
            status=result['status'],code=result['code'],request_count=len(result['requests']))
