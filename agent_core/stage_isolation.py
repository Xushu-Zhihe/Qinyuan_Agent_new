"""Trusted, conservative scope for interrupted native HTTP experiments."""

from agent_core.turn_modeling import digest


def isolated_http_operations(store, task_id):
    evidence = getattr(store, 'stage_evidence', None)
    if evidence is None:
        return ()
    policy = evidence.read('policy', task_id)
    if not policy or policy.get('tool_execution', {}).get('unknown_isolation') != 'http_stopped_v1':
        return ()
    isolated = []
    for row in store.list_d4_unresolved_operations(task_id):
        parts = row.start.resource_ref.split('/')
        if row.start.category != 'tool_dispatch' or len(parts) != 5 or parts[0] != 'mcp' or parts[2] != 'experiment':
            continue
        identity, parent = row.start.operation_id, parts[3]
        claim = evidence.read('mcp-call', identity)
        session = evidence.read('experiment-session', parent)
        stopped = evidence.read('experiment-stop', parent)
        failure = evidence.read('experiment-failure', identity)
        if not all((claim, session, stopped, failure)):
            continue
        # Both local workers must have stopped. The remote effect remains unknown.
        diagnostic = failure.get('http_diagnostic', {})
        recovered = evidence.read('http-worker-stop', identity)
        if recovered is not None:
            if (recovered.get('failure_sha256') == digest(failure)
                    and recovered.get('request_sha256') == digest(claim)
                    and recovered.get('task_run_id') == task_id
                    and recovered.get('sandbox_id') == store.load_control_state_rows(task_id).task_run.sandbox_id
                    and recovered.get('exec_id') == diagnostic.get('exec_id')
                    and recovered.get('running') is False):
                diagnostic = {**diagnostic, 'phase': 'exec_inspect', 'running': False}
        if (claim.get('task_run_id') == task_id
                and claim.get('intent_id') == parts[1]
                and claim.get('model_request_id') == parent
                and claim.get('experiment_request')
                and row.start.context_sha256 == digest(claim)
                and session.get('task_run_id') == task_id
                and session.get('intent_id') == parts[1]
                and session.get('call') == claim.get('call')
                and stopped.get('session_sha256') == digest(session)
                and stopped.get('requests') == [r.start.operation_id for r in store.list_d4_operations(task_id)
                    if r.start.resource_ref.startswith('mcp/' + parts[1] + '/experiment/' + parent + '/')]
                and stopped.get('stop_diagnostic', {}).get('process_stopped') is True
                and failure.get('request_sha256') == digest(claim)
                and failure.get('model_request_id') == parent
                and failure.get('phase') == 'http_execute'
                and failure.get('code') == 'd22_experiment_request_outcome_unknown'
                and diagnostic.get('phase') == 'exec_inspect'
                and diagnostic.get('running') is False
                and evidence.read('mcp-result', identity) is None):
            isolated.append(row)
    return tuple(isolated)


def tool_allowed(store, task_id, call, *, isolated=None):
    """Remote access stays closed; the existing networkless sandbox is independent."""
    if not (isolated_http_operations(store, task_id) if isolated is None else isolated):
        return True
    from agent_core.actions import PUBLIC_REFERENCE_TOOLS
    if call.tool_name in PUBLIC_REFERENCE_TOOLS:
        return True
    policy = store.stage_evidence.read('policy', task_id)
    if policy.get('tcp_authorization') is not None:
        return False
    # The executor freezes these namespaces from its enforced sandbox registry.
    registry = store.stage_evidence.read('mcp-registry', task_id)
    return bool(registry and call.tool_name.split('.')[0] in registry.get('sandbox_servers', ()))
