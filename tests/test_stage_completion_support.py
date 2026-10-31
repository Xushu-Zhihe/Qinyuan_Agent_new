"""Completion support must be disclosed, not inferred from an opaque rejection."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from agent_adapters.stage_application import catalog_with_completion_support
from agent_core.stage_planning import completion_support_hint, frozen_history


def history_item(ref, record_type="verified_fact", *, source_status="available", conflict=False):
    return SimpleNamespace(
        ref=ref, record_type=record_type, source_status=source_status,
        conflict=conflict, current_status="effective",
    )


def request(**overrides):
    base = dict(
        stage="stage_action", correction_of_model_request_id=None,
        context=SimpleNamespace(history=()), history_catalog=(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_hint_lists_every_eligible_ref():
    eligible = ["verified_fact:a:" + "1" * 64, "verified_fact:b:" + "2" * 64]
    req = request(
        correction_of_model_request_id="model/x",
        context=SimpleNamespace(history=[history_item(ref) for ref in eligible]),
    )
    hint = completion_support_hint(req)
    assert hint.startswith(" For turn_completion")
    assert all(ref in hint for ref in eligible)


def test_hint_is_absent_without_a_correction():
    ref = "verified_fact:a:" + "1" * 64
    req = request(context=SimpleNamespace(history=[history_item(ref)]))
    assert completion_support_hint(req) == ""


def test_hint_skips_stages_without_completion():
    ref = "verified_fact:a:" + "1" * 64
    req = request(
        stage="stage_review", correction_of_model_request_id="model/x",
        context=SimpleNamespace(history=[history_item(ref)]),
    )
    assert completion_support_hint(req) == ""


def test_hint_ignores_unreliable_facts():
    eligible = "verified_fact:b:" + "2" * 64
    conflicted = "verified_fact:a:" + "1" * 64
    req = request(
        correction_of_model_request_id="model/x",
        context=SimpleNamespace(history=[
            history_item(conflicted, conflict=True), history_item(eligible),
        ]),
    )
    hint = completion_support_hint(req)
    assert eligible in hint
    assert conflicted not in hint


def test_catalog_marks_only_eligible_refs():
    eligible = "verified_fact:a:" + "1" * 64
    ineligible = "verified_fact:b:" + "2" * 64
    catalog = (
        {"ref": eligible, "record_id": "a"},
        {"ref": ineligible, "record_id": "b"},
    )
    marked = catalog_with_completion_support(catalog, [eligible])
    assert [entry["completion_eligible"] for entry in marked] == [True, False]
    # The marker is additive: every other key survives unchanged.
    assert [entry["record_id"] for entry in marked] == ["a", "b"]


def test_frozen_history_ignores_the_completion_marker():
    task_run_id = str(uuid4())
    record_id = str(uuid4())
    ref = f"verified_fact:{record_id}:" + "1" * 64
    entry = {
        "ref": ref, "record_id": record_id, "task_run_id": task_run_id,
        "record_type": "verified_fact", "turn_id": None, "intent_id": None,
        "artifact_version_id": None, "content": "", "source_refs": (),
        "source_version": "1" * 64, "committed_sequence": 1,
        "identity": "reviewed_formal_fact", "scope": "current",
        "source_status": "available", "conflict": False,
        "completion_eligible": False,
    }
    items = frozen_history(SimpleNamespace(
        context=SimpleNamespace(history=()), history_catalog=(entry,),
    ))
    assert len(items) == 1 and items[0].ref == ref
