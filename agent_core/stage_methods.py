"""Same-TaskRun method views derived from accepted facts and original tool receipts."""
from copy import deepcopy

from agent_core.stage_contracts import StageFactBatch, StageMethodFact, StageMethodRegistration, StageMethodCall, StageMethodMaterialize, fact_review_parts, reviewed_fact
from agent_core.stage_storage import _fail
from agent_core.turn_modeling import digest
from qinyuan_core import Category


def method_schema(request, schema):
    from agent_core.stage_planning import stage_runtime, obj, text, array
    runtime = stage_runtime(request)
    if not runtime.get('methods_enabled'):
        return schema
    schema = deepcopy(schema)
    sources = [i.ref for i in request.context.history
               if i.identity == 'tool_observation_not_fact' and i.source_status == 'available']
    methods = runtime.get('methods', ())
    declaration = obj({'source_ref': {'type': 'string', 'enum': sources},
        'dependencies': {**array({'type': 'string', 'enum': [m['ref'] for m in methods]}, maximum=8),
                         'uniqueItems': True}, 'conditions': text()})
    def extend(node):
        if not isinstance(node, dict):
            return
        props = node.get('properties', {})
        if props.get('kind', {}).get('const') == 'mcp_call':
            props['pin_files'] = {**array(text(4096), minimum=1, maximum=8), 'uniqueItems': True}
        if props.get('kind', {}).get('const') == 'fact_candidate' and sources:
            props['method'] = declaration
        for key, value in node.items():
            if key == 'properties':
                for field, child in value.items():
                    if field != 'method':
                        extend(child)
            elif isinstance(value, dict):
                extend(value)
            elif isinstance(value, list):
                for child in value:
                    extend(child)
    extend(schema)
    if request.stage in {'stage_action', 'stage_roads', 'stage_continuation'} and sources and schema.get('anyOf'):
        facts = [i.ref for i in request.context.history if i.record_type == 'verified_fact' and i.source_status == 'available']
        if facts:
            schema['anyOf'].append(obj({'kind': {'type': 'string', 'const': 'register_method'},
                'fact_ref': {'type': 'string', 'enum': facts}, 'method': declaration, 'reason': text()}))
    if request.stage == 'stage_review':
        claims = request.proposal.candidates if isinstance(request.proposal, StageFactBatch) else (request.proposal,)
        if any(isinstance(c, StageMethodFact) for c in claims):
            node = schema['properties']['decisions']['items'] if isinstance(request.proposal, StageFactBatch) else schema
            node['properties']['method_decision'] = {'type': 'string', 'enum': ['accept', 'unavailable']}
            # Missing verdict means unavailable; Fact review remains independently usable.
    if (request.stage == 'stage_action' and methods and any(
            c.get('properties', {}).get('kind', {}).get('const') == 'mcp_call'
            for c in schema.get('anyOf', ()))):
        names = {tool['qualified_name'] for tool in request.tools}
        eligible = [m['ref'] for m in methods if m['call']['tool_name'] in names]
        if 'shell.run_bash' in names:
            schema['anyOf'].append(obj({'kind': {'type': 'string', 'const': 'materialize_method'},
                'method_ref': {'type': 'string', 'enum': [m['ref'] for m in methods]}}))
        if eligible:
            schema['anyOf'].append(obj({'kind': {'type': 'string', 'const': 'reuse_method'},
                'method_ref': {'type': 'string', 'enum': eligible}}))
    return schema


def resolve_method_call(request, ref):
    from agent_core.stage_planning import stage_runtime
    method = next((m for m in stage_runtime(request).get('methods', ()) if m['ref'] == ref), None)
    if method is None or method['task_run_id'] != request.task_run_id:
        _fail('stage.method_unavailable', Category.POLICY)
    call = method['call']
    from agent_core.output_contracts import shape_issues
    tool = next((t for t in request.tools if t['qualified_name'] == call['tool_name']), None)
    if tool is None:
        _fail('stage.method_tool_unavailable', Category.POLICY)
    # The original call still has to satisfy the current tool schema.
    if shape_issues(call['arguments'], tool['input_schema']):
        _fail('stage.method_arguments_changed', Category.INPUT)
    return StageMethodCall(call['tool_name'], call['arguments'], tuple(call.get('pin_files', ())),
                           tuple(tuple(p) for p in call.get('expected_files', ())), ref)


