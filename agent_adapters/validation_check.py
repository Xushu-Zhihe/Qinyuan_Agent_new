"""Explicit, body-free D9 check selection; no user-supplied verdict or endpoint."""

from agent_core.goal_feedback import ExternalValidationCheckRequest
from pathlib import Path

FIELDS = frozenset({'candidate_id', 'evidence_version', 'submission_operation_id', 'query_id'})


def validate_selection(task_run_id, selection):
    if not isinstance(selection, dict) or set(selection) != FIELDS:
        raise ValueError('d9_validation_check_fields_invalid')
    ExternalValidationCheckRequest(task_run_id=task_run_id, checkpoint_sequence=1, **selection)
    return dict(selection)


def configured_checker(declaration):
    """Production has no authoritative task adapter yet. Tests inject only here."""
    return None


def dispatch_check(directory, declaration, selection, *, events=None):
    from agent_adapters.web.task_result import snapshot_v3_task_result
    if (declaration.get('contract_version'), declaration.get('execution_profile')) not in {(19, 'd8_v19'), (20, 'd8_v20'), (21, 'd8_v21'), (22, 'd8_v22')}:
        raise ValueError('d9_validation_check_profile_unsupported')
    selection = validate_selection(declaration['task_run_id'], selection)
    from agent_core.answer_candidates import check_request
    from agent_core.sqlite_control_store import SqliteControlStore
    from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
    from agent_core.goal_feedback import ExternalValidationFeedbackConflict
    try:
        with SqliteControlStore.open_readonly(directory / 'control.sqlite') as store:
            state = store.load_control_state_rows(declaration['task_run_id'])
            if state is None:
                raise ValueError('d9_validation_state_unavailable')
            check_request(store, declaration['task_run_id'], selection, state.latest_checkpoint.sequence + 1)
            previous = store.load_d7_external_validation_check(selection['query_id'])
    except (ExecutionRecoveryRequired, ExternalValidationFeedbackConflict) as exc:
        raise ValueError('d9_validation_submission_binding_invalid') from exc
    if previous is not None and previous.result is not None:
        result = snapshot_v3_task_result(directory, declaration)
        result['validation_check'] = {'state': previous.result.verdict, 'query_id': selection['query_id']}
        result['exit_code'] = 0
        if events:
            from agent_adapters.event_export import EventExporter
            from agent_adapters.task_cli import _finish_event_export
            exporter = EventExporter(Path(events), directory / 'control.sqlite', declaration['task_run_id'], append_existing=True)
            exporter.start_monitoring()
            sequence, error = _finish_event_export(exporter)
            result['event_export'] = {'last_sequence': sequence, 'error': error}
            if error:
                result.setdefault('errors', []).append({'code': error})
                result['exit_code'] = 5 if result.get('recovery', {}).get('required') else 3
        return result
    import time
    if (directory / 'cancel-request.json').exists():
        raise ValueError('d9_validation_check_cancelled')
    if time.time() >= state.task_run.deadline_at:
        raise ValueError('d9_validation_deadline_reached')
    checker = configured_checker(declaration)
    if checker is None:
        result = snapshot_v3_task_result(directory, declaration)
        result.setdefault('errors', []).append({'code': 'd9_validation_adapter_unconfigured'})
        result['validation_check'] = {'state': 'adapter_unconfigured', 'query_id': selection['query_id']}
        result['exit_code'] = 2
        return result
    from agent_adapters.simulation import run_simulated_task
    from agent_adapters.event_export import EventExporter
    from agent_adapters.task_cli import _finish_event_export
    exporter = EventExporter(Path(events), directory / 'control.sqlite', declaration['task_run_id'], append_existing=True) if events else None
    if exporter is not None:
        exporter.start_monitoring()
    try:
        try:
            result = run_simulated_task(directory, declaration, operation='validation-check',
                                        validation_selection=selection, validation_checker=checker)
        except Exception:
            result = snapshot_v3_task_result(directory, declaration)
            result.setdefault('errors', []).append({'code': 'd9_validation_check_recovery_required'})
            result['recovery'] = {'required': True, 'reason': 'external_validation_check_unknown'}
            result['exit_code'] = 5
    finally:
        if exporter is not None:
            sequence, error = _finish_event_export(exporter)
    if exporter is not None:
        result['event_export'] = {'last_sequence': sequence, 'error': error}
        if error:
            result.setdefault('errors', []).append({'code': error})
            result['exit_code'] = 5 if result.get('recovery', {}).get('required') else 3
    return result
