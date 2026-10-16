"""Keep public synchronous v3 export live without weakening stalled-write bounds."""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters import cli, task_cli
from agent_adapters.d8_simulation import SimulationModel
from agent_adapters.event_export import EventExporter, read_events
from agent_adapters.task_cli import _finish_event_export
from test_d9_public import request


def slow_writer(pipe, path, database, task_id, finish):
    pipe.send({'ready': True, 'sequence': 0})
    pipe.send({'writing_at': time.monotonic()})
    time.sleep(5)
    Path(path).write_text('late write')


def test_continuous_monitor_still_terminates_a_stalled_writer(tmp_path):
    path = tmp_path / 'events.jsonl'
    exporter = EventExporter(path, tmp_path / 'empty.sqlite', str(uuid4()), worker=slow_writer)
    exporter.start_monitoring()
    try:
        end = time.monotonic() + 4
        while not exporter.closed and time.monotonic() < end:
            time.sleep(0.01)
        assert exporter.error == 'event_export_timeout' and exporter.closed
        assert not path.exists()
    finally:
        exporter.close()
    assert not exporter._monitor.is_alive()


def test_public_v3_exports_during_long_model_call_and_validates_cancel_append(tmp_path, monkeypatch):
    raw = request(tmp_path)
    raw['execution_profile'] = 'd8_v8'
    monkeypatch.setattr(task_cli, '_input', lambda _: raw)
    original = SimulationModel.invoke
    slept = []
    def invoke(model, env, *args):
        if env.output_contract['stage'] == 'inner_plan' and not slept:
            slept.append(True)
            time.sleep(3)
        return original(model, env, *args)
    monkeypatch.setattr(SimulationModel, 'invoke', invoke)
    events = tmp_path / 'events.jsonl'
    args = SimpleNamespace(request='unused', mode='simulated', state_dir=tmp_path / 'state', events=str(events))
    result = task_cli.dispatch_run_task(args)
    assert result['errors'] == [] and result['event_export']['error'] is None, result
    assert result['event_export']['last_sequence'] == result['checkpoint']['watermark_event_sequence']
    directory = args.state_dir / result['task_run_id']
    before = events.read_bytes()
    assert [json.loads(line) for line in before.splitlines()] == read_events(directory / 'control.sqlite', result['task_run_id'])
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('cancel called model'))
    args.command, args.task_run_id = 'cancel', result['task_run_id']
    cancelled = cli.dispatch(args)
    assert cancelled['task_run']['status'] == 'cancelled'
    assert cancelled['event_export']['error'] is None
    assert events.read_bytes().startswith(before)
    assert cancelled['event_export']['last_sequence'] == cancelled['checkpoint']['watermark_event_sequence']


def test_invalid_existing_event_prefix_is_rejected(tmp_path):
    path = tmp_path / 'events.jsonl'
    path.write_bytes(b'{"sequence":99}\n')
    exporter = EventExporter(path, tmp_path / 'absent.sqlite', str(uuid4()), append_existing=True)
    exporter.start_monitoring()
    try:
        _, error = _finish_event_export(exporter)
        assert error == 'event_export_failed'
        assert path.read_bytes() == b'{"sequence":99}\n'
    finally:
        exporter.close()