class StageMethods:
    def __init__(self, evidence, task_id):
        self.evidence, self.task_id = evidence, task_id

    def catalog(self, *, history=None):
        evidence = self.evidence
        history = evidence.history(self.task_id) if history is None else history
        from agent_core.conclusion_revisions import ConclusionRevisions
        available = {i.ref: i for i in history if ConclusionRevisions.reliable(i)}
        specifications = {}
        for fact in available.values():
            if fact.record_type != 'verified_fact':
                continue
            row = evidence.store._connection.execute(
                'SELECT f.verification_ref FROM facts f JOIN stage_turns t ON t.turn_id=f.turn_id '
                'WHERE f.fact_id=? AND t.task_run_id=?', (fact.record_id, self.task_id)).fetchone()
            if row is None or not (row[0] or '').startswith('stage:'):
                continue
            review_id, index = fact_review_parts(row[0])
            operation = evidence.store.load_d4_operation(review_id)
            if operation is None or operation.start.task_run_id != self.task_id or operation.application_ref is None:
                _fail('stage.method_review_binding_changed', Category.UNKNOWN)
            request = evidence.request(review_id)
            candidate, review = reviewed_fact(request.proposal, evidence.read_model_result(operation).result, index)
            if (isinstance(candidate, StageMethodFact) and review.decision == 'accept'
                    and candidate.content == fact.content
                    and getattr(review, 'method_decision', 'unavailable') == 'accept'
                    and candidate.method['source_ref'] in candidate.support_refs):
                specifications['method:' + fact.ref] = (fact, review_id, candidate.method)
        for operation in evidence.store.list_d4_operations(self.task_id):
            if operation.start.role != 'outer_planning' or operation.completion is None or operation.application_ref is None:
                continue
            request = evidence.request(operation.start.operation_id)
            if not isinstance(request.proposal, StageMethodRegistration):
                continue
            verdict = evidence.read_model_result(operation).result
            fact = available.get(request.proposal.fact_ref)
            if verdict.decision == 'accept' and fact is not None and fact.record_type == 'verified_fact':
                ref = 'method:' + fact.ref + ':' + request.proposal_operation_id
                specifications[ref] = (fact, operation.start.operation_id, request.proposal.method)
        entries, visiting = {}, set()
        def build(ref):
            if ref in entries:
                return entries[ref]
            if ref in visiting:
                _fail('stage.method_dependency_cycle', Category.UNKNOWN)
            specification = specifications.get(ref)
            if specification is None:
                return None
            fact, review_id, spec = specification
            visiting.add(ref)
            source = available.get(spec['source_ref'])
            calls = evidence.history_tool_requests((source,)) if source is not None else ()
            if not calls:
                visiting.remove(ref)
                return None
            receipt = evidence.read('mcp-result', source.record_id)['payload']
            if receipt.get('status') != 'completed' or receipt.get('exit_code', 0) != 0 or receipt.get('timed_out'):
                visiting.remove(ref)
                return None
            call = calls[0]['call']
            captured = evidence.read('mcp-method-inputs', source.record_id)
            expected_capture = receipt.get('method_inputs_sha256')
            if expected_capture and (captured is None or digest(captured) != expected_capture):
                _fail('stage.method_input_binding_changed', Category.UNKNOWN)
            if captured is not None and not expected_capture:
                visiting.remove(ref)
                return None
            if captured is not None:
                claim = evidence.read('mcp-call', source.record_id)
                if captured['request_sha256'] != digest(claim):
                    _fail('stage.method_input_binding_changed', Category.UNKNOWN)
                call = captured['resolved_call']

            # Mutable path arguments require execution-bound snapshots before reuse.
            args = call['arguments']
            pinned = {p[0] for p in call.get('expected_files', ())}
            if any((k.endswith('_path') or k in {'path', 'cwd'}) and v not in pinned for k, v in args.items()):
                visiting.remove(ref)
                return None
            dependencies = []
            for dependency in spec['dependencies']:
                if build(dependency) is None:
                    visiting.remove(ref)
                    return None
                dependencies.append(dependency)
            entry = {'ref': ref, 'task_run_id': self.task_id, 'fact_ref': fact.ref,
                'source_ref': source.ref, 'review_operation_id': review_id,
                'call_sha256': digest(call), 'call': call, 'dependencies': dependencies,
                'conditions': spec['conditions'], 'turn_id': fact.turn_id, 'road_id': fact.intent_id,
                **({'files': captured['files']} if captured else {})}
            entries[ref] = entry
            visiting.remove(ref)
            return entry
        for ref in specifications:
            build(ref)
        return tuple(entries.values())


def method_artifacts(methods, ref):
    """Return a finite, dependency-ordered manifest without executing dependencies."""
    import base64
    import hashlib
    from pathlib import PurePosixPath
    by_ref = {m['ref']: m for m in methods}
    files, visited, visiting = {}, set(), set()
    def visit(key):
        if key in visited:
            return
        if key in visiting:
            _fail('stage.method_dependency_cycle', Category.UNKNOWN)
        method = by_ref.get(key)
        if method is None:
            _fail('stage.method_dependency_unavailable', Category.POLICY)
        visiting.add(key)
        for dependency in method['dependencies']:
            visit(dependency)
        for item in method.get('files', ()):
            path = PurePosixPath(item['path'])
            if (not str(path).startswith(('/workspace/input/', '/workspace/work/'))
                    or '..' in path.parts or '\\' in str(path)):
                _fail('stage.method_artifact_path_invalid', Category.UNKNOWN)
            data = base64.b64decode(item['base64'], validate=True)
            if hashlib.sha256(data).hexdigest() != item['sha256']:
                _fail('stage.method_artifact_content_changed', Category.UNKNOWN)
            file_key = (key, item['path'])
            old = files.get(file_key)
            if old is not None and old['sha256'] != item['sha256']:
                _fail('stage.method_artifact_version_conflict', Category.POLICY)
            files[file_key] = {**item, 'method_ref': key}
        # Inline entry scripts are also materialized for local editing.
        call = method['call']
        script = call['arguments'].get('script')
        if isinstance(script, str):
            raw = script.encode('utf-8')
            sha = hashlib.sha256(raw).hexdigest()
            suffix = { 'shell.run_bash': '.sh', 'shell.run_powershell': '.ps1' }.get(call['tool_name'], '.py')
            path = '/workspace/work/entries/' + sha + suffix
            files[key, path] = {'path': path, 'sha256': sha, 'base64': base64.b64encode(raw).decode('ascii'), 'method_ref': key}
        else:
            import json
            raw = json.dumps(call, ensure_ascii=False, sort_keys=True).encode('utf-8')
            sha = hashlib.sha256(raw).hexdigest()
            path = '/workspace/work/entries/' + sha + '.json'
            files[key, path] = {'path': path, 'sha256': sha, 'base64': base64.b64encode(raw).decode('ascii'), 'method_ref': key}
        visiting.remove(key)
        visited.add(key)
    visit(ref)
    manifest = list(files.values())
    root = '/workspace/work/.methods/' + digest(manifest)
    return {'root': root, 'files': [{**f, 'ref': 'artifact:' + digest({'path': f['path'], 'sha256': f['sha256']}),
                                  'materialized_path': root + '/' + digest(f['method_ref']) + f['path']} for f in manifest]}


MATERIALIZE_WORKER = '''import base64,hashlib,json,os,stat,sys,zlib
manifest=json.loads(zlib.decompress(base64.b64decode(sys.argv[1])))
root=manifest['root']
def directory(path):
    parts=path.split('/')[1:]
    if not path.startswith('/') or any(p in ('','.','..') for p in parts):raise ValueError('method.materialization_scope')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in parts:
            try:os.mkdir(part,0o700,dir_fd=fd)
            except FileExistsError:pass
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        return fd
    except BaseException:
        os.close(fd);raise
for item in manifest['files']:
    path=item['materialized_path']
    if not path.startswith(root+'/'):raise ValueError('method.materialization_scope')
    parent,name=path.rsplit('/',1)
    if name in ('','.','..'):raise ValueError('method.materialization_scope')
    data=base64.b64decode(item['base64'],validate=True)
    if hashlib.sha256(data).hexdigest()!=item['sha256']:raise ValueError('method.materialization_hash')
    folder=directory(parent)
    try:
        try:fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400,dir_fd=folder)
        except FileExistsError:
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=folder)
            with os.fdopen(fd,'rb') as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode) or handle.read(len(data)+1)!=data:raise ValueError('method.materialization_conflict')
        else:
            with os.fdopen(fd,'wb') as handle:
                handle.write(data);handle.flush();os.fsync(handle.fileno())
    finally:os.close(folder)
print(json.dumps({'root':root,'files':[{k:v for k,v in f.items() if k!='base64'} for f in manifest['files']]}))
'''



def resolve_materialization(request, ref):
    import base64
    import json
    import shlex
    import zlib
    from agent_core.stage_planning import stage_runtime
    methods = stage_runtime(request).get('methods', ())
    if not any(m['ref'] == ref and m['task_run_id'] == request.task_run_id for m in methods):
        _fail('stage.method_unavailable', Category.POLICY)
    if not any(t['qualified_name'] == 'shell.run_bash' for t in request.tools):
        _fail('stage.method_tool_unavailable', Category.POLICY)
    manifest = method_artifacts(methods, ref)
    payload = base64.b64encode(zlib.compress(json.dumps(manifest).encode('utf-8'))).decode('ascii')
    script = 'python3 -I -c ' + shlex.quote(MATERIALIZE_WORKER) + ' ' + shlex.quote(payload)
    if len(script.encode('utf-8')) > 65536:
        _fail('stage.method_materialization_capacity', Category.CAPACITY)
    return StageMethodMaterialize('shell.run_bash', {'script': script}, ref)


def relevant_methods(methods, history):
    """Keep visible Fact methods and their complete explicit dependency closure."""
    visible = {i.ref for i in history}
    by_ref = {m['ref']: m for m in methods}
    selected = set()
    def include(ref):
        if ref in selected:
            return
        selected.add(ref)
        for dependency in by_ref[ref]['dependencies']:
            include(dependency)
    for method in methods:
        if method['fact_ref'] in visible:
            include(method['ref'])
    return [m for m in methods if m['ref'] in selected]
